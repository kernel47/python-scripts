import asyncio
import contextlib
import io
import json
import unittest
import threading
from unittest.mock import patch

import httpx
import elk_search as elk


class SearchTests(unittest.TestCase):
    def test_day_inclusive_and_dst(self):
        query = elk.build_query(elk.SearchOptions('logs', start='29-03-2026', end='29-03-2026', timezone='Europe/Paris'))
        bounds = query['query']['bool']['filter'][0]['range']['@timestamp']
        self.assertEqual(bounds['lt'] - bounds['gte'], 23 * 3600 * 1000)
        self.assertEqual(bounds['format'], 'epoch_millis')

    def test_timestamp_units(self):
        self.assertEqual(elk.date_bound('1700000000.123', 'UTC', 'seconds'), ('gte', 1700000000123))
        self.assertEqual(elk.date_bound('1700000000123', 'UTC', 'milliseconds', True), ('lte', 1700000000123))
        for value in ('nan', 'inf', '31-02-2026', 'abc', '0.0001'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                elk.date_bound(value, 'UTC', 'seconds')

    def test_invalid_range(self):
        with self.assertRaises(ValueError):
            elk.build_query(elk.SearchOptions('logs', start='07-10-2026', end='06-10-2026'))

    def test_combined_filters(self):
        query = elk.build_query(elk.SearchOptions('logs-*', fields=['host.name'],
            labels=['EMEA', 'APAC'], texts=['connection failed', 'timeout'], text_operator='any'))
        filters = query['query']['bool']['filter']
        self.assertEqual(query['_source'], ['host.name'])
        self.assertEqual(filters[0], {'terms': {'label.keyword': ['EMEA', 'APAC']}})
        self.assertEqual(filters[1]['bool']['minimum_should_match'], 1)
        self.assertEqual(filters[1]['bool']['should'][0], {'match_phrase': {'content': 'connection failed'}})

    def test_exact_and_words(self):
        for mode, key in [('exact', 'term'), ('words', 'match')]:
            query = elk.build_query(elk.SearchOptions('logs', texts=['a', 'b'], text_mode=mode))
            filters = query['query']['bool']['filter']
            self.assertEqual(len(filters), 2)
            self.assertIn(key, filters[0])

    def response(self, **overrides):
        data = {'timed_out': False, '_shards': {'failed': 0}, 'hits': {
            'total': {'value': 2, 'relation': 'eq'},
            'hits': [{'_index': 'logs', '_id': '1', '_source': {'content': ['hello', 'world']}}]}}
        data.update(overrides)
        return httpx.Response(200, json=data)

    def test_response_and_partial_rejection(self):
        report = elk.parse_response(self.response())
        self.assertTrue(report['truncated'])
        self.assertEqual(report['documents'][0]['attributes']['content'], ['hello', 'world'])
        for response in (self.response(timed_out=True), self.response(_shards={'failed': 1}),
                         httpx.Response(200, text='invalid'), httpx.Response(403)):
            with self.assertRaises(elk.SearchError):
                elk.parse_response(response)

    def test_sync_async_transport_parity(self):
        requests = []
        def handler(request):
            requests.append((request.url.path, dict(request.url.params), json.loads(request.content)))
            return self.response()
        transport = httpx.MockTransport(handler)
        sync_client, async_client = httpx.Client, httpx.AsyncClient
        with patch.dict(elk.os.environ, {}, clear=True), \
             patch.object(httpx, 'Client', side_effect=lambda **kw: sync_client(transport=transport, **kw)), \
             patch.object(httpx, 'AsyncClient', side_effect=lambda **kw: async_client(transport=transport, **kw)):
            options = elk.SearchOptions('logs-*', texts=['timeout'])
            sync = elk.search_sync(options, 'https://elastic.example:9200')
            asynchronous = asyncio.run(elk.search_async(options, 'https://elastic.example:9200'))
        self.assertEqual(sync, asynchronous)
        self.assertEqual(requests[0], requests[1])
        self.assertEqual(requests[0][1], {'allow_partial_search_results': 'false'})

    def test_auth_conflicts_and_url(self):
        with patch.dict(elk.os.environ, {'ELASTIC_USER': 'u', 'ELASTIC_PASSWORD': 'p', 'ELASTIC_API_KEY': 'k'}):
            with self.assertRaises(ValueError):
                elk.client_options('https://localhost:9200')
        with self.assertRaises(ValueError):
            elk.client_options('https://user:secret@localhost:9200')

    def regional_report(self):
        return {'total': 1, 'returned': 1, 'truncated': False,
                'documents': [{'id': 'same-id', 'index': 'logs', 'attributes': {}}]}

    def test_region_selection(self):
        with patch.dict(elk.os.environ, {'ELASTIC_EMEA_URL': 'https://emea:9200'}, clear=True):
            self.assertEqual(elk.selected_targets('EMEA'), {'EMEA': 'https://emea:9200'})
            self.assertEqual(list(elk.selected_targets(None)), ['EMEA', 'APAC', 'AMER'])
            with self.assertRaises(ValueError):
                elk.selected_targets(None, 'https://one:9200')

    def test_sync_regions_are_concurrent(self):
        barrier = threading.Barrier(3)
        def worker(*args):
            barrier.wait(timeout=3)
            return self.regional_report()
        with patch.object(elk, 'search_sync', side_effect=worker):
            result = elk.search_regions_sync(elk.SearchOptions('logs'), dict.fromkeys(elk.ELK_REGIONS, 'https://elk:9200'))
        self.assertEqual(result['total'], 3)
        self.assertEqual([d['region'] for d in result['documents']], ['EMEA', 'APAC', 'AMER'])
        self.assertTrue(result['complete'])

    def test_async_regions_concurrent_and_partial(self):
        async def scenario():
            started = []
            ready = asyncio.Event()
            async def worker(options, url, ca, timeout, region):
                started.append(region)
                if len(started) == 3:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 3)
                if region == 'APAC':
                    raise elk.SearchError('API unavailable')
                return self.regional_report()
            with patch.object(elk, 'search_async', side_effect=worker):
                return await elk.search_regions_async(elk.SearchOptions('logs'), dict.fromkeys(elk.ELK_REGIONS, 'https://elk:9200'))
        result = asyncio.run(scenario())
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertIsNone(result['total'])
        self.assertEqual(result['total_available'], 2)
        self.assertEqual(result['regions']['APAC']['status'], 'ERROR')

    def test_single_region_and_missing_urls(self):
        with patch.object(elk, 'search_sync', return_value=self.regional_report()) as search:
            result = elk.search_regions_sync(elk.SearchOptions('logs'), {'EMEA': 'https://emea:9200'})
            self.assertEqual(search.call_count, 1)
            self.assertEqual(list(result['regions']), ['EMEA'])
        result = elk.search_regions_sync(elk.SearchOptions('logs'), {'EMEA': '', 'APAC': ''})
        self.assertEqual(result['status'], 'ERROR')
        self.assertIsNone(result['total'])

    def test_regional_auth_does_not_mix_global_credentials(self):
        with patch.dict(elk.os.environ, {'ELASTIC_USER': 'global', 'ELASTIC_PASSWORD': 'secret',
                                        'ELASTIC_APAC_API_KEY': 'regional'}, clear=True):
            config = elk.client_options('https://apac:9200', region='APAC')
            self.assertIsNone(config['auth'])
            self.assertEqual(config['headers']['Authorization'], 'ApiKey regional')
            self.assertEqual(elk.client_options('https://emea:9200', region='EMEA')['auth'], ('global', 'secret'))

    def test_dry_run_without_network(self):
        with contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.object(elk, 'search_sync', side_effect=AssertionError('network')):
            code = elk.main(['--index', 'logs', '--from', '06-10-2026', '--dry-run'])
        self.assertEqual(code, 0)
        self.assertIn('query', json.loads(output.getvalue()))


if __name__ == '__main__':
    unittest.main()
