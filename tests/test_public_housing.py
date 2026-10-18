import csv
import io
import json
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import public_housing as public


TOKYO_CSV = '区市名,団地名,主な所在地,面積,間取,建設年度,管理戸数\r\n千代田区,四番町第２アパート,千代田区四番町6,34-63,1DK-3DK,1998,83\r\n,,,,,,\r\n'


def municipal_csv(status='受付中', contract='売買', notes='', price='3200000'):
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=['ID', '所在地_連結表記', '契約形態', '受付状況', '金額', 'URL', '備考'])
    writer.writeheader()
    writer.writerow({'ID': 'TEST-ONLY', '所在地_連結表記': '石川県河北郡津幡町字田屋地内', '契約形態': contract,
                     '受付状況': status, '金額': price, 'URL': 'javascript:alert(1)', '備考': notes})
    return output.getvalue().encode('utf-8-sig')


class PublicHousingTest(unittest.TestCase):
    def setUp(self):
        public._cache.clear()
        public._geocodes.clear()

    def test_cp932_blank_rows_and_reference_semantics(self):
        items = public.normalize_tokyo(TOKYO_CSV.encode('cp932'), '2025-03-31', public.now())
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item['name'], '四番町第２アパート')
        self.assertEqual(item['address'], '東京都千代田区四番町6')
        for field in ('price_yen', 'layout', 'area', 'last_verified_at', 'vacancy_public'):
            self.assertIsNone(item[field])
        self.assertEqual(item['availability_status'], 'unknown')
        self.assertNotEqual(item['data_as_of'], item['fetched_at'][:10])

    def test_missing_required_columns_fail_instead_of_empty_success(self):
        for raw in (b'not,a,csv\nwrong,header,here', b''):
            with self.assertRaises(ValueError):
                public.normalize_tokyo(raw, '2025-03-31', public.now())

    def test_reference_date_is_not_page_update_date(self):
        self.assertEqual(public.reiwa_date('最終更新日：令和8（2026）年3月31日 <strong>令和７年３月31日現在</strong>'), '2025-03-31')
        for page in ('更新日 2026-01-01', '令和99年3月31日現在', '令和7年2月30日現在'):
            with self.assertRaises(ValueError):
                public.reiwa_date(page)

    def test_current_official_download_link_is_discovered_not_pinned_to_old_file(self):
        self.assertEqual(public.tokyo_csv_url('<a href="/documents/d/juutakuseisaku/next_csv">全区 オープンデータCSV</a>'), public.TOKYO + '/documents/d/juutakuseisaku/next_csv')
        with self.assertRaises(ValueError):
            public.tokyo_csv_url('<a href="https://example.com/not-approved">オープンデータCSV</a>')

    def test_dedup_uses_identity_not_address_or_complex_name_alone(self):
        data = TOKYO_CSV + '千代田区,四番町第２アパート,千代田区四番町6,34-63,1DK-3DK,1998,83\n'
        self.assertEqual(len(public.normalize_tokyo(data.encode('cp932'), '2025-03-31', public.now())), 1)
        data += '千代田区,四番町第２アパート,千代田区四番町6,40,2DK,2002,20\n'
        self.assertEqual(len(public.normalize_tokyo(data.encode('cp932'), '2025-03-31', public.now())), 2)

    def test_municipal_status_prices_and_safe_official_link(self):
        items, ended = public.normalize_tsubata(municipal_csv(), '2026-09-14', public.now())
        self.assertEqual(ended, 0)
        self.assertEqual(items[0]['price_yen'], 3200000)
        self.assertEqual(items[0]['availability_status'], 'reported_open')
        self.assertIsNone(items[0]['last_verified_at'])
        self.assertIsNone(items[0]['data_as_of'])
        self.assertEqual(items[0]['source_updated_at'], '2026-09-14')
        self.assertEqual(items[0]['official_url'], public.TSUBATA_PAGE)
        self.assertEqual(public.normalize_tsubata(municipal_csv(status='成約済み'), '2026-09-14', public.now()), ([], 1))
        items, _ = public.normalize_tsubata(municipal_csv(contract='賃貸', notes='金額は月額', price='50000'), '2026-09-14', public.now())
        self.assertEqual(items[0]['price_yen'], 50000)
        self.assertIsNone(items[0]['management_fee_yen'])
        items, _ = public.normalize_tsubata(municipal_csv(contract='賃貸', price='50000'), '2026-09-14', public.now())
        self.assertIsNone(items[0]['price_yen'])

    def test_license_or_resource_change_fails_closed(self):
        metadata = {'result': {'license_id': 'all-rights-reserved', 'resources': []}}
        with patch.object(public, 'read_url', return_value=json.dumps(metadata).encode()):
            with self.assertRaisesRegex(ValueError, 'license changed'):
                public.load('tsubata')
        metadata['result'].update(license_id='cc-by', resources=[{'id': public.RESOURCE, 'last_modified': '2026-09-14', 'url': 'https://example.com/not-approved'}])
        with patch.object(public, 'read_url', return_value=json.dumps(metadata).encode()):
            with self.assertRaisesRegex(ValueError, 'URL changed'):
                public.load('tsubata')

    def test_geocoding_variants_and_cache(self):
        item = public.normalize_tokyo(TOKYO_CSV.encode('cp932'), '2025-03-31', public.now())[0]
        result = [{'geometry': {'coordinates': [139.737656, 35.68959]}, 'properties': {'title': '東京都千代田区四番町６番地'}}]
        with patch.object(public, 'load', return_value={'records': [item]}), patch.object(public, 'read_url', return_value=json.dumps(result).encode()) as fetch:
            first = public.geocode('tokyo', item['id'])
            self.assertEqual(first['geo_precision'], 'approximate')
            self.assertEqual(first['lat'], 35.68959)
            self.assertEqual(public.geocode('tokyo', item['id']), first)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(public.address_key('石川県河北郡津幡町字田屋地内'), public.address_key('石川県津幡町田屋'))
        self.assertEqual(public.address_key('東京都千代田区飯田橋2-1'), public.address_key('東京都千代田区飯田橋二丁目１番'))

    def test_wrong_area_geocode_does_not_make_a_pin(self):
        item = public.normalize_tokyo(TOKYO_CSV.encode('cp932'), '2025-03-31', public.now())[0]
        result = [{'geometry': {'coordinates': [135, 34]}, 'properties': {'title': '大阪府大阪市'}}]
        with patch.object(public, 'load', return_value={'records': [item]}), patch.object(public, 'read_url', return_value=json.dumps(result).encode()), patch.object(public.time, 'sleep'):
            position = public.geocode('tokyo', item['id'])
        self.assertEqual(position['geo_precision'], 'unknown')
        self.assertIsNone(position['lat'])

    def test_pagination_preserves_exact_feed_coordinates(self):
        items = [public.record('tokyo', str(index), 'TEST ONLY', '東京都TEST', '2025-03-31', public.now(), public.TOKYO_PAGE, 'CC BY', public.TOKYO_LICENSE) for index in range(23)]
        items[0].update(lat=35, lon=139, geo_precision='exact')
        public._geocodes['東京都TEST'] = (time.monotonic(), {'lat': 36, 'lon': 140, 'geo_precision': 'approximate'})
        with patch.object(public, 'load', return_value={'records': items, 'municipalities': [], 'ended_excluded': 0}):
            first = public.search('tokyo', 'TEST')
            self.assertEqual(first['records'][0]['lat'], 35)
            self.assertEqual(len(first['records']), 20)
            self.assertEqual(first['next_offset'], 20)
            self.assertEqual(len(public.search('tokyo', 'TEST', 20)['records']), 3)
            self.assertEqual(public.search('tokyo', 'NO MATCH')['total'], 0)
        with self.assertRaises(ValueError):
            public.load('https://example.com/arbitrary-fetch')

    def test_permitted_feed_separates_vacancy_and_fresh_recruitment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'public.json'
            vacancy = {'id': 'TEST-VACANCY', 'name': 'TEST ONLY', 'address': 'TEST ONLY', 'layer': 'vacancy',
                       'source_url': 'https://example.com/test-only', 'data_as_of': '2026-03-31', 'vacancy_public': 7, 'vacancy_reserved': 1}
            offer = {**vacancy, 'id': 'TEST-OFFER', 'layer': 'recruitment', 'availability_status': 'confirmed', 'last_verified_at': datetime.now(timezone.utc).isoformat()}
            old = {**offer, 'id': 'TEST-OLD', 'last_verified_at': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
            feed = {'permitted': True, 'license': 'TEST ONLY permission', 'license_url': 'https://example.com/permission', 'records': [vacancy, offer, old]}
            path.write_text(json.dumps(feed))
            items = public.permitted_feed(path)
            self.assertEqual(len(items), 2)
            self.assertEqual(items[0]['vacancy_public'], 7)
            self.assertEqual(items[0]['availability_status'], 'unknown')
            self.assertEqual(items[1]['availability_status'], 'confirmed')
            feed['permitted'] = False
            path.write_text(json.dumps(feed))
            with self.assertRaises(ValueError):
                public.permitted_feed(path)
