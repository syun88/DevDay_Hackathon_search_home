"""No network access required."""
import json
import time
import unittest
from unittest.mock import patch, MagicMock
from http.server import ThreadingHTTPServer
from threading import Thread
from urllib.request import urlopen
from urllib.error import HTTPError
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import server
from filter_core import haversine_km, filter_listings


class SearchServerTest(unittest.TestCase):
    def test_domain_whitelist(self):
        self.assertEqual(server.portal_host('https://suumo.jp/chintai/xx'),'suumo.jp')
        for url in ['http://suumo.jp/a','https://suumo.jp.evil.com/',
                    'https://evil.com/?next=suumo.jp','https://suumo.jp:444/xx',
                    'javascript:alert(1)','https://evil@suumo.jp.evil.com/x']:
            self.assertIsNone(server.portal_host(url),url)

    def test_bing_rss_normalization(self):
        xml='<rss><channel><item><title>物件 1K 家賃7万円 25㎡</title><link>https://suumo.jp/a</link><description>東京都渋谷区</description></item><item><title>spam</title><link>https://evil.example/</link></item></channel></rss>'
        items=server.normalize_results(server.parse_bing_rss(xml))
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['extracted']['price'],7)
        self.assertEqual(items[0]['extracted']['layout'],'1K')
        self.assertEqual(items[0]['availability_status'],'unknown')
        self.assertEqual(items[0]['geo_precision'],'unknown')
        self.assertTrue(items[0]['retrieved_at'])

    def test_duplicate_and_unknown_fields(self):
        items=[{'url':'https://www.homes.co.jp/x/?track=1','title':'家賃8万円'},
               {'url':'https://www.homes.co.jp/x/?track=2','title':'家賃8万円'}]
        self.assertEqual(len(server.normalize_results(items)),1)
        self.assertIsNone(server.parse_fields('間取り未定', '', 'rent')['price'])

    def test_no_network_for_unknown_provider(self):
        with patch.object(server,'bing_rss',return_value=[]):
            result=server.fetch_listings('東京都千代田区','rent',{},'bing')
        self.assertEqual(result['results'],[])
        self.assertIn('note',result)

    def test_decisions_api_contract(self):
        payload={'answers':[{'type':'choice','name':'page_kind','choice':'list','confidence':0.7}]}
        resp=MagicMock();resp.read.return_value=json.dumps(payload).encode()
        cm=MagicMock();cm.__enter__.return_value=resp
        with patch.object(server,'OPENAI_KEY','test-only'),patch.object(server,'urlopen',return_value=cm) as mock_open:
            result=server.decision_classify('一覧','物件が複数あります')
            request=mock_open.call_args.args[0]
        self.assertEqual(result['choice'],'list')
        self.assertFalse(result['is_verified'])
        self.assertIn(b'gpt-6-luna',request.data)
        self.assertEqual(request.get_method(),'POST')


class FilterTest(unittest.TestCase):
    def test_origin_and_budget(self):
        unit={'lat':35.681236,'lon':139.767125,'rent_yen':70000,'management_fee_yen':5000,'mandatory_fee_yen':0}
        origins=[(35.681236,139.767125,2),(35.681236,139.767125,1)]
        self.assertEqual(len(filter_listings([unit],origins,75000,'all')),1)
        self.assertEqual(len(filter_listings([unit],origins,74999,'any')),0)
        self.assertEqual(filter_listings([{**unit,'management_fee_yen':None}],origins,80000),[])
        self.assertEqual(filter_listings([{**unit,'mandatory_fee_yen':None}],origins,80000),[])
        self.assertEqual(filter_listings([{**unit,'mandatory_fee_yen':6000}],origins,80000),[])
        for value in [float('nan'), float('inf'), -1, True, '5000']:
            self.assertEqual(filter_listings([{**unit,'management_fee_yen':value}],origins,80000),[])
        self.assertLess(haversine_km(35.681236,139.767125,35.69,139.77),2)
        self.assertRaises(ValueError,filter_listings,[unit],origins,80000,'invalid')


