"""No network access required."""
import json
import unittest
from unittest.mock import patch, MagicMock
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

if __name__=='__main__': unittest.main()
