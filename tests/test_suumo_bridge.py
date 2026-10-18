"""No network access required."""
import json
import unittest
from unittest.mock import patch
from subprocess import TimeoutExpired
from types import SimpleNamespace
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suumo_bridge as bridge


class SuumoBridgeTest(unittest.TestCase):
    def test_money_normalization_and_unknown_costs(self):
        row = bridge.normalize_item({
            'bukken_cd': '123456789', 'link': 'https://suumo.jp/chintai/bc_123456789/',
            'jusho': '東京都新宿区', 'kakaku': '5.4万円', 'kanrihi': '7000円',
            'madori': '1K', 'menseki': '22.91平米', 'lt': 35.69, 'lg': 139.70,
        }, '2026-10-18T00:00:00+00:00')
        self.assertEqual((row['rent_jpy'], row['management_fee_jpy'], row['known_monthly_subtotal_jpy']), (54000, 7000, 61000))
        self.assertEqual(row['availability_status'], 'unknown')
        self.assertIsNone(row['source_updated_at'])
        self.assertFalse(row['within_radius_verified'])
        self.assertNotIn('lat', row)
        self.assertNotIn('lon', row)
        self.assertEqual(row['other_monthly_costs'], 'unknown')

    def test_out_of_region_excluded_not_pinned(self):
        def item(id, address):
            return {'bukken_cd': id, 'link': f'https://suumo.jp/chintai/bc_{id}/', 'jusho': address,
                    'kakaku': '5万円', 'kanrihi': '-', 'madori': '1K', 'menseki': '20平米'}
        body = json.dumps({'smatch': {'resultset': {'totalhits': 2, 'item': [item('123456789', '京都府京都市'), item('987654321', '東京都新宿区')]}}}).encode()
        result = bridge.parse_search_response(body, 'now')
        self.assertEqual(result['excluded_out_of_region'], 1)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['address'], '東京都新宿区')
        self.assertIsNone(result['results'][0]['known_monthly_subtotal_jpy'])

    def test_http_200_error_json_is_failure(self):
        body = json.dumps({'smatch': {'errors': {'error': [{'message': '認証エラーです。'}]}}}).encode()
        with self.assertRaises(bridge.SuumoError) as raised:
            bridge.parse_search_response(body, 'now')
        self.assertEqual(raised.exception.code, 'authentication')

    def test_rent_and_layout_filters_exclude_mismatch_and_unknown(self):
        def item(id, rent, layout):
            return {'bukken_cd': id, 'link': f'https://suumo.jp/chintai/bc_{id}/',
                    'jusho': '東京都千代田区', 'kakaku': rent, 'kanrihi': '0円',
                    'madori': layout, 'menseki': '25平米'}
        body = json.dumps({'smatch': {'resultset': {'totalhits': 5, 'item': [
            item('100000001', '15万円', '1K'), item('100000002', '16万円', '1K'),
            item('100000003', '14万円', '1LDK'), item('100000004', '-', '1K'),
            item('100000005', '12万円', ''),
        ]}}}).encode()
        result = bridge.parse_search_response(body, 'now', 15, '1K')
        self.assertEqual([x['source_id'] for x in result['results']], ['100000001'])
        self.assertEqual(result['excluded_filter_mismatch_or_unknown'], 4)

    def test_filter_values_follow_public_map_controls(self):
        self.assertEqual(bridge.validate_filters(15, '1K'), (15, '1K'))
        for price, layout in [(10.5, '1K'), (0, None), (101, None), (None, 'studio')]:
            with self.assertRaises(ValueError): bridge.validate_filters(price, layout)

    def test_zero_hits_distinct_from_broken_response_and_one_room_alias(self):
        empty = json.dumps({'smatch': {'resultset': {'totalhits': 0}}}).encode()
        self.assertEqual(bridge.parse_search_response(empty, 'now', 15, '1R')['results'], [])
        broken = json.dumps({'smatch': {'resultset': {'totalhits': 10}}}).encode()
        with self.assertRaises(bridge.SuumoError): bridge.parse_search_response(broken, 'now')
        room = {'bukken_cd': '100000001', 'link': 'https://suumo.jp/chintai/bc_100000001/',
                'jusho': '東京都中央区', 'kakaku': '13万円', 'madori': 'ワンルーム'}
        body = json.dumps({'smatch': {'resultset': {'totalhits': 1, 'item': [room]}}}).encode()
        self.assertEqual(len(bridge.parse_search_response(body, 'now', 15, '1R')['results']), 1)

    def test_validation_and_bounded_normal_sequence(self):
        for values in [(float('nan'), 139.7, 1), (35.69, 139.7, 3), (34.6, 139.7, 1)]:
            with self.assertRaises(ValueError): bridge.validate_search(*values)
        page = b"<script>suumo.ApiParam={'bkApi':{'UID':'public123','STMP':'1234567890','ATT':'abcdef0123456789abcdef0123456789abcdef01'}}</script>"
        body = json.dumps({'smatch': {'resultset': {'totalhits': 0, 'item': []}}}).encode()
        with patch.object(bridge, '_curl_get', side_effect=[page, body]) as fetch:
            result = bridge.search(35.69, 139.70, 1)
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(fetch.call_args_list[0].args[0], bridge.MAP_PAGE)
        second_url = fetch.call_args_list[1].args[0]
        self.assertIn('CNT=5', second_url)
        self.assertIn('SE=040', second_url)
        self.assertEqual(result['results'], [])

    def test_bounded_request_sends_observed_suumo_filter_fields(self):
        page = b"<script>suumo.ApiParam={'bkApi':{'UID':'public123','STMP':'1234567890','ATT':'abcdef0123456789abcdef0123456789abcdef01'}}</script>"
        body = json.dumps({'smatch': {'resultset': {'totalhits': 0, 'item': []}}}).encode()
        with patch.object(bridge, '_curl_get', side_effect=[page, body]) as fetch:
            bridge.search(35.69, 139.70, 1, 15, '1K')
        second_url = fetch.call_args_list[1].args[0]
        self.assertIn('CHINRYOMAX=15', second_url)
        self.assertIn('CINM=02', second_url)
        self.assertIn('CNT=5', second_url)

    def test_transport_failure_and_timeout_are_explicit_errors(self):
        with patch.object(bridge, 'run', return_value=SimpleNamespace(returncode=7, stderr=b'', stdout=b'')):
            with self.assertRaises(bridge.SuumoError) as failed:
                bridge._curl_get(bridge.MAP_PAGE, max_bytes=100, timeout=1)
            self.assertEqual(failed.exception.code, 'transport')
        with patch.object(bridge, 'run', side_effect=TimeoutExpired('curl', 1)):
            with self.assertRaises(bridge.SuumoError) as timed_out:
                bridge._curl_get(bridge.MAP_PAGE, max_bytes=100, timeout=1)
            self.assertEqual(timed_out.exception.code, 'transport')

    def test_non_success_http_and_oversize_body_are_rejected(self):
        with patch.object(bridge, 'run', return_value=SimpleNamespace(returncode=0, stderr=b'HTTP_STATUS:403', stdout=b'')):
            with self.assertRaises(bridge.SuumoError) as denied:
                bridge._curl_get(bridge.MAP_PAGE, max_bytes=100, timeout=1)
            self.assertEqual(denied.exception.code, 'http')
        with patch.object(bridge, 'run', return_value=SimpleNamespace(returncode=0, stderr=b'HTTP_STATUS:200', stdout=b'x' * 101)):
            with self.assertRaises(bridge.SuumoError) as too_large:
                bridge._curl_get(bridge.MAP_PAGE, max_bytes=100, timeout=1)
            self.assertEqual(too_large.exception.code, 'size')


if __name__ == '__main__': unittest.main()
