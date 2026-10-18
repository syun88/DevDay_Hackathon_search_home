"""Agent tool and lifecycle tests. No external API requests are made here."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from session_store import SessionStore, public_session
from home_agent import HomeAgent, AgentError, evidence

def full_result(store, token, out):
    """Search tools return a compact summary; tests also inspect the stored result set."""
    if isinstance(out, dict) and 'recommended' in out:
        stored = next(r for r in store.get(token)['result_sets'] if r['id'] == out['result_set_id'])
        return {**stored, 'summary': out}
    return out


class AgentToolsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.token, _ = self.store.create()
        self.place = {'id': 'place_tokyo', 'name': '東京駅', 'lat': 35.681236, 'lon': 139.767125}
        self.raw = {'results': [{'title': 'Example source result', 'url': 'https://suumo.jp/chintai/example/',
                    'description': 'Source snippet', 'source': 'suumo.jp', 'availability_status': 'unknown',
                    'extracted': {'price': 8, 'layout': '1K', 'area': 25, 'age': None, 'address': None}}], 'provider': 'bing'}
        self.agent = HomeAgent(self.store, 'synthetic-test-key', lambda *args: self.raw,
                               geocoder=lambda query: [self.place])
        self.agent.resume = lambda token, **kwargs: None
        self.agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Find a place',
                                     'expected_revision': 0, 'language': 'en'})
        self.count = 0

    def execute(self, name, args=None, call_id=None):
        self.count += 1
        return full_result(self.store, self.token, self.agent.execute(self.token, 'request_1234567890',
            {'type': 'function_call', 'call_id': call_id or 'call_' + str(self.count),
             'turn_id': 'turn_1', 'name': name, 'arguments': args or {}}))

    def test_tool_journal_prevents_duplicate_updates(self):
        first = self.execute('update_search', {'criteria': {'max_price_yen': 100000}}, 'one')
        second = self.execute('update_search', {'criteria': {'max_price_yen': 100000}}, 'one')
        self.assertEqual(first, second)
        self.assertEqual(self.store.get(self.token)['revision'], 2)
        self.assertEqual(len(self.store.get(self.token)['history']), 1)

    def test_manual_edits_are_not_silently_overwritten(self):
        self.store.update(self.token, {'expected_revision': 1, 'criteria': {'layout': '2K'}})
        result = self.execute('update_search', {'criteria': {'max_price_yen': 100000}})
        self.assertEqual(result['error'], 'stale_revision')
        self.execute('get_search_state')
        self.execute('update_search', {'criteria': {'max_price_yen': 100000}})
        self.assertEqual(self.store.get(self.token)['criteria']['layout'], '2K')

    def test_named_place_must_come_from_geocoder(self):
        self.assertIn('error', self.execute('set_origin', {'place_id': 'invented', 'which': 'a'}))
        self.execute('resolve_place', {'query': '東京駅'})
        self.execute('set_origin', {'place_id': 'place_tokyo', 'which': 'a', 'alias': 'my office'})
        self.assertEqual(self.store.get(self.token)['memory']['places']['my office']['name'], '東京駅')
        self.assertIn('error', self.execute('update_search', {'criteria': {'origin_a': self.place}}))
        self.execute('set_origin', {'place_id': 'my office', 'which': 'b'})
        self.assertEqual(self.store.get(self.token)['criteria']['origin_b']['name'], '東京駅')

    def test_explicit_memory_correction_and_forgetting(self):
        self.execute('remember_preference', {'name': 'cat', 'value': 'Mochi'})
        self.execute('remember_preference', {'name': 'cat', 'value': 'Momo'})
        self.assertEqual(self.store.get(self.token)['memory']['facts']['cat'], 'Momo')
        self.execute('remember_preference', {'name': 'cat', 'value': ''})
        self.assertNotIn('cat', self.store.get(self.token)['memory']['facts'])

    def test_result_ids_comparison_saving_and_removal(self):
        result = self.execute('search_listings')
        listing = result['items'][0]
        args = {'result_set_id': result['id'], 'listing_id': listing['id']}
        self.execute('save_listing', args, 'save_one'); self.execute('save_listing', args, 'save_one')
        self.assertEqual(len(self.store.get(self.token)['saved_candidates']), 1)
        compared = self.execute('compare_listings', {'result_set_id': result['id'], 'listing_ids': [listing['id']]})
        self.assertEqual(compared['candidates'][0]['url'], listing['url'])
        self.assertEqual(listing['match']['status'], 'mismatch')
        self.execute('remove_saved_listing', {'listing_id': listing['id']})
        self.assertEqual(self.store.get(self.token)['saved_candidates'], [])

    def test_discovery_leads_render_as_references_without_becoming_verified_matches(self):
        item = self.raw['results'].pop()
        item['discovery_only'] = True
        item['limitations'] = ['availability unknown', 'mandatory monthly fees unknown']
        self.raw.update(leads=[item], inventory_status='no_verified_feed_configured', source_search_links=[{'source':'SUUMO','url':'https://www.google.com/search?q=suumo'}])
        result = self.execute('search_listings')
        self.assertEqual(result['counts']['leads'],1)
        self.assertEqual(result['counts']['confirmed'],0)
        self.assertEqual(result['items'][0]['match']['status'],'unverified')
        self.assertEqual(result['inventory_status'],'no_verified_feed_configured')
        saved = self.execute('save_listing', {'result_set_id':result['id'], 'listing_id':result['items'][0]['id']})
        self.assertTrue(saved['saved']['discovery_only'])
        self.assertEqual(saved['saved']['match']['status'],'unverified')

    def test_road_routes_cover_both_origins_and_are_invalidated_by_mode_changes(self):
        from datetime import datetime, timezone
        item = self.raw['results'][0]
        item.update(kind='rent', availability_status='confirmed', last_verified_at=datetime.now(timezone.utc).isoformat(),
                    lat=self.place['lat'], lon=self.place['lon'], geo_precision='exact')
        self.execute('set_origin', {'place_id':'origin_a', 'which':'b'})
        self.execute('update_search', {'criteria':{'restrict_b':True, 'max_commute_minutes':20}})
        routes = []
        def router(payload):
            routes.append(payload)
            return {'status':'ok', 'duration_seconds':1200, 'checked_at':datetime.now(timezone.utc).isoformat()}
        self.agent.router = router
        result = self.execute('search_listings')
        args = {'result_set_id':result['id'], 'listing_id':result['items'][0]['id']}
        routed = self.execute('get_commute', args)['candidate']
        self.assertEqual(len(routes), 2)
        self.assertNotIn('commute time', routed['match']['unknown'])
        self.execute('update_search', {'criteria':{'commute_mode':'bike'}})
        compared = self.execute('compare_listings', {'result_set_id':result['id'], 'listing_ids':[routed['id']]})
        self.assertIn('commute time', compared['candidates'][0]['match']['unknown'])

    def test_road_route_never_uses_an_approximate_geocoder_position(self):
        result = self.execute('search_listings')
        self.agent.router = lambda payload: self.fail('Must not route an unverified position')
        routed = self.execute('get_commute', {'result_set_id':result['id'], 'listing_id':result['items'][0]['id']})
        self.assertEqual(routed['error'], 'route_unavailable')

    def test_unknown_reference_does_not_save_invented_listing(self):
        result = self.execute('search_listings')
        self.assertIn('error', self.execute('save_listing', {'result_set_id': result['id'], 'listing_id': 'invented'}))
        self.assertEqual(self.store.get(self.token)['saved_candidates'], [])

    def test_late_search_does_not_replace_newer_criteria_results(self):
        def slow(*args):
            self.store.update(self.token, {'expected_revision': 1, 'criteria': {'layout': '2K'}})
            return self.raw
        self.agent.search = slow
        self.assertEqual(self.execute('search_listings')['error'], 'stale_revision')
        self.assertEqual(self.store.get(self.token)['result_sets'], [])

    def test_undo_restores_criteria_and_keeps_unrelated_memory(self):
        self.execute('remember_preference', {'name': 'cat', 'value': 'Mochi'})
        self.execute('update_search', {'criteria': {'pets': True}})
        self.execute('undo_search')
        self.assertFalse(self.store.get(self.token)['criteria']['pets'])
        self.assertEqual(self.store.get(self.token)['memory']['facts']['cat'], 'Mochi')

    def test_unknown_fields_and_invalid_tool_arguments_do_not_run(self):
        for name, args in [('execute_code', {}), ('set_origin', {'which': 'a', 'place_id': 42}),
                           ('remember_preference', {'name': 'x', 'value': 'y', 'extra': 1}),
                           ('update_search', {'criteria': {'pets': 'true'}})]:
            with self.subTest(name=name): self.assertIn('error', self.execute(name, args))
        self.assertEqual(self.store.get(self.token)['revision'], 1)

    def test_current_origin_can_be_remembered_without_regeocoding(self):
        self.execute('set_origin', {'place_id': 'origin_a', 'which': 'a', 'alias': 'office'})
        self.assertEqual(self.store.get(self.token)['memory']['places']['office']['name'], '東京駅')

    def test_submission_id_cannot_be_reused_for_different_text(self):
        with self.assertRaises(ValueError):
            self.agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Different text', 'expected_revision': 1, 'language': 'en'})
        self.assertEqual(len(self.store.get(self.token)['conversation']), 1)

    def test_old_submission_replay_never_adds_a_second_turn(self):
        self.store.mutate(self.token, lambda s: s['chat'].update(status='idle'))
        self.agent.start(self.token, {'request_id': 'another_1234567890', 'message': 'Hello', 'expected_revision': 1, 'language': 'en'})
        self.store.mutate(self.token, lambda s: s['chat'].update(status='idle'))
        self.agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Find a place', 'expected_revision': 0, 'language': 'en'})
        self.assertEqual(len(self.store.get(self.token)['conversation']), 2)

    def test_pending_reset_blocks_criteria_writes_until_remote_deletion(self):
        self.store.mutate(self.token, lambda s: s['chat'].update(status='idle'))
        result = self.agent.reset(self.token, 1)
        self.assertEqual(result['chat']['status'], 'resetting')
        from session_store import RevisionConflict
        with self.assertRaises(RevisionConflict):
            self.store.update(self.token, {'expected_revision': result['revision'], 'criteria': {'layout': '2K'}})

    def test_stop_cancels_provider_before_allowing_new_messages(self):
        class API:
            def __init__(self): self.called = []
            def request(self, method, path, body=None):
                self.called.append((method,path))
                return {'status': 'idle'}
        self.agent.api = API()
        self.store.mutate(self.token, lambda s: s['_agent'].update(session_id='sess_test'))
        self.assertEqual(self.agent.cancel(self.token)['chat']['status'], 'cancelling')
        self.agent.lifecycle(self.token)
        self.assertEqual(self.store.get(self.token)['chat']['status'], 'cancelled')
        self.assertEqual(self.agent.api.called[0], ('POST', '/sess_test/events'))

    def test_session_private_state_is_not_exposed(self):
        self.execute('remember_preference', {'name': 'cat', 'value': 'Mochi'})
        self.assertNotIn('_agent', public_session(self.store.get(self.token)))
        self.assertNotIn('_tool_results', public_session(self.store.get(self.token)))
        self.assertNotIn('expected_revision', public_session(self.store.get(self.token))['chat'])

    def test_idempotent_submission_and_busy_other_message(self):
        payload = {'request_id': 'request_1234567890', 'message': 'Find a place', 'expected_revision': 0, 'language': 'en'}
        self.agent.start(self.token, payload)
        self.assertEqual(len(self.store.get(self.token)['conversation']), 1)
        payload.update(request_id='different_1234567890', expected_revision=1)
        with self.assertRaises(AgentError): self.agent.start(self.token, payload)

    def test_clear_history_also_deletes_remote_session(self):
        class API:
            def request(self, *args, **kwargs):
                self.called = args
                return {'deleted': True}
        api = API(); self.agent.api = api
        def prepare(s):
            s['chat']['status'] = 'idle'; s['_agent']['session_id'] = 'sess_test'
            s['memory']['facts']['cat'] = 'Mochi'
        self.store.mutate(self.token, prepare)
        self.agent.reset(self.token, 1)
        self.agent.lifecycle(self.token)
        result = self.store.get(self.token)
        self.assertEqual(api.called, ('DELETE', '/sess_test'))
        self.assertEqual(result['conversation'], [])
        self.assertEqual(result['memory'], {'facts': {}, 'places': {}})
        self.assertEqual(result['_agent'], {})


class AgentLifecycleTest(unittest.TestCase):
    def test_agent_executes_required_tool_then_returns_saved_final_message(self):
        class FakeAPI:
            model = 'synthetic-model'
            completed = False
            def request(self, method, path, body=None, key=None):
                if method == 'POST' and path == '':
                    self.configuration = body
                    return {'id': 'sess_test'}
                if method == 'GET' and path == '/sess_test':
                    return {'status': 'idle' if self.completed else 'requires_action', 'required_actions': [] if self.completed else [
                        {'type': 'function_call', 'turn_id': 'turn_1', 'call_id': 'call_1', 'name': 'update_search', 'arguments': {'criteria': {'max_price_yen': 80000}}}]}
                if method == 'POST' and path.endswith('/events'):
                    self.completed = True
                    return {}
                if '/turns?' in path:
                    return {'data': [{'id': 'turn_1', 'status': 'completed' if self.completed else 'in_progress'}]}
                if '/items?' in path:
                    return {'data': [{'type': 'message', 'role': 'assistant', 'turn_id': 'turn_1', 'phase': 'final_answer',
                                      'content': [{'type': 'output_text', 'text': 'Updated the budget to ¥80,000.'}]}], 'has_more': False}
                raise AssertionError(path)
        with tempfile.TemporaryDirectory() as folder:
            store = SessionStore(Path(folder) / 'sessions.sqlite3'); token, _ = store.create()
            api = FakeAPI(); agent = HomeAgent(store, 'synthetic-key', lambda *args: {}, api=api)
            agent.resume = lambda token, **kwargs: None
            agent.start(token, {'request_id': 'request_1234567890', 'message': 'Budget 80000 yen', 'expected_revision': 0, 'language': 'en'})
            agent.run(token)
            result = store.get(token)
            self.assertEqual(result['chat']['status'], 'idle')
            self.assertEqual(result['criteria']['max_price_yen'], 80000)
            self.assertIn('80,000', result['conversation'][-1]['text'])
            self.assertEqual(api.configuration['environment'], {'type': 'none'})
            self.assertIn('update_search', [t['name'] for t in api.configuration['agent']['tools']])

    def test_unknown_fields_never_become_confirmed_matches(self):
        criteria = {'max_price_yen': 100000, 'min_area_m2': None, 'max_age_years': None,
                    'layout': '', 'pets': True, 'parking': False}
        flags = evidence({'extracted': {}}, criteria)
        self.assertEqual(flags['status'], 'mismatch')
        self.assertIn('total monthly cost / sale price', flags['unknown'])
        flags = evidence({'extracted': {'price': 15}}, criteria)
        self.assertEqual(flags['status'], 'mismatch')


class CurrentListingRulesTest(unittest.TestCase):
    def record(self):
        from datetime import datetime, timezone
        return {'url': 'https://example.com/home', 'kind': 'rent', 'availability_status': 'confirmed',
                'last_verified_at': datetime.now(timezone.utc).isoformat(), 'lat': 35.681236, 'lon': 139.767125, 'geo_precision': 'exact',
                'extracted': {'price': 8, 'management_fee_yen': 5000, 'mandatory_fee_yen': 2000}}

    def test_agent_budget_counts_all_monthly_fees_and_unknown_is_not_zero(self):
        from session_store import DEFAULT_CRITERIA
        criteria = {**DEFAULT_CRITERIA, 'property_type': 'rent', 'max_price_yen': 87000}
        item = self.record()
        self.assertEqual(evidence(item, criteria)['status'], 'confirmed')
        criteria['max_price_yen'] = 86999
        self.assertEqual(evidence(item, criteria)['status'], 'mismatch')
        item['extracted']['mandatory_fee_yen'] = None
        self.assertEqual(evidence(item, criteria)['status'], 'unverified')

    def test_source_expiry_and_approximate_positions_never_confirm_matches(self):
        from session_store import DEFAULT_CRITERIA
        item = self.record(); item['geo_precision'] = 'approximate'
        self.assertEqual(evidence(item, DEFAULT_CRITERIA)['status'], 'unverified')
        item['last_verified_at'] = '2000-01-01T00:00:00+00:00'
        self.assertEqual(evidence(item, DEFAULT_CRITERIA)['status'], 'mismatch')

    def test_prototype_listed_budget_requires_reconfirmation_after_upgrade(self):
        from session_store import DEFAULT_CRITERIA
        session = SessionStore.upgrade({'created_at':'2026-01-01', 'schema_version':2, 'revision':4,
                                       'criteria':{**DEFAULT_CRITERIA, 'max_price_yen':90000}})
        self.assertIsNone(session['criteria']['max_price_yen'])
        self.assertEqual(session['memory']['facts']['previous_listed_rent_cap_yen'], '90000')
        self.assertEqual(session['revision'], 5)
        self.assertEqual(SessionStore.upgrade(session)['revision'], 5)


if __name__ == '__main__':
    unittest.main()
