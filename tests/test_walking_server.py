"""Walking API contracts, validation and failures; no external network access."""
import copy
import json
import threading
import unittest
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen
from unittest.mock import patch

import server


def contour(minutes=15, geometry=None):
    return {'type': 'FeatureCollection', 'features': [{
        'type': 'Feature', 'properties': {'contour': minutes, 'metric': 'time'},
        'geometry': geometry or {'type': 'Polygon', 'coordinates': [[
            [139.76, 35.68], [139.77, 35.68], [139.77, 35.69], [139.76, 35.68],
        ]]},
    }]}


def expansion():
    return {'type': 'FeatureCollection', 'features': [{
        'type': 'Feature', 'properties': {'duration': 600, 'edge_status': 's'},
        'geometry': {'type': 'LineString', 'coordinates': [[139.76, 35.68], [139.77, 35.68]]},
    }]}


class WalkingAreaTest(unittest.TestCase):
    def setUp(self):
        server._walking_cache.clear()
        server._walking_last_request = 0

    def test_parameters_reject_nonfinite_out_of_range_and_unsupported_times(self):
        for parameters in [('', '139', '15'), ('nan', '139', '15'), ('35', 'inf', '15'),
                           ('86', '139', '15'), ('35', '181', '15'), ('35', '139', '4'),
                           ('35', '139', '61'), ('35', '139', '15.5')]:
            with self.subTest(parameters=parameters), patch.object(server, 'get_text') as get:
                self.assertRaises(ValueError, server.fetch_walking_area, *parameters)
                get.assert_not_called()
        self.assertEqual(server.walking_parameters('35.6812361', '139.7671251', '15'),
                         (35.681236, 139.767125, 15))

    def test_actual_upstream_contract_is_pedestrian_polygon_with_identifying_header(self):
        with patch.object(server, 'get_text', side_effect=[json.dumps(contour()), json.dumps(expansion())]) as get, \
                patch.object(server.time, 'sleep'):
            result = server.fetch_walking_area(35.681236, 139.767125, 15)
        payload = json.loads(parse_qs(urlparse(get.call_args_list[0].args[0]).query)['json'][0])
        self.assertEqual(payload['costing'], 'pedestrian')
        self.assertEqual(payload['costing_options']['pedestrian']['walking_speed'], 4.8)
        self.assertEqual(payload['locations'][0]['search_filter'], {'exclude_ferry': True})
        self.assertGreater(payload['costing_options']['pedestrian']['ferry_cost'], 60 * 60)
        self.assertEqual(payload['contours'], [{'time': 15}])
        self.assertIs(payload['polygons'], True)
        self.assertIn('X-Client-Id', get.call_args.kwargs['headers'])
        self.assertEqual(result['geometry']['type'], 'Polygon')
        self.assertEqual(result['network']['type'], 'MultiLineString')
        network_payload = json.loads(parse_qs(urlparse(get.call_args_list[1].args[0]).query)['json'][0])
        self.assertEqual(network_payload['action'], 'isochrone')
        self.assertTrue(network_payload['skip_opposites'])
        self.assertEqual(network_payload['expansion_properties'], ['duration', 'edge_status'])
        self.assertEqual(result['minutes'], 15)
        self.assertEqual(result['source'], 'Valhalla / OpenStreetMap')

    def test_cache_separates_time_and_origin_and_expires(self):
        clock = [100.0]

        def upstream(url, **kwargs):
            payload = json.loads(parse_qs(urlparse(url).query)['json'][0])
            return json.dumps(expansion() if '/expansion?' in url else contour(payload['contours'][0]['time']))

        with patch.object(server, 'get_text', side_effect=upstream) as get, \
                patch.object(server.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(server.time, 'sleep'):
            server.fetch_walking_area(35.68, 139.76, 15)
            self.assertTrue(server.fetch_walking_area(35.68, 139.76, 15)['cached'])
            self.assertEqual(get.call_count, 2)
            server.fetch_walking_area(35.68, 139.76, 30)
            server.fetch_walking_area(35.69, 139.76, 15)
            self.assertEqual(get.call_count, 6)
            clock[0] += server.WALKING_CACHE_TTL + 1
            server.fetch_walking_area(35.68, 139.76, 15)
            self.assertEqual(get.call_count, 8)

    def test_cached_area_is_not_blocked_by_a_slow_network_request(self):
        with patch.object(server, 'get_text', side_effect=[json.dumps(contour()), json.dumps(expansion())]), \
                patch.object(server.time, 'sleep'):
            server.fetch_walking_area(35.68, 139.76, 15)
        results = []
        server._walking_lock.acquire()
        worker = threading.Thread(target=lambda: results.append(server.fetch_walking_area(35.68, 139.76, 15)), daemon=True)
        try:
            worker.start()
            worker.join(timeout=0.5)
            blocked = worker.is_alive()
        finally:
            server._walking_lock.release()
            worker.join(timeout=1)
        self.assertFalse(blocked, 'A cached area waited for the public-service lock')
        self.assertTrue(results[0]['cached'])

    def test_accepts_multipolygon_holes_and_rejects_invalid_or_wrong_contours(self):
        ring = contour()['features'][0]['geometry']['coordinates'][0]
        geometry = {'type': 'MultiPolygon', 'coordinates': [[ring, ring], [ring]]}
        self.assertEqual(server.walking_geometry(contour(geometry=geometry), 15), geometry)
        invalid = [[], {}, contour(30), contour(geometry={'type': 'LineString', 'coordinates': ring})]
        for value in (None, [], {'type': 'Polygon', 'coordinates': [[]]},
                      {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, 1], [2, 2]]]},
                      {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 0], [1, float('nan')], [0, 0]]]}):
            data = copy.deepcopy(contour())
            data['features'][0]['geometry'] = value
            invalid.append(data)
        bad_properties = contour()
        bad_properties['features'][0]['properties'] = []
        invalid.append(bad_properties)
        for data in invalid:
            with self.subTest(data=data):
                self.assertRaises(ValueError, server.walking_geometry, data, 15)

    def test_failure_is_not_cached_or_replaced_with_a_circle(self):
        with patch.object(server, 'get_text', side_effect=URLError('offline')):
            self.assertRaises(URLError, server.fetch_walking_area, 35.68, 139.76, 15)
        self.assertEqual(server._walking_cache, {})

    def test_expansion_discards_frontier_edges_and_edges_over_the_time_budget(self):
        data = expansion()
        for duration, status in [(901, 's'), (1200, 's'), (300, 'r'), (300, 'c'), (-1, 's'), (float('nan'), 's')]:
            edge = copy.deepcopy(data['features'][0])
            edge['properties'] = {'duration': duration, 'edge_status': status}
            data['features'].append(edge)
        result = server.walking_network(data, 15)
        self.assertEqual(len(result['coordinates']), 1)
        self.assertEqual(result['coordinates'][0], expansion()['features'][0]['geometry']['coordinates'])
        self.assertRaises(ValueError, server.walking_network, {'type': 'FeatureCollection', 'features': []}, 15)


class WalkingHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class QuietHandler(server.Handler):
            def log_message(self, *args):
                pass
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def test_get_serves_area_and_invalid_input_stays_local(self):
        expected = {'geometry': contour()['features'][0]['geometry'], 'minutes': 15}
        with patch.object(server, 'fetch_walking_area', return_value=expected) as fetch:
            with urlopen(self.base + '/api/walking-area?lat=35.68&lon=139.76&minutes=15') as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response), expected)
            fetch.assert_called_once_with(35.68, 139.76, 15)
        with patch.object(server, 'fetch_walking_area') as fetch:
            with self.assertRaises(HTTPError) as error:
                urlopen(self.base + '/api/walking-area?lat=nan&lon=139&minutes=15')
            self.assertEqual(error.exception.code, 400)
            fetch.assert_not_called()

    def test_upstream_failures_return_json_error(self):
        for failure in (URLError('offline'), ValueError('invalid upstream JSON'), TimeoutError()):
            with self.subTest(failure=failure), patch.object(server, 'fetch_walking_area', side_effect=failure):
                with self.assertRaises(HTTPError) as error:
                    urlopen(self.base + '/api/walking-area?lat=35&lon=139&minutes=15')
                self.assertEqual(error.exception.code, 502)
                self.assertIn('error', json.load(error.exception))


if __name__ == '__main__':
    unittest.main()
