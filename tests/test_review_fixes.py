"""Regressions for map sync, public records and cancellation edge cases. No external requests."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import map_agent
import server
from home_agent import AgentError, HomeAgent
from session_store import SessionStore
try:
    from test_agent import full_result
except ImportError:
    from tests.test_agent import full_result
from datetime import datetime, timezone

PUBLIC = [{'id': 'tokyo:one', 'source': 'tokyo', 'name': 'Official one', 'address': '東京都千代田区一番町1',
           'geo_precision': 'unknown', 'lat': None, 'lon': None, 'layer': 'inventory'},
          {'id': 'tokyo:two', 'source': 'tokyo', 'name': 'Official two', 'address': '東京都千代田区二番町2',
           'geo_precision': 'unknown', 'lat': None, 'lon': None, 'layer': 'inventory'}]


class Harness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.places = [{'id': 'place_shinjuku', 'name': '新宿駅', 'lat': 35.6896, 'lon': 139.7006}]
        # A verified feed record near Shinjuku: confirmed only once the origin is Shinjuku.
        listing = {'title': 'Verified home', 'url': 'https://example.com/home', 'kind': 'rent', 'availability_status': 'confirmed',
                   'last_verified_at': datetime.now(timezone.utc).isoformat(), 'lat': 35.69, 'lon': 139.70,
                   'geo_precision': 'exact', 'extracted': {'price': 9}}
        self.agent = HomeAgent(self.store, 'synthetic-key', lambda *a: {'results': [], 'leads': [dict(listing)]},
                               geocoder=lambda q: self.places)
        self.agent.resume = lambda token: None
        for target, value in (('SESSION_STORE', self.store), ('HOME_AGENT', self.agent)):
            patcher = patch.object(server, target, value); patcher.start(); self.addCleanup(patcher.stop)
        class Quiet(server.Handler):
            def log_message(self, *args): pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.http.shutdown(), self.http.server_close()))
        status, headers, _ = self.request('GET', '/api/session')
        self.cookie = headers['Set-Cookie'].split(';')[0]
        self.token = self.cookie.split('=', 1)[1]
        self.count = 0

    def request(self, method, path, payload=None):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        headers = {'Content-Type': 'application/json'}
        if getattr(self, 'cookie', None): headers['Cookie'] = self.cookie
        connection.request(method, path, json.dumps(payload) if payload is not None else None, headers)
        response = connection.getresponse(); raw = response.read(); connection.close()
        return response.status, dict(response.getheaders()), json.loads(raw) if raw else None

    def report(self, **values):
        s = self.store.get(self.token)
        body = {'chat_id': s['chat_id'], 'expected_revision': s['map_view']['revision'], 'center': {'lat': 35.68, 'lon': 139.76},
                'zoom': 13, 'public_records': [{'id': r['id'], 'source': r['source']} for r in s['public_records']],
                'public_version': s.get('public_version', 0), **values}
        return self.request('POST', '/api/map', body)

    def start(self, message='Find a home'):
        s = self.store.get(self.token)
        self.agent.start(self.token, {'request_id': 'request_%016d' % self.count, 'message': message,
                                      'expected_revision': s['revision'], 'language': 'en'})

    def execute(self, name, args=None):
        self.count += 1
        request_id = self.store.get(self.token)['chat']['request_id']
        return full_result(self.store, self.token, self.agent.execute(self.token, request_id, {'type': 'function_call', 'call_id': 'call_%d' % self.count,
                                  'turn_id': 'turn_1', 'name': name, 'arguments': args or {}}))


class ResetWithSelectionTest(Harness):
    def test_reports_naming_records_removed_by_reset_are_pruned_not_rejected(self):
        self.start(); result = self.execute('search_listings')
        listing = result['items'][0]['id']
        command = self.store.mutate(self.token, lambda s: map_agent.queue(s, 'zoom', zoom=15))[1]['map_command']
        self.assertEqual(self.report(result_set_id=result['id'], selected_listing_id=listing, visible_listing_ids=[listing])[0], 200)
        self.store.mutate(self.token, lambda s: s['chat'].update(status='idle'))
        self.agent.reset(self.token, self.store.get(self.token)['revision'])
        self.agent.lifecycle(self.token)
        # A lagging browser still names the old selection, result set and command ack.
        status, _, body = self.report(result_set_id=result['id'], selected_listing_id=listing, visible_listing_ids=[listing],
                                      visible_features=[{'result_set_id': result['id'], 'listing_id': listing}],
                                      ack={'id': command['id'], 'status': 'applied'})
        self.assertEqual(status, 200)
        view = self.store.get(self.token)['map_view']
        self.assertEqual((view['selected_listing_id'], view['result_set_id'], view['visible_listing_ids'], view['visible_features']), (None, None, [], []))
        self.assertIsNone(self.store.get(self.token)['map_command'])
        # Structurally invalid reports are still rejected.
        self.assertEqual(self.report(zoom=99)[0], 400)


class AgentMapRevisionTest(Harness):
    def setUp(self):
        super().setUp()
        sleeper = patch('home_agent.time.sleep'); sleeper.start(); self.addCleanup(sleeper.stop)  # No browser acks here.

    def test_origin_recenter_and_applied_commands_do_not_look_like_manual_pans(self):
        self.start('Find homes near Shinjuku Station')
        self.execute('resolve_place', {'query': '新宿駅'})
        self.assertNotIn('error', self.execute('set_origin', {'place_id': 'place_shinjuku', 'which': 'a'}))
        # The browser recenters for the new origin: an app change, not a user pan.
        self.assertEqual(self.report(center={'lat': 35.6896, 'lon': 139.7006}, cause='app')[0], 200)
        result = self.execute('search_listings')
        self.assertEqual(result['summary']['map'], 'queued')  # Not stale_map_view.
        command = self.store.get(self.token)['map_command']
        self.assertEqual(command['action'], 'show_results')
        self.assertEqual(self.report(zoom=15, cause='app', ack={'id': command['id'], 'status': 'applied'})[0], 200)
        # A second command in the same turn, without re-reading the map, is still allowed.
        second = self.agent.apply_tool(self.store.get(self.token), 'control_map', {'action': 'zoom', 'zoom': 16}, None, {})
        self.assertIn('map_command', second)

    def test_real_manual_pan_still_blocks_an_older_agent_plan(self):
        self.start()
        self.assertEqual(self.report(center={'lat': 35.70, 'lon': 139.70}, cause='user')[0], 200)
        result = self.execute('control_map', {'action': 'zoom', 'zoom': 16})
        self.assertEqual(result['error'], 'stale_map_view')
        self.execute('get_map_state')
        self.assertNotIn('error', self.execute('control_map', {'action': 'zoom', 'zoom': 16}))

    def test_queue_compares_against_user_revision_only(self):
        s = {'chat_id': 'c', 'map_view': map_agent.initial_view(), 'map_command': None, 'result_sets': [], 'public_records': []}
        base = {'chat_id': 'c', 'center': {'lat': 35.6, 'lon': 139.7}, 'zoom': 12}
        map_agent.report(s, {**base, 'expected_revision': 0, 'zoom': 13, 'cause': 'app'})
        self.assertEqual((s['map_view']['revision'], s['map_view']['user_revision']), (1, 0))
        self.assertIn('map_command', map_agent.queue(s, 'zoom', zoom=14, expected_revision=0))
        map_agent.report(s, {**base, 'expected_revision': 1, 'zoom': 11})  # Default cause is a user change.
        self.assertEqual(map_agent.queue(s, 'zoom', zoom=14, expected_revision=0)['error'], 'stale_map_view')


class PublicRecordTest(Harness):
    def setUp(self):
        super().setUp()
        loader = patch('public_housing.load', return_value={'records': [dict(r) for r in PUBLIC]})
        loader.start(); self.addCleanup(loader.stop)

    def store_agent_records(self, records, version_bump=True):
        def change(s):
            s['public_records'] = records
            if version_bump: s['public_version'] = s.get('public_version', 0) + 1
        self.store.mutate(self.token, change)

    def test_owned_coordinates_survive_an_expired_geocode_cache(self):
        located = {**PUBLIC[0], 'lat': 35.6895, 'lon': 139.7389, 'geo_precision': 'approximate', 'geocode_label': '一番町1'}
        self.store_agent_records([located])
        with patch('public_housing.cached_position', return_value={}):
            self.assertEqual(self.report(result_set_id='public')[0], 200)
        record = self.store.get(self.token)['public_records'][0]
        self.assertEqual((record['lat'], record['lon'], record['geo_precision']), (35.6895, 139.7389, 'approximate'))
        self.assertIn('map_command', map_agent.queue(self.store.get(self.token), 'focus_listing', 'public', 'tokyo:one'))

    def test_older_browser_list_cannot_replace_records_the_agent_just_loaded(self):
        stale_version = self.store.get(self.token).get('public_version', 0)
        self.store_agent_records([dict(PUBLIC[1])])
        status, _, body = self.report(public_records=[{'id': 'tokyo:one', 'source': 'tokyo'}], public_version=stale_version)
        self.assertEqual(status, 200)
        self.assertEqual([r['id'] for r in self.store.get(self.token)['public_records']], ['tokyo:two'])
        self.assertEqual([r['id'] for r in body['public_records']], ['tokyo:two'])  # The browser adopts it.

    def test_manual_load_on_the_current_version_is_accepted_and_versioned(self):
        version = self.store.get(self.token).get('public_version', 0)
        status, _, body = self.report(public_records=[{'id': 'tokyo:one', 'source': 'tokyo'}], public_version=version)
        self.assertEqual(status, 200)
        self.assertEqual([r['id'] for r in body['public_records']], ['tokyo:one'])
        self.assertEqual(body['public_version'], version + 1)
        self.assertEqual(self.report(public_version='1')[0], 400)


class FailedCancelTest(unittest.TestCase):
    def test_retry_after_failed_cancel_cancels_again_and_never_resumes(self):
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 's.sqlite3'); token, _ = store.create()
            class API:
                idle = False
                def request(self, method, path, body=None, key=None):
                    return {'status': 'idle' if self.idle else 'in_progress'}
            api = API(); agent = HomeAgent(store, 'synthetic-key', lambda *a: {}, api=api)
            agent.resume = lambda token: None
            agent.start(token, {'request_id': 'request_1234567890', 'message': 'Hi', 'expected_revision': 0, 'language': 'en'})
            store.mutate(token, lambda s: s['_agent'].update(session_id='sess_1'))
            agent.cancel(token, 'request_1234567890')
            with patch('home_agent.time.sleep'):
                agent.lifecycle(token)
            chat = store.get(token)['chat']
            self.assertEqual((chat['status'], chat['operation'], chat['recoverable']), ('error', 'cancel', True))
            with self.assertRaises(AgentError):
                agent.recover(token, 'request_1234567890')  # Resume would re-run tools the user stopped.
            self.assertEqual(store.get(token)['chat']['status'], 'error')
            api.idle = True
            agent.cancel(token, 'request_1234567890')
            agent.lifecycle(token)
            self.assertEqual(store.get(token)['chat']['status'], 'cancelled')


if __name__ == '__main__':
    unittest.main()
