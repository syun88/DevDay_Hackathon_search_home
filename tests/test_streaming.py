"""Streaming contract tests. No external API requests are made here."""
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import server
from home_agent import AGENT_CONFIG_VERSION, AgentError, EventStream, HomeAgent, LiveTurn
from session_store import SessionStore, public_session


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type='text/event-stream'):
        super().__init__(body)
        self.headers = {'Content-Type': content_type}


class FakeStream:
    def __init__(self, events):
        self.events, self.json_body, self.closed = events, None, False

    def __iter__(self):
        yield from self.events

    def close(self):
        self.closed = True


def delta(item, text, index=0):
    return {'type': 'agent.session.turn.output_text.delta', 'turn_id': 'turn_1', 'item_id': item,
            'output_index': 0, 'content_index': index, 'delta': text}


def added(item, phase):
    return {'type': 'agent.session.turn.item.added', 'turn_id': 'turn_1',
            'item': {'type': 'message', 'role': 'assistant', 'id': item, 'phase': phase, 'content': [], 'turn_id': 'turn_1'}}


class EventStreamTest(unittest.TestCase):
    def test_parses_documented_sse_frames(self):
        body = (b': comment\n\nevent: agent.session.turn.output_text.delta\n'
                b'data: {"item_id":"msg_1","delta":"Hel"}\n\n'
                b'data: {"type":"agent.session.turn.output_text.delta",\ndata: "item_id":"msg_1","delta":"lo"}\n\n'
                b'data: not json\n\n')
        events = list(EventStream(FakeResponse(body)))
        self.assertEqual([e['type'] for e in events], ['agent.session.turn.output_text.delta'] * 2)
        self.assertEqual(''.join(e['delta'] for e in events), 'Hello')

    def test_json_response_is_not_treated_as_a_stream(self):
        stream = EventStream(FakeResponse(b'{"id":"sess_1"}', 'application/json'))
        self.assertEqual(stream.json_body, {'id': 'sess_1'})
        self.assertEqual(list(stream), [])


class LiveTurnTest(unittest.TestCase):
    def test_commentary_is_separate_and_restored_items_wait_for_complete_text(self):
        live = LiveTurn('chat_1', 'request_1', [{'id': 'msg_1', 'phase': 'final_answer', 'text': 'Partial'}])
        agent = HomeAgent(None, 'synthetic-key', lambda *args: {})
        agent.apply_event('owner', live, delta('msg_1', ' gap?'), None)
        self.assertEqual(live.text(), 'Partial')  # Missed deltas are never spliced together.
        agent.apply_event('owner', live, {'type': 'agent.session.turn.output_text.done', 'item_id': 'msg_1',
                                          'turn_id': 'turn_1', 'content_index': 0, 'text': 'Partial answer.'}, None)
        agent.apply_event('owner', live, added('msg_c', 'commentary'), None)
        agent.apply_event('owner', live, delta('msg_c', 'Checking the map'), None)
        self.assertEqual(live.text(), 'Partial answer.')
        self.assertEqual(live.public()['note'], 'Checking the map')
        self.assertIsNone(agent.apply_event('owner', live, delta('msg_old', 'old turn'), 'turn_1'))
        self.assertEqual(agent.apply_event('owner', live, {'type': 'agent.session.turn.failed',
                         'turn': {'id': 'turn_2', 'subagent_id': None}}, 'turn_1'), 'failed')
        self.assertIsNone(agent.apply_event('owner', live, {'type': 'agent.session.turn.completed',
                          'turn': {'id': 'turn_3', 'subagent_id': 'sub_1'}}, 'turn_1'))


class StreamingLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.token, _ = self.store.create()

    def agent(self, api):
        agent = HomeAgent(self.store, 'synthetic-key', lambda *args: {}, api=api)
        agent.resume = lambda token, **kwargs: None
        agent.start(self.token, {'request_id': 'request_1234567890', 'message': 'Budget 80000 yen',
                                 'expected_revision': 0, 'language': 'en'})
        return agent

    def test_create_streams_text_and_tool_activity_then_saves_authoritative_items(self):
        test = self

        class API:
            model = 'synthetic-model'
            posted = False
            seen_live = None
            calls = []

            def stream(self, method, path, body=None, key=None):
                self.calls.append(('stream', method, path))
                if method == 'POST':
                    self.create_body = body
                    return FakeStream([
                        {'type': 'agent.session.created', 'session': {'id': 'sess_1'}},
                        {'type': 'agent.session.turn.created', 'turn_id': 'turn_1', 'turn': {'id': 'turn_1', 'subagent_id': None}},
                        added('msg_c', 'commentary'), delta('msg_c', 'Updating your budget.'),
                        {'type': 'agent.session.turn.item.added', 'turn_id': 'turn_1', 'item': {
                            'type': 'function_call', 'call_id': 'call_1', 'name': 'update_search', 'id': 'fc_1', 'turn_id': 'turn_1'}},
                        {'type': 'agent.session.requires_action'}])
                return FakeStream([added('msg_f', 'final_answer'), delta('msg_f', 'Updated '), delta('msg_f', 'the budget.'),
                                   {'type': 'agent.session.turn.completed', 'turn_id': 'turn_1', 'turn': {'id': 'turn_1', 'subagent_id': None}}])

            def request(self, method, path, body=None, key=None):
                self.calls.append((method, path))
                if path == '/sess_1':
                    return {'status': 'idle' if self.posted else 'requires_action', 'required_actions': [] if self.posted else [
                        {'type': 'function_call', 'turn_id': 'turn_1', 'call_id': 'call_1', 'name': 'update_search',
                         'arguments': {'criteria': {'max_price_yen': 80000}}}]}
                if path.endswith('/events'):
                    self.posted = True
                    return {}
                if '/turns?' in path:
                    # Completion is reported once the stream has delivered the answer text.
                    streamed = (agent.live_view(test.token, test.store.get(test.token)) or {}).get('text') == 'Updated the budget.'
                    return {'data': [{'id': 'turn_1', 'status': 'completed' if self.posted and streamed else 'in_progress'}]}
                if '/items?' in path:
                    self.seen_live = agent.live_view(test.token, test.store.get(test.token))
                    return {'data': [{'type': 'message', 'role': 'assistant', 'turn_id': 'turn_1', 'phase': 'final_answer',
                                      'content': [{'type': 'output_text', 'text': 'Updated the budget to ¥80,000.'}]}], 'has_more': False}
                raise AssertionError(path)

        api = API(); agent = self.agent(api)
        agent.run(self.token)
        result = self.store.get(self.token)
        self.assertTrue(api.create_body['stream'])
        self.assertEqual(api.create_body['environment'], {'type': 'none'})
        self.assertEqual(api.seen_live['text'], 'Updated the budget.')  # Streamed before the final save.
        self.assertEqual(api.seen_live['note'], 'Updating your budget.')
        self.assertEqual(result['chat']['status'], 'idle')
        self.assertEqual(result['criteria']['max_price_yen'], 80000)
        reply = result['conversation'][-1]
        self.assertEqual(reply['text'], 'Updated the budget to ¥80,000.')
        self.assertEqual([(a['name'], a['status']) for a in reply['activity']], [('update_search', 'done')])
        self.assertNotIn('partial', result['chat'])
        self.assertNotIn('call_1', json.dumps(public_session(result)))  # Provider call IDs stay private.
        self.assertEqual(sum(1 for c in api.calls if c[0] == 'POST' and c[1].endswith('/events')), 1)

    def test_follow_up_subscribes_before_sending_input(self):
        self.store.mutate(self.token, lambda s: s['_agent'].update(session_id='sess_1', config_version=AGENT_CONFIG_VERSION))

        class API:
            model = 'synthetic-model'
            calls = []
            def stream(self, method, path, body=None, key=None):
                self.calls.append(('stream', path))
                return FakeStream([added('msg_f', None), delta('msg_f', 'Done.'),
                                   {'type': 'agent.session.turn.completed', 'turn': {'id': 'turn_2', 'subagent_id': None}}])
            def request(self, method, path, body=None, key=None):
                self.calls.append((method, path))
                if '/turns?' in path:
                    return {'data': [{'id': 'turn_2' if any(c[0] == 'POST' for c in self.calls) else 'turn_1', 'status': 'completed'}]}
                if path == '/sess_1':
                    return {'status': 'idle', 'required_actions': []}
                if '/items?' in path:
                    return {'data': [{'type': 'message', 'role': 'assistant', 'turn_id': 'turn_2', 'phase': None,
                                      'content': [{'type': 'output_text', 'text': 'Done.'}]}]}
                return {}
        api = API(); agent = self.agent(api)
        agent.run(self.token)
        order = [c for c in api.calls if c[0] in ('stream', 'POST')]
        self.assertEqual(order[0], ('stream', '/sess_1/events?stream=true'))
        self.assertEqual(order[1], ('POST', '/sess_1/events'))
        self.assertEqual(self.store.get(self.token)['conversation'][-1]['text'], 'Done.')

    def test_terminal_failure_keeps_partial_text_explicitly_incomplete(self):
        class API:
            model = 'synthetic-model'
            def stream(self, method, path, body=None, key=None):
                return FakeStream([{'type': 'agent.session.created', 'session': {'id': 'sess_1'}},
                                   added('msg_f', 'final_answer'), delta('msg_f', 'I found two'),
                                   {'type': 'agent.session.turn.failed', 'turn': {'id': 'turn_1', 'subagent_id': None}}])
            def request(self, method, path, body=None, key=None):
                return {'status': 'in_progress', 'required_actions': []}
        agent = self.agent(API())
        agent.run(self.token)
        result = self.store.get(self.token)
        self.assertEqual(result['chat']['status'], 'error')
        self.assertFalse(result['chat']['recoverable'])
        partial = result['conversation'][-1]
        self.assertEqual((partial['id'], partial['text'], partial['incomplete']), ('partial_request_1234567890', 'I found two', 'failed'))
        self.assertNotIn('partial', result['chat'])

    def test_recoverable_disconnect_keeps_durable_partial_then_cancel_marks_it_incomplete(self):
        class API:
            model = 'synthetic-model'
            def stream(self, method, path, body=None, key=None):
                if method == 'POST':
                    return FakeStream([{'type': 'agent.session.created', 'session': {'id': 'sess_1'}},
                                       added('msg_f', 'final_answer'), delta('msg_f', 'Searching official records')])
                raise AgentError('provider_connection')
            cancelled = False
            def request(self, method, path, body=None, key=None):
                if path == '/sess_1/events':
                    self.cancelled = True
                if path == '/sess_1' and not self.cancelled:
                    raise AgentError('provider_connection')
                return {'status': 'idle'}
        agent = self.agent(API())
        agent.run(self.token)
        session = self.store.get(self.token)
        self.assertEqual((session['chat']['status'], session['chat']['error'], session['chat']['recoverable']), ('error', 'provider_connection', True))
        self.assertEqual(public_session(session)['chat']['partial']['text'], 'Searching official records')
        self.assertFalse(any(m['role'] == 'assistant' for m in session['conversation']))
        agent.cancel(self.token)
        agent.lifecycle(self.token)
        result = self.store.get(self.token)
        self.assertEqual(result['chat']['status'], 'cancelled')
        self.assertEqual(result['conversation'][-1]['incomplete'], 'cancelled')
        self.assertEqual(result['conversation'][-1]['text'], 'Searching official records')


class ChatStreamEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        store = SessionStore(Path(self.temp.name) / 'sessions.sqlite3')
        self.agent = HomeAgent(store, 'synthetic-key', lambda *args: {})
        self.agent.resume = lambda token, **kwargs: None
        for target, value in (('SESSION_STORE', store), ('HOME_AGENT', self.agent)):
            patcher = patch.object(server, target, value); patcher.start(); self.addCleanup(patcher.stop)
        class Quiet(server.Handler):
            def log_message(self, *args): pass
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.http.shutdown(), self.http.server_close()))
        self.store = store

    def events(self, response, count):
        out, name = [], None
        while len(out) < count:
            line = response.fp.readline().decode().rstrip('\n')
            if line.startswith('event: '): name = line[7:]
            elif line.startswith('data: '): out.append((name, json.loads(line[6:])))
        return out

    def test_stream_sends_state_then_offset_checked_appends_and_replacements(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request('GET', '/api/session'); response = connection.getresponse(); response.read()
        cookie = response.getheader('Set-Cookie').split(';')[0]
        token = cookie.split('=', 1)[1]
        self.agent.start(token, {'request_id': 'request_1234567890', 'message': 'Hi', 'expected_revision': 0, 'language': 'en'})
        session = self.store.get(token)
        live = LiveTurn(session['chat_id'], 'request_1234567890')
        self.agent.live[token] = live
        self.agent.apply_event(token, live, delta('msg_1', 'Hel'), None)
        stream = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        stream.request('GET', '/api/chat/stream', headers={'Cookie': cookie})
        response = stream.getresponse()
        self.assertEqual(response.getheader('Content-Type'), 'text/event-stream; charset=utf-8')
        (_, state), (_, first) = self.events(response, 2)
        self.assertEqual((state['status'], state['request_id']), ('running', 'request_1234567890'))
        self.assertEqual(first['text'], 'Hel')
        self.agent.apply_event(token, live, delta('msg_1', 'lo'), None)
        (_, second), = self.events(response, 1)
        self.assertEqual((second['offset'], second['delta']), (3, 'lo'))
        self.agent.apply_event(token, live, {'type': 'agent.session.turn.output_text.done', 'item_id': 'msg_1', 'text': 'Hi!'}, None)
        (_, third), = self.events(response, 1)
        self.assertEqual(third['text'], 'Hi!')
        stream.close(); connection.close()

    def test_stream_requires_session_and_same_origin(self):
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request('GET', '/api/chat/stream')
        self.assertEqual(connection.getresponse().status, 404)
        connection.close()
        connection = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        connection.request('GET', '/api/chat/stream', headers={'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(connection.getresponse().status, 403)
        connection.close()


if __name__ == '__main__':
    unittest.main()
