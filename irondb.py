import itertools
import time
import threading
import django
import copy

try:
    import irondb_flatcc as irondb_flatbuf
except ImportError:
    import irondb_flatbuf

from graphite.intervals import Interval, IntervalSet
from graphite.node import LeafNode, BranchNode


import json
import requests

try:
    from json.decoder import JSONDecodeError
except ImportError:
    JSONDecodeError = ValueError


class URLs(object):
    def __init__(self, hosts):
        self.iterator = itertools.cycle(hosts)
        self.hc = len(hosts)

    @property
    def host(self):
        return next(self.iterator)

    @property
    def names(self):
        return '{0}/metrics/find/'.format(self.host)

    @property
    def series(self):
        return '{0}/series/'.format(self.host)

    @property
    def series_multi(self):
        return '{0}/series_multi/'.format(self.host)

    @property
    def host_count(self):
        return self.hc

urls = None
urllength = 4096


class IronDBMeasurementFetcher(object):
    __slots__ = ('leaves','lock', 'fetched', 'results', 'headers', 'database_rollups', 'timeout', 'connection_timeout', 'retries')

    def __init__(self, headers, timeout, connection_timeout, db_rollups, retries):
        self.leaves = list()
        self.lock = threading.Lock()
        self.fetched = False
        self.results = {}
        self.headers = {}
        self.timeout = timeout
        self.connection_timeout = connection_timeout
        self.database_rollups = db_rollups
        self.retries = retries
        if headers:
            self.headers = headers

    def add_leaf(self, leaf_name, leaf_data):
        self.leaves.append({'leaf_name': leaf_name, 'leaf_data': leaf_data})

    def fetch(self, start_time, end_time):
        if (self.fetched == False):
            self.lock.acquire()
            # recheck in case we were waiting
            if (self.fetched == False):
                params = {}
                params['names'] = self.leaves
                params['start'] = start_time
                params['end'] = end_time
                params['database_rollups'] = self.database_rollups
                tries = self.retries
                for i in range(0, min(urls.host_count, tries)):
                    try:
                        self.fetched = False
                        d = requests.post(urls.series_multi, json = params, headers = self.headers, timeout=((self.connection_timeout / 1000), (self.timeout / 1000)))
                        if d.headers['content-type'] == 'application/json':
                            self.results = d.json()
                            self.fetched = True
                        elif d.headers['content-type'] == 'application/x-flatbuffer-metric-get-result-list':
                            self.results = irondb_flatbuf.metric_get_results(d.content)
                            self.fetched = True
                        else:
                            pass
                        break
                    except requests.exceptions.ConnectionError:
                        # on down nodes, retry on another up to "tries" times
                        pass
                    except requests.exceptions.ConnectTimeout:
                        # on down nodes, retry on another up to "tries" times
                        pass
                    except JSONDecodeError:
                        pass
                    except requests.exceptions.ReadTimeout:
                        # read timeouts are failures, stop immediately
                        break

            self.lock.release()
            
    def is_error(self):
        return self.fetched == False or self.results == None or 'error' in self.results or 'series' not in self.results or len(self.results['series']) == 0

    def series(self, name):
        if self.is_error():
            return

        time_info = self.results['from'], self.results['to'], self.results['step']
        if len(self.results['series'].get(name, [])) == 0:
            return time_info, [None] * ((self.results['to'] - self.results['from']) / self.results['step'])

        return time_info, self.results['series'].get(name, [])

class IronDBReader(object):
    __slots__ = ('name', 'fetcher',)

    def __init__(self, name, fetcher):
        self.name = name
        self.fetcher = fetcher

    def fetch(self, start_time, end_time):
        self.fetcher.fetch(start_time, end_time)
        return self.fetcher.series(self.name)

    def get_intervals(self):
        # all time
        return IntervalSet([Interval(0, int(time.time()))])


