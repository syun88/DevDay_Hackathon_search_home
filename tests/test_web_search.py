"""Web search fallback tests. No external requests are made here."""
import json
import tempfile
import unittest
from pathlib import Path

import web_search
from home_agent import HomeAgent, fallback_query, tool_outcome
from session_store import SessionStore

DETAIL = 'https://www.e-room.co/room/17212/マンションVIP日本橋浜町-701/'
SEARCH_PAGE = 'https://www.homes.co.jp/chintai/tokyo/chuo/list/'
SUUMO = 'https://suumo.jp/chintai/jnc_000109433742/'


def response(results, sources=(DETAIL, SEARCH_PAGE, SUUMO), searched=True, cited=()):
    """A Responses API body shaped like the documented web_search output."""
    output = []
    if searched:
        output.append({'type': 'web_search_call', 'status': 'completed', 'action': {
            'type': 'search', 'query': '東京駅 1LDK 賃貸', 'sources': [{'type': 'url', 'url': u} for u in sources]}})
    output.append({'type': 'message', 'role': 'assistant', 'content': [{
        'type': 'output_text', 'text': json.dumps({'summary': 'Found pages.', 'results': results}, ensure_ascii=False),
        'annotations': [{'type': 'url_citation', 'url': u, 'title': 't', 'start_index': 0, 'end_index': 1} for u in cited]}]})
    return {'status': 'completed', 'output': output}


def row(url, kind='listing_detail', **values):
    base = {'url': url, 'title': 'Sample page', 'kind': kind, 'snippet': '1LDK ([e-room.co](https://e-room.co/x)) <b>note</b>',
            'address': '東京都中央区日本橋浜町1丁目11-3', 'rent_yen': 96000, 'management_fee_yen': 9000,
            'layout': '1LDK', 'area_m2': 34.52, 'station_access': '浜町駅 徒歩4分'}
    base.update(values)
    return base


class ParseTest(unittest.TestCase):
    def test_only_urls_the_search_returned_are_kept(self):
        invented = 'https://example.com/invented-home'
        result = web_search.parse(response([row(DETAIL), row(invented), row('http://insecure.example.com/x')]), 'q')
        self.assertEqual(result['status'], 'ok')
        self.assertEqual([r['url'] for r in result['results']], [DETAIL])
        self.assertEqual(result['dropped_unverified'], 2)
        record = result['results'][0]
        self.assertTrue(record['discovery_only'] and record['web_result'])
        self.assertEqual((record['availability_status'], record['last_verified_at'], record['lat']), ('unknown', None, None))
        self.assertEqual(record['extracted']['price'], 9.6)
        self.assertEqual(record['snippet'], '1LDK e-room.co note')  # Markup and links are stripped.

    def test_percent_encoded_citation_matches_the_same_page(self):
        cited = 'https://www.e-room.co/room/17212/%E3%83%9E%E3%83%B3%E3%82%B7%E3%83%A7%E3%83%B3VIP%E6%97%A5%E6%9C%AC%E6%A9%8B%E6%B5%9C%E7%94%BA-701/'
        result = web_search.parse(response([row(DETAIL)], sources=(), cited=(cited,)), 'q')
        self.assertEqual(len(result['results']), 1)

    def test_search_pages_and_restricted_sources_carry_no_home_facts(self):
        result = web_search.parse(response([row(SEARCH_PAGE, 'listing_search_page'), row(SUUMO)]), 'q', ('suumo.jp',))
        page, suumo = result['results']
        self.assertIsNone(page['extracted']['address'])
        self.assertIsNone(page['extracted']['price'])
        self.assertIn('not one specific home', ' '.join(page['limitations']))
        self.assertEqual((suumo['extracted']['address'], suumo['extracted']['price'], suumo['snippet']), (None, None, ''))
        self.assertIn('reuse is not permitted', ' '.join(suumo['limitations']))

    def test_implausible_values_are_dropped(self):
        result = web_search.parse(response([row(DETAIL, rent_yen=12, area_m2=-3, management_fee_yen=True)]), 'q')
        self.assertEqual(result['results'][0]['extracted'], {**result['results'][0]['extracted'], 'price': None, 'area': None, 'management_fee_yen': None})

    def test_no_search_call_or_bad_output_is_not_success(self):
        self.assertEqual(web_search.parse(response([row(DETAIL)], searched=False), 'q')['status'], 'not_run')
        broken = response([])
        broken['output'][-1]['content'][0]['text'] = 'not json'
        self.assertEqual(web_search.parse(broken, 'q')['status'], 'invalid_output')
        self.assertEqual(web_search.parse(response([]), 'q')['status'], 'empty')

    def test_search_uses_bounded_documented_request_and_caches(self):
        calls = []
        def transport(key, body):
            calls.append(body)
            return response([row(DETAIL)])
        web_search._cache.clear()
        first = web_search.search('synthetic-key', 'gpt-6.1-sol', 'Tokyo 1LDK', 'near Tokyo', transport=transport)
        second = web_search.search('synthetic-key', 'gpt-6.1-sol', 'Tokyo 1LDK', 'near Tokyo', transport=transport)
        self.assertEqual(len(calls), 1)
        self.assertFalse(first['cached']); self.assertTrue(second['cached'])
        body = calls[0]
        self.assertEqual(body['tools'][0]['type'], 'web_search')
        self.assertEqual(body['tool_choice'], 'required')
        self.assertIn('web_search_call.action.sources', body['include'])
        self.assertEqual(body['model'], 'gpt-6.1-sol')
        with self.assertRaises(web_search.WebSearchError):
            web_search.search('', 'm', 'Tokyo 1LDK')
        with self.assertRaises(web_search.WebSearchError):
            web_search.search('k', 'm', 'x')


class AgentFallbackTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.token, _ = self.store.create()
        self.listings = {'results': [], 'leads': [], 'provider': 'discovery', 'inventory_status': 'no_verified_feed_configured',
                         'source_search_links': [{'source': 'SUUMO', 'url': 'https://www.google.com/search?q=site%3Asuumo.jp'}]}
        self.searches, self.web = [], {'status': 'ok', 'query': 'q', 'queries': ['q'], 'results': []}
        self.places = []
        def fake_web(query, context=''):
            self.searches.append((query, context))
            if isinstance(self.web, Exception):
                raise self.web
            return json.loads(json.dumps(self.web))
        self.agent = HomeAgent(self.store, 'synthetic-key', lambda *args: json.loads(json.dumps(self.listings)),
                               geocoder=lambda query: self.places, web_search=fake_web)
        self.agent.resume = lambda token: None
        self.agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Find a 1LDK',
                                      'expected_revision': 0, 'language': 'en'})
        self.store.mutate(self.token, lambda s: s['criteria'].update(property_type='rent', layout='1LDK', max_price_yen=150000))
        self.count = 0

    def execute(self, name, args=None):
        self.count += 1
        return self.agent.execute(self.token, 'request_1234567890', {'type': 'function_call', 'call_id': 'call_%d' % self.count,
                                  'turn_id': 'turn_1', 'name': name, 'arguments': args or {}})

    def records(self, *rows):
        return web_search.parse(response(list(rows)), 'q')['results']

    def test_empty_listing_sources_trigger_a_real_fallback_search(self):
        self.web['results'] = self.records(row(DETAIL), row(SEARCH_PAGE, 'listing_search_page'))
        result = self.execute('search_listings')
        self.assertEqual(len(self.searches), 1)
        query, context = self.searches[0]
        self.assertEqual(query, fallback_query(self.store.get(self.token)['criteria']))
        self.assertIn('1LDK', query); self.assertIn('15万円以下', query)
        self.assertIn('150,000 yen', context)
        self.assertEqual(result['web_search']['status'], 'ok')
        self.assertNotIn('results', result['web_search'])
        self.assertEqual(result['counts']['web'], 2)
        self.assertEqual(result['counts']['confirmed'], 0)
        for item in result['items']:
            self.assertTrue(item['id'].startswith('listing_'))
            self.assertEqual(item['match']['status'], 'unverified')  # Never a verified match.
        self.assertEqual(self.store.get(self.token)['result_sets'][-1]['id'], result['id'])

    def test_listing_leads_do_not_trigger_web_search(self):
        self.listings['leads'] = [{'title': 'Lead', 'url': 'https://example.com/lead', 'discovery_only': True, 'extracted': {}}]
        self.execute('search_listings')
        self.assertEqual(self.searches, [])

    def test_failed_and_empty_searches_report_actual_status_without_inventing(self):
        self.web = web_search.WebSearchError('rate_limited')
        result = self.execute('search_listings')
        self.assertEqual((result['web_search']['status'], result['web_search']['code']), ('error', 'rate_limited'))
        self.assertEqual(result['items'], [])
        self.assertTrue(result['source_search_links'])
        self.web = {'status': 'empty', 'query': 'q', 'queries': ['q'], 'results': []}
        explicit = self.execute('web_search', {'query': 'Tokyo Station 1LDK rent'})
        self.assertEqual(explicit['web_search']['status'], 'empty')
        self.assertEqual(explicit['items'], [])
        self.assertEqual(tool_outcome('web_search', explicit), 'empty')

    def test_explicit_search_creates_stable_records_and_is_bounded_per_message(self):
        self.web['results'] = self.records(row(DETAIL))
        first = self.execute('web_search', {'query': 'Tokyo Station 1LDK rent'})
        self.assertEqual(first['provider'], 'web_search')
        self.assertEqual(first['items'][0]['url'], DETAIL)
        self.assertEqual(tool_outcome('web_search', first), 'done')
        self.execute('web_search', {'query': 'another search'})
        third = self.execute('web_search', {'query': 'third search'})
        self.assertEqual((third['web_search']['status'], third['web_search']['code']), ('skipped', 'limit'))
        self.assertEqual(len(self.searches), 2)
        self.assertEqual(self.execute('web_search', {'query': 'x'})['error'], 'invalid_arguments')

    def test_pins_only_from_a_matching_geocode_of_a_single_property_page(self):
        self.web['results'] = self.records(row(DETAIL), row(SEARCH_PAGE, 'listing_search_page', address='東京都中央区'))
        self.places = [{'name': '東京駅', 'lat': 35.68, 'lon': 139.76}]  # Unrelated hit must not become a pin.
        unrelated = self.execute('web_search', {'query': 'Tokyo Station 1LDK rent'})
        self.assertTrue(all(item['lat'] is None for item in unrelated['items']))
        self.store.mutate(self.token, lambda s: s['chat'].update(web_searches=0))
        self.places = [{'name': '東京都中央区日本橋浜町', 'lat': 35.6866, 'lon': 139.7895}]
        located = self.execute('web_search', {'query': 'Nihonbashi 1LDK rent'})
        detail, page = located['items']
        self.assertEqual((detail['lat'], detail['geo_precision']), (35.6866, 'approximate'))
        self.assertIn('not exact building', detail['location_evidence']['method'])
        self.assertIsNone(page['lat'])  # A search/list page is never placed as a home.
        self.assertEqual(located['map_action']['map_command']['action'], 'show_results')


if __name__ == '__main__':
    unittest.main()
