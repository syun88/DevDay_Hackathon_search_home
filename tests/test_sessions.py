"""Offline schema, persistence, concurrency and HTTP session tests."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import server
from session_store import SessionStore, RevisionConflict, DEFAULT_CRITERIA, validate_patch


class SessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.token, self.session = self.store.create()

    def test_patch_preserves_other_fields_and_survives_store_restart(self):
        out = self.store.update(self.token, {'expected_revision': 0,
            'criteria': {'max_price_yen': 100000, 'pets': True}, 'draft_message': '猫と暮らしたい'})
        self.assertEqual(out['revision'], 1)
        self.assertEqual(out['criteria']['radius_a_km'], 3)
        self.assertEqual(SessionStore(self.store.path).get(self.token), out)
        self.assertEqual(out['draft_message'], '猫と暮らしたい')
        self.assertEqual(self.store.update(self.token, {'expected_revision': 1})['revision'], 1)

    def test_rejects_unknown_types_nonfinite_and_out_of_range_fields(self):
        invalid = [{'invented': True}, {'radius_a_km': 0}, {'radius_b_km': float('nan')}, {'radius_a_km': 1.25},
                   {'max_price_yen': True}, {'max_price_yen': 1.5}, {'max_age_years': -1},
                   {'origin_a': None}, {'origin_a': {'name': 'x', 'lat': 90, 'lon': 0}},
                   {'pets': 'true'}, {'listing_area': 'x' * 91}, {'property_type': 'unknown'}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_patch(value)
        self.assertEqual(self.store.get(self.token)['revision'], 0)

    def test_nullable_zero_and_coordinate_boundaries(self):
        self.assertEqual(validate_patch({'max_price_yen': 0, 'max_age_years': 0,
            'origin_b': None, 'origin_a': {'name': '日付変更線', 'lat': 85, 'lon': 180}})['max_price_yen'], 0)
        validate_patch({'max_price_yen': None})

    def test_sessions_are_isolated_and_defaults_not_mutated(self):
        second, _ = self.store.create()
        self.store.update(self.token, {'expected_revision': 0, 'criteria': {'pets': True}})
        self.assertFalse(self.store.get(second)['criteria']['pets'])
        self.assertEqual(self.store.get(second)['criteria'], DEFAULT_CRITERIA)
        self.assertIsNone(self.store.get('not-a-session'))

    def test_stale_revision_cannot_overwrite_newer_state(self):
        self.store.update(self.token, {'expected_revision': 0, 'criteria': {'layout': '1K'}})
        with self.assertRaises(RevisionConflict) as conflict:
            self.store.update(self.token, {'expected_revision': 0, 'criteria': {'layout': '2K'}})
        self.assertEqual(conflict.exception.session['criteria']['layout'], '1K')

    def test_concurrent_updates_have_only_one_winner(self):
        def update(layout):
            try:
                self.store.update(self.token, {'expected_revision': 0, 'criteria': {'layout': layout}})
                return 'saved'
            except RevisionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(update, ['1K', '2K']))
        self.assertCountEqual(outcomes, ['saved', 'conflict'])
        self.assertEqual(self.store.get(self.token)['revision'], 1)

    def test_multiple_chats_restore_their_own_memory_and_criteria(self):
        first = self.store.get(self.token)
        self.store.mutate(self.token, lambda s: s['memory']['facts'].update(cat='Mochi'))
        first = self.store.update(self.token, {'expected_revision': 0, 'criteria': {'layout': '1K'}})
        second = self.store.switch_chat(self.token, 1)
        self.assertNotEqual(first['chat_id'], second['chat_id'])
        self.assertEqual(second['memory']['facts'], {})
        self.assertEqual(second['criteria']['layout'], '')
        restored = self.store.switch_chat(self.token, second['revision'], first['chat_id'])
        self.assertEqual(restored['memory']['facts']['cat'], 'Mochi')
        self.assertEqual(restored['criteria']['layout'], '1K')
        self.assertGreater(restored['revision'], second['revision'])
        self.assertEqual(len(self.store.list_chats(self.token)), 2)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT data FROM archived_chats WHERE owner=? AND id=?',(self.token,first['chat_id'])).fetchone())

    def test_chat_archive_is_scoped_to_browser_session(self):
        first = self.store.get(self.token)
        self.store.switch_chat(self.token, 0)
        other, _ = self.store.create()
        with self.assertRaises(ValueError):
            self.store.switch_chat(other, 0, first['chat_id'])
        self.assertEqual(len(self.store.list_chats(other)), 1)

    def test_chat_switch_rejects_stale_revisions_and_active_work(self):
        from session_store import RevisionConflict
        with self.assertRaises(RevisionConflict): self.store.switch_chat(self.token, 99)
        self.store.mutate(self.token, lambda s: s['chat'].update(status='running'))
        with self.assertRaises(RevisionConflict): self.store.switch_chat(self.token, 0)

    def test_update_envelope_and_draft_limits(self):
        for data in [{}, {'expected_revision': True}, {'expected_revision': 0, 'other': 1},
                     {'expected_revision': 0, 'draft_message': 'x' * 2001}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                self.store.update(self.token, data)


class SessionHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.patcher = patch.object(server, 'SESSION_STORE', store)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        class QuietHandler(server.Handler):
            def log_message(self, *args):
                pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)

    def close(self):
        self.http.shutdown(); self.http.server_close(); self.thread.join()

    def request(self, method, path, payload=None, cookie=None, origin=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port)
        headers = {'Content-Type': 'application/json'}
        if cookie: headers['Cookie'] = cookie
        if origin: headers['Origin'] = origin
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse()
        raw = response.read()
        result = (response.status, dict(response.getheaders()), json.loads(raw) if raw and response.getheader('Content-Type', '').startswith('application/json') else raw)
        connection.close()
        return result

    def test_cookie_restore_revision_conflict_and_isolation(self):
        status, headers, first = self.request('GET', '/api/session')
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie); self.assertIn('SameSite=Strict', cookie)
        cookie = cookie.split(';')[0]
        status, _, updated = self.request('POST', '/api/session',
            {'expected_revision': 0, 'criteria': {'max_price_yen': 80000}}, cookie)
        self.assertEqual(status, 200)
        self.assertEqual(self.request('GET', '/api/session', cookie=cookie)[2], updated)
        self.assertIsNone(self.request('GET', '/api/session')[2]['criteria']['max_price_yen'])
        status, _, conflict = self.request('POST', '/api/session', {'expected_revision': 0}, cookie)
        self.assertEqual(status, 409)
        self.assertEqual(conflict['session']['revision'], 1)

    def test_rejects_cross_origin_missing_session_and_bad_schema(self):
        _, headers, _ = self.request('GET', '/api/session')
        cookie = headers['Set-Cookie'].split(';')[0]
        self.assertEqual(self.request('POST', '/api/session', {'expected_revision': 0}, cookie, 'https://evil.example')[0], 403)
        self.assertEqual(self.request('POST', '/api/session', {'expected_revision': 0})[0], 404)
        self.assertEqual(self.request('POST', '/api/session', {'expected_revision': 0, 'criteria': {'pets': 1}}, cookie)[0], 400)
        self.assertEqual(self.request('GET', '/api/session/schema')[2]['schema_version'], 2)

    def test_chat_endpoint_is_cookie_bound_and_hides_provider_state(self):
        from home_agent import HomeAgent
        agent = HomeAgent(server.SESSION_STORE, 'synthetic-test-key', lambda *args: {})
        agent.resume = lambda token: None
        with patch.object(server, 'HOME_AGENT', agent):
            _, headers, _ = self.request('GET', '/api/session')
            cookie = headers['Set-Cookie'].split(';')[0]
            payload = {'request_id': 'request_1234567890', 'message': 'Remember my cat', 'expected_revision': 0, 'language': 'en'}
            status, _, session = self.request('POST', '/api/chat', payload, cookie)
            self.assertEqual(status, 200)
            self.assertEqual(session['chat']['status'], 'running')
            self.assertNotIn('_agent', session)
            self.assertNotIn('provider_input', session['chat'])
            self.assertEqual(len(self.request('GET', '/api/session', cookie=cookie)[2]['conversation']), 1)
            self.assertEqual(self.request('GET', '/api/session')[2]['conversation'], [])
            self.assertEqual(self.request('POST', '/api/chat', payload, cookie, 'https://evil.example')[0], 403)
            self.assertEqual(self.request('POST', '/api/chat', [], cookie)[0], 400)

    def test_schema_exposes_standard_json_types(self):
        _, _, schema = self.request('GET', '/api/session/schema')
        self.assertEqual(schema['update']['required'], ['expected_revision'])
        self.assertEqual(schema['criteria']['properties']['max_price_yen']['type'], ['integer', 'null'])
        self.assertEqual(schema['criteria']['properties']['origin_a']['type'], 'object')

    def test_assets_are_served_and_private_files_are_not(self):
        for path in ['/', '/i18n.js', '/session.js', '/workspace.js', '/workspace.css']:
            with self.subTest(path=path): self.assertEqual(self.request('GET', path)[0], 200)
        for path in ['/.env', '/%2eenv', '/.local/sessions.sqlite3', '/session_store.py', '/.git/config']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path)[0], 404)
                self.assertEqual(self.request('HEAD', path)[0], 404)


if __name__ == '__main__':
    unittest.main()
