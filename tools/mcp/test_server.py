"""Boundary and error checks. The live SUUMO flow is in verify_stdio.py."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import unittest
from unittest.mock import patch

from mcp import Client

import server


class LocalBackendHandler(BaseHTTPRequestHandler):
    status = 200
    body = b'{"results": [], "availability_status": "unknown"}'
    redirect_hits = 0

    def do_GET(self):
        if self.path == "/should-not-follow":
            type(self).redirect_hits += 1
        self.send_response(self.status)
        if self.status == 302:
            self.send_header("Location", "/should-not-follow")
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class BackendTests(unittest.TestCase):
    def setUp(self):
        LocalBackendHandler.status = 200
        LocalBackendHandler.body = b'{"results": [], "availability_status": "unknown"}'
        LocalBackendHandler.redirect_hits = 0
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), LocalBackendHandler)
        self.worker = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.worker.start()
        self.backend = server.HousingBackend(f"http://127.0.0.1:{self.httpd.server_port}")

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.worker.join()

    def test_loopback_origin_only(self):
        for value in ("https://127.0.0.1", "http://example.com", "http://10.0.0.1", "http://127.0.0.1.evil.example", "http://user:secret@127.0.0.1", "http://127.0.0.1/path", "http://127.0.0.1?url=x", "http://127.0.0.1#x", "file:///tmp/a", "http://127.0.0.1:0"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.validate_backend_url(value)
        self.assertEqual(server.validate_backend_url("http://localhost:8773/"), "http://127.0.0.1:8773")
        self.assertEqual(server.validate_backend_url("http://[::1]:8773"), "http://[::1]:8773")

    def test_response_preserves_unknowns_and_empty_results(self):
        self.assertEqual(self.backend.get("/api/suumo-search"), {"results": [], "availability_status": "unknown"})

    def test_upstream_failure_is_not_empty_success(self):
        LocalBackendHandler.status = 502
        LocalBackendHandler.body = json.dumps({"error": "source unavailable"}).encode()
        with self.assertRaisesRegex(server.BackendError, "HTTP 502: source unavailable"):
            self.backend.get("/api/suumo-search")

    def test_redirects_are_not_followed(self):
        LocalBackendHandler.status = 302
        with self.assertRaisesRegex(server.BackendError, "HTTP 302"):
            self.backend.get("/api/health")
        self.assertEqual(LocalBackendHandler.redirect_hits, 0)

    def test_non_json_is_error(self):
        LocalBackendHandler.body = b"<html>unavailable</html>"
        with self.assertRaisesRegex(server.BackendError, "invalid JSON"):
            self.backend.get("/api/health")

    def test_endpoint_is_closed(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            self.backend.get("http://example.com")


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_invalid_arguments_never_reach_backend(self):
        async with Client(server.mcp) as client:
            for name, args in (
                ("search_properties", {"lat": 0, "lon": 139.7}),
                ("search_properties", {"lat": 35.7, "lon": 139.7, "radius_km": 5}),
                ("search_properties", {"lat": 35.7, "lon": 139.7, "page": 1.5}),
                ("get_property_address", {"source_id": "https://example.com"}),
                ("search_public_housing", {"source": "unapproved"}),
                ("search_public_housing", {"offset": -1}),
            ):
                with self.subTest(tool=name, args=args), patch.object(server.backend, "get") as get:
                    result = await client.call_tool(name, args)
                    self.assertTrue(result.is_error)
                    get.assert_not_called()

    async def test_backend_error_becomes_mcp_error(self):
        with patch.object(server.backend, "get", side_effect=server.BackendError("Housing backend HTTP 403: disabled")):
            async with Client(server.mcp) as client:
                result = await client.call_tool("get_property_address", {"source_id": "123456789012"})
                self.assertTrue(result.is_error)
                self.assertIsNone(result.structured_content)
                self.assertIn("HTTP 403: disabled", str(result.content))

    async def test_search_passes_conditions_and_evidence_unchanged(self):
        evidence = {"results": [{"station_access": None, "availability_status": "unknown"}], "cached": True, "has_next": True}
        with patch.object(server.backend, "get", return_value=evidence) as get:
            async with Client(server.mcp) as client:
                result = await client.call_tool("search_properties", {"lat": 35.7, "lon": 139.7, "radius_km": 0.5, "max_rent_man": 15, "layout": "1K", "page": 2})
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content, evidence)
                get.assert_called_once_with("/api/suumo-search", {"lat": 35.7, "lon": 139.7, "radius_km": 0.5, "max_rent_man": 15.0, "layout": "1K", "page": 2})


if __name__ == "__main__":
    unittest.main()
