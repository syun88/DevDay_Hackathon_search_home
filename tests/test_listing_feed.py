import json
import io
import unittest
from datetime import datetime, timezone
from unittest.mock import patch
from urllib.error import HTTPError

import server
from listing_feed import normalize_feed


class FeedTest(unittest.TestCase):
    now = datetime(2026, 10, 18, 5, tzinfo=timezone.utc)

    def record(self, **overrides):
        return {'title': 'TEST ONLY, not a rental advertisement', 'source_name': 'Test source',
                'source_url': 'https://example.com/test-unit', 'kind': 'rent', 'rent_yen': 70000,
                'management_fee_yen': 5000, 'mandatory_fee_yen': 0, 'area_m2': 25, 'age': 5,
                'lat': 35.681236, 'lon': 139.767125, 'geo_precision': 'exact',
                'availability_status': 'confirmed', 'last_verified_at': '2026-10-18T04:59:00Z', **overrides}

    def test_source_values_and_verification_time_are_preserved(self):
        output = normalize_feed([self.record()], self.now)
        result = output['results'][0]
        self.assertEqual(result['extracted']['price'], 7)
        self.assertEqual(result['extracted']['management_fee_yen'], 5000)
        self.assertEqual(result['extracted']['mandatory_fee_yen'], 0)
        self.assertEqual(result['last_verified_at'], '2026-10-18T04:59:00+00:00')
        self.assertEqual(result['geo_precision'], 'exact')
        self.assertIsNone(result['extracted']['pets'])

    def test_stale_unknown_ended_future_and_undated_records_are_rejected(self):
        records = [self.record(last_verified_at=value) for value in [None, '', '2026-10-18T03:59:59Z', '2026-10-18T05:00:01Z', '2026-10-18T04:59:00']]
        records += [self.record(availability_status=value) for value in ['unknown', 'ended']]
        output = normalize_feed(records, self.now)
        self.assertEqual(output['results'], [])
        self.assertEqual(output['rejected'], len(records))

    def test_one_hour_boundary_and_unknown_fees(self):
        output = normalize_feed([self.record(last_verified_at='2026-10-18T04:00:00Z', management_fee_yen=None)], self.now)
        self.assertEqual(len(output['results']), 1)
        self.assertIsNone(output['results'][0]['extracted']['management_fee_yen'])

    def test_missing_source_and_unsafe_links_are_rejected(self):
        records = [self.record(source_url=url) for url in ['javascript:alert(1)', 'http://example.com/', 'https://user:secret@example.com/']]
        records += [self.record(source_name='')]
        self.assertEqual(normalize_feed(records, self.now)['results'], [])

    def test_invalid_coordinates_and_numbers_remain_unknown(self):
        for value in [True, None, float('nan'), '35.0']:
            result = normalize_feed([self.record(lat=value, rent_yen='invalid')], self.now)['results'][0]
            self.assertIsNone(result['lat'])
            self.assertIsNone(result['extracted']['price'])
            self.assertEqual(result['geo_precision'], 'unknown')

    def test_source_identifiers_preserve_distinct_units(self):
        output = normalize_feed([self.record(source_record_id='A'), self.record(source_record_id='B'), self.record(source_record_id='A')], self.now)
        self.assertEqual([record['source_record_id'] for record in output['results']], ['A', 'B'])


class RoutingTest(unittest.TestCase):
    payload = {'origin': {'lat': 35.681236, 'lon': 139.767125},
               'destination': {'lat': 35.6808, 'lon': 139.7638}, 'mode': 'foot'}

    def test_profiles_direction_and_returned_duration(self):
        response = json.dumps({'code': 'Ok', 'routes': [{'duration': 125.5, 'distance': 320}]})
        for mode in ('foot', 'bike', 'car'):
            with patch.object(server, 'get_text', return_value=response) as request, patch.object(server, '_last_route_request', 0):
                result = server.route_time({**self.payload, 'mode': mode})
            self.assertIn(f'/routed-{mode}/route/v1/{mode}/139.767125,35.681236;139.763800,35.680800', request.call_args.args[0])
            self.assertEqual(result['duration_seconds'], 125.5)
            self.assertFalse(result['live_traffic'])

    def test_invalid_inputs_do_not_contact_the_provider(self):
        with patch.object(server, 'get_text') as request:
            for payload in [{**self.payload, 'mode': 'transit'}, {**self.payload, 'origin': {'lat': True, 'lon': 139}}, {**self.payload, 'origin': {'lat': 95, 'lon': 139}}]:
                with self.assertRaises(ValueError):
                    server.route_time(payload)
            request.assert_not_called()

    def test_no_route_is_not_replaced_with_a_straight_line_estimate(self):
        with patch.object(server, 'get_text', return_value='{"code":"NoRoute","routes":[]}'), patch.object(server, '_last_route_request', 0):
            result = server.route_time(self.payload)
        self.assertIsNone(result['duration_seconds'])
        self.assertEqual(result['status'], 'unavailable')

    def test_rate_limit_and_invalid_responses(self):
        with patch.object(server, 'get_text', return_value='{"code":"Ok","routes":[{"duration":null,"distance":10}]}'), patch.object(server, '_last_route_request', 10), patch.object(server.time, 'monotonic', return_value=10.1), patch.object(server.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                server.route_time(self.payload)
            self.assertAlmostEqual(sleep.call_args.args[0], 1.0)

    def test_http_no_segment_is_an_unavailable_route(self):
        error = HTTPError('https://routing.openstreetmap.de/test', 400, 'Bad Request', {}, io.BytesIO(b'{"code":"NoSegment"}'))
        with patch.object(server, 'get_text', side_effect=error), patch.object(server, '_last_route_request', 0):
            result = server.route_time(self.payload)
        self.assertEqual(result['status'], 'unavailable')
        self.assertIsNone(result['duration_seconds'])