class IronDBFinder(object):
    __slots__ = ('disabled', 'batch_size', 'database_rollups', 'timeout', 'connection_timeout', 'headers', 'disabled', 'max_retries')

    def __init__(self, config=None):
        global urls
        self.batch_size = 250
        self.database_rollups = True
        self.timeout = 10000
        self.connection_timeout = 3005
        self.headers = {}
        self.disabled = False
        self.max_retries = 2
        if config is not None:
            if 'urls' in config['irondb']:
                urls = config['irondb']['urls']
            else:
                urls = [config['irondb']['url'].strip('/')]
            if 'batch_size' in config['irondb']:
                self.batch_size = config['irondb']['batch_size']
        else:
            from django.conf import settings
            urls = getattr(settings, 'IRONDB_URLS')
            if not urls:
                urls = [settings.IRONDB_URL]
            try:
                bs = getattr(settings, 'IRONDB_BATCH_SIZE')
                if bs:
                    self.batch_size = int(bs)
            except AttributeError:
                self.batch_size = 250
            try:
                to = getattr(settings, 'IRONDB_TIMEOUT_MS')
                if to:
                    self.timeout = int(to)
            except AttributeError:
                self.timeout = 10000
            try:
                cto = getattr(settings, 'IRONDB_CONNECTION_TIMEOUT_MS')
                if cto:
                    self.connection_timeout = int(cto)
            except AttributeError:
                self.connection_timeout = 3005

            try:
                token = getattr(settings, 'CIRCONUS_TOKEN')
                if token:
                    self.headers['X-Circonus-Auth-Token'] = token
                    self.headers['X-Circonus-App-Name'] = 'graphite-web'
            except AttributeError:
                self.headers = {}

            self.headers['X-Snowth-Timeout'] = str(self.timeout) + 'ms'

            try:
                self.database_rollups = getattr(settings, 'IRONDB_USE_DATABASE_ROLLUPS')
            except AttributeError:
                self.database_rollups = True
            try:
                mr = getattr(settings, 'IRONDB_MAX_RETRIES')
                if mr:
                    self.max_retries = int(mr)
            except AttributeError:
                self.max_retries = 2


        urls = URLs(urls)

    def find_nodes(self, query):
        names = {}
        tries = self.max_retries
        name_headers = copy.deepcopy(self.headers)
        name_headers['Accept'] = 'application/x-flatbuffer-metric-find-result-list'
        for i in range(0, min(urls.host_count, tries)):
            try:
                r = requests.get(urls.names, params={'query': query.pattern}, headers=name_headers, timeout=((self.connection_timeout / 1000), (self.timeout / 1000)))
                if r.headers['content-type'] == 'application/json':
                    names = r.json()
                elif r.headers['content-type'] == 'application/x-flatbuffer-metric-find-result-list':
                    names = irondb_flatbuf.metric_find_results(r.content)
                else:
                    pass
                break
            except requests.exceptions.ConnectionError:
                # on down nodes, try again on another node until "tries"
                pass
            except requests.exceptions.ConnectTimeout:
                # on down nodes, try again on another node until "tries"
                pass
            except requests.exceptions.ReadTimeout:
                # up node that simply timed out is a failure
                break

        # for each set of self.batch_size leafnodes, execute an IronDBMeasurementFetcher
        # so we can do these in batches.
        counter = 0
        measurement_headers = copy.deepcopy(self.headers)
        measurement_headers['Accept'] = 'application/x-flatbuffer-metric-get-result-list'
        fetcher = IronDBMeasurementFetcher(measurement_headers, self.timeout, self.connection_timeout, self.database_rollups, self.max_retries)

        for name in names:
            if 'leaf' in name and 'leaf_data' in name:
                fetcher.add_leaf(name['name'], name['leaf_data'])
                reader = IronDBReader(name['name'], fetcher)
                counter = counter + 1
                if (counter % self.batch_size == 0):
                    fetcher = IronDBMeasurementFetcher(self.headers, self.timeout, self.connection_timeout, self.database_rollups, self.max_retries)
                    counter = 0
                yield LeafNode(name['name'], reader)
            else:
                yield BranchNode(name['name'])
