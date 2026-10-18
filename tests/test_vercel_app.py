"""The Vercel WSGI bridge serves the same handler, including streamed events. Offline only."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from socketserver import ThreadingMixIn

import server
import vercel_app
from home_agent import HomeAgent
from session_store import SessionStore, _PostgresConnection, open_store


class ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Quiet(WSGIRequestHandler):
    def log_message(self, *args):
        pass


class BridgeTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        store = SessionStore(Path(self.temp.name) / 's.sqlite3')
        agent = HomeAgent(store, 'synthetic-key', lambda *a: {})
        agent.resume = lambda token: None
        for target, value in (('SESSION_STORE', store), ('HOME_AGENT', agent), ('STREAM_SECONDS', 3)):
            patcher = patch.object(server, target, value); patcher.start(); self.addCleanup(patcher.stop)
        self.http = make_server('127.0.0.1', 0, vercel_app.app, ThreadingWSGIServer, Quiet)
        patcher = patch.object(vercel_app._Server, 'server_port', self.http.server_port); patcher.start(); self.addCleanup(patcher.stop)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.http.shutdown(), self.http.server_close()))
        self.agent, self.store = agent, store

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=10)
        headers = {'Content-Type': 'application/json', 'Host': f'localhost:{self.http.server_port}'}
        if cookie: headers['Cookie'] = cookie
        connection.request(method, path, json.dumps(body) if body is not None else None, headers)
        response = connection.getresponse()
        data = response.read(); connection.close()
        return response.status, dict(response.getheaders()), data

    def test_static_assets_sessions_writes_and_private_paths(self):
        status, headers, page = self.request('GET', '/')
        self.assertEqual(status, 200); self.assertIn(b'chatDraft', page)
        self.assertEqual(self.request('GET', '/chat.js')[0], 200)
        for private in ('/.env', '/.local/sessions.sqlite3', '/server.py', '/session_store.py', '/.git/config'):
            self.assertEqual(self.request('GET', private)[0], 404, private)
        status, headers, body = self.request('GET', '/api/session')
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        status, _, body = self.request('POST', '/api/session', {'expected_revision': 0, 'criteria': {'layout': '1LDK'}}, cookie)
        self.assertEqual((status, json.loads(body)['criteria']['layout']), (200, '1LDK'))
        self.assertEqual(self.request('POST', '/api/session', {'expected_revision': 0}, cookie)[0], 409)

    def test_server_sent_events_stream_through_the_bridge(self):
        _, headers, _ = self.request('GET', '/api/session')
        cookie = headers['Set-Cookie'].split(';')[0]
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=10)
        connection.request('GET', '/api/chat/stream', headers={'Cookie': cookie, 'Host': f'localhost:{self.http.server_port}'})
        response = connection.getresponse()
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
        lines = []
        while not any(line.startswith('data: ') for line in lines):
            lines.append(response.fp.readline().decode().strip())
        self.assertIn('event: state', lines)
        connection.close()


class StoreSelectionTest(unittest.TestCase):
    def test_serverless_without_database_refuses_ephemeral_sqlite(self):
        with patch.dict(os.environ, {'VERCEL': '1'}, clear=False):
            os.environ.pop('DATABASE_URL', None); os.environ.pop('POSTGRES_URL', None)
            with self.assertRaises(RuntimeError):
                open_store('/tmp/never-used.sqlite3')

    def test_postgres_adapter_locks_sessions_and_upserts_archives(self):
        calls = []
        class Fake:
            def execute(self, sql, params=()):
                calls.append(sql); return self
        connection = _PostgresConnection(Fake())
        connection.execute('SELECT data FROM sessions WHERE id=?', ('a',))
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('SELECT data FROM sessions WHERE id=?', ('a',))
        connection.execute('INSERT OR REPLACE INTO archived_chats VALUES (?,?,?,?,?)', (1, 2, 3, 4, 5))
        self.assertEqual(calls[0], 'SELECT data FROM sessions WHERE id=%s')
        self.assertEqual(calls[2], 'SELECT data FROM sessions WHERE id=%s FOR UPDATE')
        self.assertIn('ON CONFLICT (owner,id) DO UPDATE', calls[3])
        self.assertNotIn('?', calls[3])


if __name__ == '__main__':
    unittest.main()


class DailyLimitTest(unittest.TestCase):
    def test_shared_daily_turn_allowance_is_atomic_and_bounded(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 's.sqlite3')
            self.assertEqual([store.take_quota('turns-day', 2) for _ in range(3)], [True, True, False])
            self.assertTrue(store.take_quota('turns-next-day', 2))