class SuumoHttpTest(unittest.TestCase):
    def setUp(self):
        permission = patch.object(server, 'SUUMO_REUSE_PERMITTED', True)
        permission.start()
        self.addCleanup(permission.stop)
        server._suumo_cache.clear()
        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.worker = Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.base = f'http://127.0.0.1:{self.httpd.server_port}/api/suumo-search?'

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join()
        server._suumo_cache.clear()

    def get(self, query):
        try:
            with urlopen(self.base + query, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_cache_uses_every_search_condition_without_rounding(self):
        query = 'lat=35.681236&lon=139.767125&radius_km=1'
        def result(lat, lon, radius, rent, layout):
            return {'filters': {'max_rent_man': rent, 'layout': layout},
                    'query': [lat, lon, radius], 'results': []}
        with patch.object(server, 'search_suumo', side_effect=result) as search:
            self.assertFalse(self.get(query)[1]['cached'])
            self.assertTrue(self.get(query)[1]['cached'])
            self.assertFalse(self.get(query + '&max_rent_man=15')[1]['cached'])
            self.assertFalse(self.get(query + '&layout=1K')[1]['cached'])
            self.assertFalse(self.get(query + '&max_rent_man=15&layout=1K')[1]['cached'])
            self.assertFalse(self.get(query.replace('35.681236', '35.681237'))[1]['cached'])
            self.assertFalse(self.get(query.replace('radius_km=1', 'radius_km=1.001'))[1]['cached'])
        self.assertEqual(search.call_count, 6)

    def test_unpermitted_suumo_never_contacts_upstream_or_serves_cached_data(self):
        server._suumo_cache[(35.681236, 139.767125, 1, None, None)] = (time.monotonic(), {'results': ['TEST ONLY cached']})
        with patch.object(server, 'SUUMO_REUSE_PERMITTED', False), patch.object(server, 'search_suumo') as search:
            status, body = self.get('lat=35.681236&lon=139.767125&radius_km=1')
        self.assertEqual(status, 403)
        self.assertNotIn('results', body)
        search.assert_not_called()

    def test_invalid_input_and_upstream_failure_do_not_search_or_cache(self):
        query = 'lat=35.681236&lon=139.767125&radius_km=1'
        invalid = [query + '&max_rent_man=10.5', query + '&layout=bad',
                   query + '&radius_km=2', query.replace('lat=35.681236', 'lat=nan'),
                   query.replace('radius_km=1', 'radius_km=3'),
                   query + '&unexpected=1', query + '&layout=1K&layout=1R']
        with patch.object(server, 'search_suumo', side_effect=server.SuumoError('transport', '通信失敗')) as search:
            for request in invalid:
                self.assertEqual(self.get(request)[0], 400, request)
            self.assertEqual(search.call_count, 0)
            self.assertEqual(self.get(query)[0], 502)
            self.assertEqual(self.get(query)[0], 502)
            self.assertEqual(search.call_count, 2)
            self.assertEqual(server._suumo_cache, {})

if __name__=='__main__': unittest.main()


class AgentDiscoveryTest(unittest.TestCase):
    def setUp(self):
        permission = patch.object(server, 'SUUMO_REUSE_PERMITTED', True)
        permission.start()
        self.addCleanup(permission.stop)

    def test_chat_discovery_obeys_the_same_permission_gate_as_manual_search(self):
        with patch.object(server, 'SUUMO_REUSE_PERMITTED', False), patch.object(server, 'FEED_PATH', ''), patch.object(server, 'FEED_URL', ''), patch.object(server, 'search_suumo') as search, patch.object(server, 'fetch_listings', return_value={'results': []}):
            result = server.agent_listings('Tokyo', 'rent', self.details())
        search.assert_not_called()
        self.assertEqual(result['leads'], [])
        self.assertEqual(len(result['source_search_links']), 3)

    def details(self, budget=300000):
        return {'origin': {'name':'東京駅', 'lat':35.681236, 'lon':139.767125},
                'radius_km':3, 'layout':'', 'keywords':'', 'max_price_yen':budget}

    def raw(self, rent=165000, management=10000):
        return {'fetched_at':'2026-10-18T00:00:00+00:00', 'results':[
            {'source_id':'100123456789', 'source_url':'https://suumo.jp/chintai/bc_100123456789/',
             'rent_jpy':rent, 'management_fee_jpy':management,
             'known_monthly_subtotal_jpy':rent+management if management is not None else None,
             'area_sqm':35, 'layout':'1LDK', 'address':'東京都千代田区'}]}

    def test_no_feed_returns_real_source_leads_without_claiming_verified_inventory(self):
        with patch.object(server,'FEED_PATH',''),patch.object(server,'FEED_URL',''),patch.object(server,'search_suumo',return_value=self.raw()) as search,patch.object(server,'fetch_listings') as index:
            result=server.agent_listings('Tokyo','rent',self.details())
        self.assertEqual(result['results'], [])
        self.assertEqual(len(result['leads']),1)
        self.assertTrue(result['leads'][0]['discovery_only'])
        self.assertIsNone(result['leads'][0]['last_verified_at'])
        self.assertIsNone(result['leads'][0]['extracted']['mandatory_fee_yen'])
        self.assertEqual(result['inventory_status'],'no_verified_feed_configured')
        self.assertFalse(result['coverage']['citywide'])
        self.assertEqual(result['coverage']['requested_radius_km'],3)
        self.assertEqual(result['coverage']['query_radius_km'],2)
        self.assertEqual(search.call_args.args[3],30)
        index.assert_not_called()

    def test_known_subtotal_over_budget_is_not_offered_as_a_lead(self):
        with patch.object(server,'FEED_PATH',''),patch.object(server,'FEED_URL',''),patch.object(server,'search_suumo',return_value=self.raw(290000,20000)),patch.object(server,'fetch_listings',return_value={'results':[]}):
            result=server.agent_listings('Tokyo','rent',self.details())
        self.assertEqual(result['leads'],[])
        self.assertEqual(len(result['source_search_links']),3)

    def test_missing_management_fee_does_not_hide_known_rent_or_invent_total(self):
        with patch.object(server,'FEED_PATH',''),patch.object(server,'FEED_URL',''),patch.object(server,'search_suumo',return_value=self.raw(165000,None)):
            result=server.agent_listings('Tokyo','rent',self.details())
        self.assertEqual(result['leads'][0]['extracted']['price'],16.5)
        self.assertIsNone(result['leads'][0]['known_monthly_subtotal_yen'])

    def test_source_failure_and_empty_index_still_offer_source_searches(self):
        with patch.object(server,'FEED_PATH',''),patch.object(server,'FEED_URL',''),patch.object(server,'search_suumo',side_effect=server.SuumoError('transport','unavailable')),patch.object(server,'fetch_listings',return_value={'results':[]}):
            result=server.agent_listings('Tokyo','rent',self.details())
        self.assertEqual(result['leads'],[])
        self.assertIn('transport',result['errors'][0])
        self.assertEqual(len(result['source_search_links']),3)

    def test_outside_tokyo_never_moves_search_to_tokyo(self):
        details=self.details();details['origin'].update(lat=34.69,lon=135.50)
        with patch.object(server,'FEED_PATH',''),patch.object(server,'FEED_URL',''),patch.object(server,'search_suumo') as search,patch.object(server,'fetch_listings',return_value={'results':[]}):
            result=server.agent_listings('Osaka','rent',details)
        search.assert_not_called()
        self.assertTrue(result['errors'])
        self.assertTrue(all('Osaka' in link['url'] for link in result['source_search_links']))

    def test_permitted_feed_still_takes_priority(self):
        with patch.object(server,'FEED_PATH','configured'),patch.object(server,'load_feed',return_value={'results':[{'kind':'rent'}]}),patch.object(server,'search_suumo') as search:
            result=server.agent_listings('Tokyo','rent',self.details())
        self.assertEqual(result['inventory_status'],'configured_verified_feed')
        search.assert_not_called()
