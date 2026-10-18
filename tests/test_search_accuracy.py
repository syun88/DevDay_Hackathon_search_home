"""Search accuracy regressions traced from live results. No external requests are made here."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import page_verify
import public_housing
import web_search
from home_agent import HomeAgent, apply_verification, discovery_checks, qualify, compact, INSTRUCTIONS
from session_store import DEFAULT_CRITERIA, SessionStore
try:
    from test_agent import full_result
except ImportError:
    from tests.test_agent import full_result

DETAIL = 'https://www.eheya.net/detail/300001333005653000002/'
LIBRARY = 'https://www.eheya.net/library/detail/t00656023/'
HOMES = 'https://www.homes.co.jp/chintai/b-1403170024163/'
HOMES_ROOM = 'https://www.homes.co.jp/chintai/room/b5925824ca94f4a2017a181f0a79aeb00be9501f/'
LIST = 'https://www.chintai.net/tokyo/ensen/000001700/list/1ldk/'
CRITERIA = {**DEFAULT_CRITERIA, 'property_type': 'rent', 'layout': '1LDK', 'max_price_yen': 150000, 'radius_a_km': 3}


def page(title, body='', canonical=None, ld=None):
    html = f'<html><head><title>{title}</title>'
    if canonical:
        html += f'<link rel="canonical" href="{canonical}">'
    if ld:
        html += f'<script type="application/ld+json">{json.dumps(ld, ensure_ascii=False)}</script>'
    return html + f'</head><body>{body}</body></html>'


class GeocodePrecisionTest(unittest.TestCase):
    def candidates(self, *titles):
        return [{'properties': {'title': t}, 'geometry': {'coordinates': [139.7 + i / 100, 35.6 + i / 100]}} for i, t in enumerate(titles)]

    def test_most_specific_prefix_candidate_wins_over_the_first(self):
        found = public_housing.best_candidate('東京都中央区日本橋浜町３丁目10-1', self.candidates('東京都中央区', '東京都中央区日本橋浜町三丁目'))
        self.assertEqual(found[0], '東京都中央区日本橋浜町三丁目')
        self.assertIsNone(public_housing.best_candidate('東京都中央区湊1-1', self.candidates('東京都港区湊')))

    def test_levels_distinguish_block_chome_town_and_municipality(self):
        cases = {'東京都中央区': 'municipality', '東京都中央区明石町': 'town', '東京都千代田区一番町': 'town',
                 '東京都中央区日本橋浜町三丁目': 'chome', '東京都千代田区四番町６番地': 'block', '東京都中央区日本橋浜町3-10-1': 'block',
                 '神奈川県横浜市中区本町一丁目': 'chome', '石川県河北郡津幡町字太田': 'town'}
        for title, level in cases.items():
            with self.subTest(title=title):
                self.assertEqual(public_housing.geocode_level(title), level)


class PageVerifyTest(unittest.TestCase):
    def check(self, response, extracted=None, url=DETAIL, restricted=()):
        def fetcher(target):
            if isinstance(response, Exception):
                raise response
            return response
        return page_verify.verify(url, extracted or {}, restricted, fetcher=fetcher)

    def test_redirect_to_another_page_family_is_reported_as_moved(self):
        result = self.check((200, LIBRARY, 'text/html', page('セグレート日本橋の物件情報', canonical=LIBRARY)))
        self.assertTrue(result['moved'])
        self.assertEqual(result['canonical_url'], LIBRARY)

    def test_same_listing_canonical_and_facts_found_on_the_page(self):
        body = '1LDK 34.08㎡ 賃料 14万円 管理費 なし 東京都中央区明石町'
        ld = {'@type': 'Residence', 'address': {'addressRegion': '東京都', 'addressLocality': '中央区', 'streetAddress': '明石町'}}
        result = self.check((200, HOMES_ROOM, 'text/html; charset=utf-8', page('明石町 1LDK', body, HOMES_ROOM, ld)),
                            {'price': 14.0, 'layout': '1LDK', 'area': 34.08, 'address': '東京都中央区明石町', 'management_fee_yen': 9000}, HOMES)
        self.assertFalse(result['moved'])
        self.assertEqual(result['facts'], {'price': 'page', 'layout': 'page', 'area': 'page', 'address': 'page', 'management_fee_yen': 'not_found'})
        self.assertEqual(result['page_addresses'], ['東京都中央区明石町'])

    def test_ended_search_missing_blocked_restricted_and_unsafe_pages(self):
        self.assertTrue(self.check((200, DETAIL, 'text/html', page('物件詳細', 'この物件の掲載は終了しました')))['looks_ended'])
        self.assertTrue(self.check((200, LIST, 'text/html', page('一覧')), url=LIST)['looks_like_search_page'])
        self.assertEqual(self.check(HTTPError(DETAIL, 404, 'x', {}, None))['status'], 'gone')
        self.assertEqual(self.check(HTTPError(DETAIL, 403, 'x', {}, None))['status'], 'blocked')
        self.assertEqual(self.check(URLError('down'))['status'], 'unreachable')
        self.assertEqual(self.check(None, url='https://suumo.jp/chintai/jnc_1/', restricted=('suumo.jp',))['status'], 'not_permitted')
        self.assertEqual(page_verify.verify('http://example.com/x', {})['status'], 'unsafe_url')
        for host in ('localhost', '127.0.0.1', '10.0.0.8', '169.254.169.254', 'printer.local'):
            self.assertFalse(page_verify.public_host(host), host)

    def test_redirects_to_private_or_insecure_targets_are_refused(self):
        handler = page_verify.SafeRedirects()
        for target in ('http://example.com/', 'https://127.0.0.1/admin', 'https://localhost:8766/api/session'):
            with self.assertRaises(URLError):
                handler.redirect_request(None, None, 302, 'Found', {}, target)

    def test_robots_disallow_prevents_fetching(self):
        page_verify._robots.clear()
        robots = (200, 'https://www.example.co.jp/robots.txt', 'text/plain', 'User-agent: *\nDisallow: /room/')
        with patch.object(page_verify, 'fetch', side_effect=[robots]) as fetch, patch.object(page_verify, 'public_host', return_value=True):
            result = page_verify.verify('https://www.example.co.jp/room/1/', {})
        self.assertEqual(result['status'], 'robots_disallowed')
        self.assertEqual(fetch.call_count, 1)  # Only robots.txt was read.


class ChecksTest(unittest.TestCase):
    def test_budget_layout_and_radius_are_checked_independently(self):
        over = {'extracted': {'price': 13.8, 'management_fee_yen': 15000, 'layout': '1LDK'}, 'lat': 35.6852, 'lon': 139.7895, 'geocode_level': 'chome'}
        checks = discovery_checks(over, CRITERIA)
        self.assertEqual(checks['budget']['result'], 'fail')
        self.assertEqual(checks['layout']['result'], 'pass')
        self.assertEqual(checks['radius_A']['result'], 'pass')
        unknown_fee = discovery_checks({'extracted': {'price': 13.5, 'layout': '2LDK'}}, CRITERIA)
        self.assertEqual((unknown_fee['budget']['result'], unknown_fee['layout']['result'], unknown_fee['radius_A']['result']), ('unknown', 'fail', 'unknown'))
        # A town-level centre near the boundary stays unknown instead of a false pass.
        edge = discovery_checks({'extracted': {}, 'area_center': {'lat': 35.6812 + 2.7 / 111, 'lon': 139.7671, 'level': 'town'}}, CRITERIA)
        self.assertEqual(edge['radius_A']['result'], 'unknown')

    def test_verification_outcomes_shape_reliability_and_links(self):
        moved = apply_verification({'url': DETAIL, 'page_kind': 'listing_detail', 'extracted': {'price': 13.5}},
                                   {'status': 'ok', 'final_url': LIBRARY, 'canonical_url': LIBRARY, 'moved': True, 'facts': {'price': 'page'}})
        self.assertEqual((moved['reliability'], moved['listing_status'], moved['url'], moved['search_result_url']), ('ended_or_moved', 'ended_or_moved', LIBRARY, DETAIL))
        demoted = apply_verification({'url': LIST, 'page_kind': 'listing_detail', 'extracted': {'address': '東京都'}},
                                     {'status': 'ok', 'final_url': LIST, 'looks_like_search_page': True, 'facts': {}})
        self.assertEqual((demoted['page_kind'], demoted['reliability']), ('listing_search_page', 'search_page'))
        unchecked = apply_verification({'url': HOMES, 'page_kind': 'listing_detail', 'extracted': {'price': 14.0}}, {'status': 'blocked'})
        self.assertEqual((unchecked['reliability'], unchecked['fact_sources']), ('page_not_checked', {'price': 'search_summary'}))


class AgentAccuracyTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.token, _ = self.store.create()
        self.pages, self.places, self.refines = {}, {}, []
        self.rows = [[]]
        def fake_web(query, context='', refine=False):
            self.refines.append(refine)
            rows = self.rows[min(len(self.refines) - 1, len(self.rows) - 1)]
            sources = [r['url'] for r in rows]
            body = {'status': 'completed', 'output': [{'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'query': query, 'sources': [{'type': 'url', 'url': u} for u in sources]}},
                    {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'summary': '', 'results': rows}, ensure_ascii=False)}]}]}
            return web_search.parse(body, query)
        verify = lambda url, extracted: page_verify.verify(url, extracted, (), fetcher=lambda u: self.pages[u])
        self.agent = HomeAgent(self.store, 'synthetic-key', lambda *a: {'results': [], 'leads': []}, geocoder=lambda q: self.places.get(q, []),
                               web_search=fake_web, page_verifier=verify)
        self.agent.resume = lambda token: None
        self.agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Find', 'expected_revision': 0, 'language': 'en'})
        self.store.mutate(self.token, lambda s: s['criteria'].update(CRITERIA))

    def row(self, url, kind='listing_detail', address='東京都中央区日本橋浜町３丁目', rent=135000, fee=15000, layout='1LDK'):
        return {'url': url, 'title': 'x', 'kind': kind, 'snippet': '', 'address': address, 'rent_yen': rent, 'management_fee_yen': fee,
                'layout': layout, 'area_m2': 36.84, 'station_access': None}

    def run_search(self):
        return full_result(self.store, self.token, self.agent.execute(self.token, 'request_1234567890', {'type': 'function_call', 'call_id': 'call_1', 'turn_id': 't', 'name': 'web_search', 'arguments': {'query': 'Tokyo Station 1LDK'}}))

    def test_traced_live_case_moved_listing_town_address_and_list_page(self):
        town = 'https://www.homes.co.jp/chintai/b-2/'
        self.rows = [[self.row(DETAIL), self.row(town, address='東京都中央区明石町', rent=140000, fee=None), self.row(LIST, 'listing_search_page', None, None, None)]]
        self.pages = {DETAIL: (200, LIBRARY, 'text/html', page('建物情報', '賃料 13.5万円 1LDK')),
                      town: (200, town, 'text/html', page('1LDK', '14万円 1LDK 東京都中央区明石町')),
                      LIST: (200, LIST, 'text/html', page('一覧'))}
        self.places = {'東京都中央区明石町': [{'name': '東京都中央区明石町', 'lat': 35.667, 'lon': 139.7777}],
                       '東京都中央区日本橋浜町３丁目': [{'name': '東京都中央区日本橋浜町三丁目', 'lat': 35.6852, 'lon': 139.7895}]}
        result = self.run_search()
        items = {i['search_result_url'] if i.get('search_result_url') else i['url']: i for i in result['items']}
        moved, town_item, listing = items[DETAIL], items[town], items[LIST]
        self.assertEqual((moved['reliability'], moved['lat']), ('ended_or_moved', None))  # A moved listing is never pinned.
        self.assertEqual(moved['url'], LIBRARY)
        self.assertEqual((town_item['reliability'], town_item['lat'], town_item['geocode_level']), ('page_checked', None, 'town'))
        self.assertEqual(town_item['checks']['radius_A']['result'], 'pass')  # 1.6 km ± 1 km is inside 3 km.
        self.assertEqual(town_item['checks']['budget']['result'], 'unknown')  # Management fee unknown, not zero.
        self.assertEqual((listing['reliability'], listing['lat']), ('search_page', None))
        self.assertEqual(self.refines, [False, True])  # Fewer than two checked listing pages: one refinement.
        self.assertEqual(result['web_search']['checked_listing_pages'], 1)

    def test_over_budget_page_is_a_labelled_mismatch_and_does_not_count_as_reliable(self):
        a, b = 'https://www.homes.co.jp/chintai/room/a/', 'https://www.homes.co.jp/chintai/room/b/'
        self.rows = [[self.row(a), self.row(b, rent=150000, fee=8000)]]
        self.pages = {a: (200, a, 'text/html', page('1LDK', '13.5万円 15,000円 1LDK 36.84㎡ 東京都中央区日本橋浜町３丁目')),
                      b: (200, b, 'text/html', page('1LDK', '15万円 1LDK'))}
        self.places = {'東京都中央区日本橋浜町３丁目': [{'name': '東京都中央区', 'lat': 35.67, 'lon': 139.77}, {'name': '東京都中央区日本橋浜町三丁目', 'lat': 35.6852, 'lon': 139.7895}]}
        result = self.run_search()
        self.assertEqual(self.refines, [False, True])  # Only one checked page without a known conflict.
        self.assertEqual((result['web_search']['checked_listing_pages'], result['web_search']['checked_without_known_conflict']), (2, 1))
        first, second = result['items']
        self.assertEqual((first['lat'], first['geocode_level']), (35.6852, 'chome'))  # Most specific candidate, not the ward.
        self.assertEqual(first['fact_sources'], {'price': 'page', 'management_fee_yen': 'page', 'layout': 'page', 'area': 'page', 'address': 'page'})
        self.assertEqual(first['checks']['budget']['result'], 'pass_known')
        self.assertEqual(first['match']['status'], 'unverified')
        self.assertEqual(second['match']['status'], 'mismatch')
        self.assertIn('budget', second['match']['mismatches'])

    def test_two_conflict_free_checked_listings_need_no_refinement(self):
        a, b = 'https://www.homes.co.jp/chintai/room/a/', 'https://www.homes.co.jp/chintai/room/b/'
        self.rows = [[self.row(a), self.row(b, rent=120000, fee=10000)]]
        self.pages = {a: (200, a, 'text/html', page('1LDK', '13.5万円')), b: (200, b, 'text/html', page('1LDK', '12万円'))}
        result = self.run_search()
        self.assertEqual(self.refines, [False])
        self.assertNotIn('refinement', result['web_search'])

    def test_failed_refinement_is_reported_truthfully(self):
        self.rows = [[], []]
        calls = []
        original = self.agent.web_search
        def flaky(query, context='', refine=False):
            calls.append(refine)
            if refine:
                raise web_search.WebSearchError('rate_limited')
            return original(query, context)
        self.agent.web_search = flaky
        result = self.run_search()
        self.assertEqual(calls, [False, True])
        self.assertEqual(result['web_search']['status'], 'empty')
        self.assertEqual((result['web_search']['refinement']['status'], result['web_search']['refinement']['code']), ('error', 'rate_limited'))
        self.assertEqual(result['items'], [])

    def test_removed_listing_is_ended_and_never_pinned(self):
        url = 'https://realestate.yahoo.co.jp/rent/detail/000001280258befebec72221bdce43e6e3ac1e40b307/'
        self.rows = [[self.row(url, address='東京都中央区月島4')], []]
        self.pages = {url: HTTPError(url, 404, 'Not Found', {}, None)}
        self.places = {'東京都中央区月島4': [{'name': '東京都中央区月島四丁目', 'lat': 35.6604, 'lon': 139.7827}]}
        verify = self.agent.page_verifier
        def raising(u, extracted):
            return page_verify.verify(u, extracted, (), fetcher=lambda target: (_ for _ in ()).throw(self.pages[target]))
        self.agent.page_verifier = raising
        item = self.run_search()['items'][0]
        self.assertEqual((item['reliability'], item['listing_status'], item['lat']), ('ended_or_moved', 'ended_or_moved', None))
        self.assertIn('HTTP 404', ' '.join(item['limitations']))

    def test_consulted_detail_pages_are_checked_and_added_with_page_facts_only(self):
        live = 'https://www.homes.co.jp/chintai/b-1331530543050/'
        ended = 'https://www.homes.co.jp/chintai/b-2222222222/'
        listing = 'https://www.homes.co.jp/chintai/theme/11158/tokyo/chuo-city/list/'
        restricted = 'https://www.chintai.net/detail/bk-C010090830000012042692190001/'
        self.rows = [[self.row(LIST, 'listing_search_page', None, None, None)], []]
        original = self.agent.web_search
        def with_sources(query, context='', refine=False):
            result = original(query, context, refine)
            result['source_urls'] = [LIST, live, ended, listing, restricted]
            return result
        self.agent.web_search = with_sources
        ld = {'address': {'addressRegion': '東京都', 'addressLocality': '中央区', 'streetAddress': '日本橋人形町２丁目33-3'}}
        self.pages = {LIST: (200, LIST, 'text/html', page('一覧')),
                      live: (200, live, 'text/html', page('AZ日本橋人形町 [1LDK/2階/31.86㎡]', '', live, ld)),
                      ended: (200, ended, 'text/html', page('物件', 'この物件の掲載は終了しました'))}
        self.places = {'東京都中央区日本橋人形町2丁目33-3': [{'name': '東京都中央区日本橋人形町二丁目', 'lat': 35.6858, 'lon': 139.7826}]}
        result = self.run_search()
        harvested = [i for i in result['items'] if i.get('harvested')]
        self.assertEqual([i['url'] for i in harvested], [live])  # Ended, list and non-reuse pages are not added.
        item = harvested[0]
        self.assertEqual((item['reliability'], item['extracted']['layout'], item['extracted']['area'], item['extracted']['price']), ('page_checked', '1LDK', 31.86, None))
        self.assertEqual(item['fact_sources'], {'layout': 'page', 'area': 'page', 'address': 'page'})
        self.assertEqual(item['checks']['budget']['result'], 'unknown')  # Rent was not read, so it is not assumed.
        title = '京葉線八丁堀駅14階建築15年東京都中央区新川1(18.2万円/1LDK(洋5.8LDK8.2)/35.68m2)|賃貸物件'
        self.assertEqual(page_verify.title_facts(page_verify.normalize(title)), {'price': 18.2, 'address': '東京都中央区新川1', 'layout': '1LDK', 'area': 35.68})

    def test_refinement_uses_documented_portal_filter(self):
        body = web_search.build_request('m', 'q', 'c', web_search.RENTAL_PORTALS)
        self.assertEqual(body['tools'][0]['filters']['allowed_domains'], list(web_search.RENTAL_PORTALS))
        self.assertNotIn('suumo.jp', web_search.RENTAL_PORTALS)
        self.assertFalse(set(web_search.NO_REUSE_HOSTS) & set(web_search.RENTAL_PORTALS))
        self.assertNotIn('filters', web_search.build_request('m', 'q')['tools'][0])



class TierTest(unittest.TestCase):
    def home(self, **overrides):
        item = {'discovery_only': True, 'web_result': True, 'page_kind': 'listing_detail', 'reliability': 'page_checked',
                'title': 'AZ日本橋人形町', 'url': 'https://www.homes.co.jp/chintai/b-1/', 'source': 'www.homes.co.jp', 'id': 'listing_x',
                'lat': 35.6858, 'lon': 139.7826, 'geocode_level': 'block',
                'extracted': {'price': 14.0, 'management_fee_yen': 10000, 'layout': '1LDK', 'area': 31.86, 'address': '東京都中央区日本橋人形町2丁目33-3'},
                'fact_sources': {'price': 'page', 'management_fee_yen': 'page', 'layout': 'page', 'area': 'page', 'address': 'page'},
                'limitations': ['availability unknown']}
        item.update(overrides)
        return item

    def test_exact_requires_every_active_criterion_on_the_page(self):
        self.assertEqual(qualify(self.home(), CRITERIA), ('exact', [], []))
        tier, differences, unconfirmed = qualify(self.home(fact_sources={'price': 'page', 'layout': 'page', 'address': 'page'}), CRITERIA)
        self.assertEqual((tier, differences, unconfirmed), ('alternative', [], ['management_fee']))
        self.assertEqual(qualify(self.home(), {**CRITERIA, 'pets': True})[0], 'alternative')  # Never assumed.

    def test_alternatives_state_specific_known_differences(self):
        over = qualify(self.home(extracted={**self.home()['extracted'], 'price': 15.2}), CRITERIA)
        self.assertEqual(over, ('alternative', [{'criterion': 'budget', 'over_yen': 12000, 'at_least': False}], []))
        far = qualify(self.home(lat=35.6812 + 4.5 / 111, lon=139.7671, geocode_level='block'), CRITERIA)
        self.assertEqual(far[1], [{'criterion': 'radius', 'origin': 'A', 'beyond_km': 1.5}])
        layout = qualify(self.home(extracted={**self.home()['extracted'], 'layout': '2LDK'}), CRITERIA)
        self.assertEqual(layout[1], [{'criterion': 'layout', 'stated': '2LDK'}])

    def test_weak_or_ended_results_are_never_homes(self):
        self.assertEqual(qualify(self.home(listing_status='ended_or_moved', reliability='ended_or_moved'), CRITERIA)[0], 'excluded')
        for weak in ({'reliability': 'page_not_checked'}, {'page_kind': 'listing_search_page'}, {'geocode_level': 'town', 'lat': None},
                     {'fact_sources': {'price': 'search_summary', 'address': 'page'}}, {'fact_sources': {'price': 'page'}}):
            with self.subTest(weak=weak):
                self.assertEqual(qualify(self.home(**weak), CRITERIA)[0], 'browse')

    def test_compact_output_carries_differences_but_no_disclaimer_dumps(self):
        item = self.home(extracted={**self.home()['extracted'], 'price': 15.2})
        item['tier'], item['differences'], item['unconfirmed'] = qualify(item, CRITERIA)
        item['checks'] = discovery_checks(item, CRITERIA)
        out = compact(item)
        self.assertEqual(out['monthly_total_yen'], 162000)
        self.assertEqual(out['differences'], [{'criterion': 'budget', 'over_yen': 12000, 'at_least': False}])
        self.assertNotIn('limitations', out)
        self.assertNotIn('unknown', json.dumps(out))

    def test_instructions_give_concise_tiered_replies_without_internal_dumps(self):
        self.assertIn("I couldn't find an exact match. If you're flexible on", INSTRUCTIONS)
        self.assertIn('条件に合う物件を確認できませんでした', INSTRUCTIONS)
        self.assertIn('Never call an alternative a match', INSTRUCTIONS)
        self.assertNotIn('say this app has no verified inventory', INSTRUCTIONS)
        self.assertNotIn('Do not discard all leads', INSTRUCTIONS)


class OpenedPageTest(unittest.TestCase):
    def test_page_opened_by_the_search_counts_when_the_app_is_blocked_but_snippets_do_not(self):
        opened = {'url': HOMES, 'page_kind': 'listing_detail', 'opened_by_search': True, 'extracted': {'price': 14.0, 'address': '東京都中央区明石町'}}
        apply_verification(opened, {'status': 'blocked', 'http_status': 403})
        self.assertEqual((opened['reliability'], opened['fact_sources']), ('page_checked', {'price': 'opened_page', 'address': 'opened_page'}))
        snippet = {'url': HOMES, 'page_kind': 'listing_detail', 'opened_by_search': False, 'extracted': {'price': 14.0}}
        apply_verification(snippet, {'status': 'blocked', 'http_status': 403})
        self.assertEqual(snippet['reliability'], 'page_not_checked')
        gone = {'url': HOMES, 'page_kind': 'listing_detail', 'opened_by_search': True, 'extracted': {'price': 14.0}}
        apply_verification(gone, {'status': 'gone', 'http_status': 404})
        self.assertEqual(gone['reliability'], 'ended_or_moved')

    def test_opened_pages_are_marked_from_documented_open_page_actions(self):
        body = {'status': 'completed', 'output': [
            {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'search', 'query': 'q', 'sources': [{'type': 'url', 'url': HOMES}, {'type': 'url', 'url': LIST}]}},
            {'type': 'web_search_call', 'status': 'completed', 'action': {'type': 'open_page', 'url': HOMES}},
            {'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps({'summary': '', 'results': [
                {'url': HOMES, 'title': 't', 'kind': 'listing_detail', 'snippet': '', 'address': None, 'rent_yen': None, 'management_fee_yen': None, 'layout': None, 'area_m2': None, 'station_access': None},
                {'url': LIST, 'title': 't', 'kind': 'listing_detail', 'snippet': '', 'address': None, 'rent_yen': None, 'management_fee_yen': None, 'layout': None, 'area_m2': None, 'station_access': None}]})}]}]}
        records = web_search.parse(body, 'q')['results']
        self.assertEqual([r['opened_by_search'] for r in records], [True, False])

    def test_title_facts_fill_values_the_summary_missed(self):
        item = {'url': DETAIL, 'page_kind': 'listing_detail', 'extracted': {'layout': '1LDK', 'price': None, 'address': None}}
        apply_verification(item, {'status': 'ok', 'final_url': DETAIL, 'facts': {'layout': 'page'},
                                  'title_facts': {'price': 13.0, 'address': '東京都中央区新川1', 'layout': '2LDK'}})
        self.assertEqual((item['extracted']['price'], item['extracted']['address'], item['extracted']['layout']), (13.0, '東京都中央区新川1', '1LDK'))
        self.assertEqual(item['fact_sources'], {'layout': 'page', 'price': 'page', 'address': 'page'})


class TierSummaryTest(AgentAccuracyTest):
    def test_summary_separates_exact_alternatives_and_browsing_and_pins_only_homes(self):
        exact, over, ended = ('https://www.homes.co.jp/chintai/room/e/', 'https://www.homes.co.jp/chintai/room/o/', 'https://www.homes.co.jp/chintai/room/x/')
        self.rows = [[self.row(exact, rent=130000, fee=10000), self.row(over, rent=155000, fee=10000), self.row(ended), self.row(LIST, 'listing_search_page', None, None, None)]]
        body = lambda rent: page('1LDK', f'{rent}万円 管理費 10,000円 1LDK 36.84㎡ 東京都中央区日本橋浜町３丁目')
        self.pages = {exact: (200, exact, 'text/html', body(13)), over: (200, over, 'text/html', body(15.5)),
                      ended: (200, ended, 'text/html', page('物件', 'この物件の掲載は終了しました')), LIST: (200, LIST, 'text/html', page('一覧'))}
        self.places = {'東京都中央区日本橋浜町３丁目': [{'name': '東京都中央区日本橋浜町三丁目', 'lat': 35.6852, 'lon': 139.7895}]}
        result = self.run_search()
        summary = result['summary']
        self.assertEqual([h['url'] for h in summary['exact']], [exact])
        self.assertEqual([h['url'] for h in summary['alternatives']], [over])
        self.assertEqual(summary['alternatives'][0]['differences'], [{'criterion': 'budget', 'over_yen': 15000, 'at_least': False}])
        self.assertEqual([b['url'] for b in summary['browse']], [LIST])  # The ended page is not even a browsing link.
        tiers = {i['url']: (i['tier'], i['lat'] is not None) for i in result['items']}
        self.assertEqual(tiers[exact], ('exact', True)); self.assertEqual(tiers[over], ('alternative', True))
        self.assertEqual(tiers[ended], ('excluded', False)); self.assertEqual(tiers[LIST], ('browse', False))
        self.assertEqual(summary['map'], 'queued')

    def test_no_exact_match_reports_what_to_be_flexible_on(self):
        far = 'https://www.homes.co.jp/chintai/room/f/'
        self.rows = [[self.row(far, rent=120000, fee=5000, address='東京都江東区豊洲6丁目')], []]
        self.pages = {far: (200, far, 'text/html', page('1LDK', '12万円 管理費 5,000円 1LDK 東京都江東区豊洲6丁目'))}
        self.places = {'東京都江東区豊洲6丁目': [{'name': '東京都江東区豊洲六丁目', 'lat': 35.6455, 'lon': 139.7845}]}
        self.store.mutate(self.token, lambda s: s['criteria'].update(radius_a_km=2))
        summary = self.run_search()['summary']
        self.assertEqual(summary['exact'], [])
        self.assertEqual(summary['flexible_on'], ['radius'])
        self.assertEqual(summary['alternatives'][0]['differences'][0]['criterion'], 'radius')

if __name__ == '__main__':
    unittest.main()
