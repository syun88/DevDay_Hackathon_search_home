import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import server


class QuietHandler(server.Handler):
    def log_message(self, *args):
        pass


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = 'http://127.0.0.1:' + str(cls.http.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join()

    def request(self, path, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = Request(self.base + path, data=data, headers={'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def record(self):
        return {'title': 'TEST ONLY', 'source_name': 'Test source', 'source_url': 'https://example.com/test-only',
                'kind': 'rent', 'rent_yen': 70000, 'availability_status': 'confirmed',
                'last_verified_at': datetime.now(timezone.utc).isoformat()}

    def test_import_requires_permission_and_preserves_source_time(self):
        record = self.record()
        status, _ = self.request('/api/import', {'records': [record], 'permitted': False})
        self.assertEqual(status, 400)
        status, body = self.request('/api/import', {'records': [record], 'permitted': True})
        self.assertEqual(status, 200)
        self.assertEqual(body['results'][0]['last_verified_at'], record['last_verified_at'])

    def test_refresh_reads_feed_again_without_reusing_outdated_data(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory) / 'feed.json'
            filename.write_text(json.dumps([self.record()]))
            with patch.object(server, 'FEED_PATH', str(filename)), patch.object(server, 'FEED_URL', ''):
                status, body = self.request('/api/listings?place=Tokyo&provider=feed')
                self.assertEqual(status, 200)
                self.assertEqual(len(body['results']), 1)
                old = {**self.record(), 'last_verified_at': (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()}
                filename.write_text(json.dumps([old]))
                _, body = self.request('/api/listings?place=Tokyo&provider=feed')
                self.assertEqual(body['results'], [])
                self.assertEqual(body['rejected'], 1)

    def test_search_returns_links_without_snippet_prices_or_availability(self):
        with patch.object(server, 'fetch_listings', return_value={'results': [{'url': 'https://suumo.jp/test-only', 'source': 'suumo.jp', 'title': 'OLD TEST', 'extracted': {'price': 1}}], 'provider': 'bing'}):
            status, body = self.request('/api/listings?place=TEST-UNIQUE&provider=bing')
        self.assertEqual(status, 200)
        self.assertEqual(body['results'], [])
        self.assertEqual(body['discovery_links'], [{'url': 'https://suumo.jp/test-only', 'source': 'suumo.jp'}])
        self.assertNotIn('OLD TEST', json.dumps(body))

    def test_route_endpoint_and_failure_are_not_reported_as_matches(self):
        payload = {'origin': {'lat': 35, 'lon': 139}, 'destination': {'lat': 35.001, 'lon': 139}, 'mode': 'foot'}
        with patch.object(server, 'route_time', return_value={'status': 'unavailable', 'duration_seconds': None}):
            status, body = self.request('/api/route', payload)
        self.assertEqual(status, 200)
        self.assertIsNone(body['duration_seconds'])
        with patch.object(server, 'route_time', side_effect=OSError('test-only')):
            status, body = self.request('/api/route', payload)
        self.assertEqual(status, 502)
        self.assertNotIn('duration_seconds', body)

    def test_private_files_are_not_served(self):
        for path in ['/.git/config', '/server.py', '/listing_feed.py', '/.env']:
            self.assertEqual(self.request(path)[0], 404)

    def test_invalid_imports_and_routes_are_rejected(self):
        self.assertEqual(self.request('/api/import', {'permitted': True, 'records': {}})[0], 400)
        self.assertEqual(self.request('/api/route', {'mode': 'transit'})[0], 400)

    def test_health_never_exposes_keys_or_private_feed_path(self):
        with patch.object(server, 'FEED_PATH', '/private/test-only.json'):
            status, body = self.request('/api/health')
        self.assertEqual(status, 200)
        self.assertTrue(body['feed_enabled'])
        self.assertNotIn('/private/', json.dumps(body))
