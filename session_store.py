"""Validated, revisioned search sessions for the local app (no external services)."""
from copy import deepcopy
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
from hashlib import sha256
import math
import secrets
import sqlite3

SCHEMA_VERSION = 2
DEFAULT_CRITERIA = {
    'origin_a': {'name': '東京駅', 'lat': 35.681236, 'lon': 139.767125},
    'origin_b': None, 'radius_a_km': 3, 'radius_b_km': 5,
    'restrict_b': False, 'max_price_yen': None, 'min_area_m2': None,
    'max_age_years': None, 'layout': '', 'property_type': '',
    'pets': False, 'parking': False, 'inside_only': True, 'sort': 'distance',
    'listing_area': '', 'listing_keywords': '', 'listing_provider': 'auto',
    'listing_only_within': True,
}
# This is also exposed as the update contract for the next conversational layer.
PLACE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['name', 'lat', 'lon'],
    'properties': {
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 300, 'pattern': r'\S'},
        'lat': {'type': 'number', 'minimum': -85, 'maximum': 85},
        'lon': {'type': 'number', 'minimum': -180, 'maximum': 180},
    },
}
CRITERIA_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'origin_a': PLACE_SCHEMA, 'origin_b': {'anyOf': [PLACE_SCHEMA, {'type': 'null'}]},
        'radius_a_km': {'type': 'number', 'minimum': .5, 'maximum': 25, 'multipleOf': .5},
        'radius_b_km': {'type': 'number', 'minimum': .5, 'maximum': 25, 'multipleOf': .5},
        'max_price_yen': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 10**11},
        'min_area_m2': {'type': ['number', 'null'], 'minimum': 0, 'maximum': 10**6},
        'max_age_years': {'type': ['integer', 'null'], 'minimum': 0, 'maximum': 300},
        **{key: {'type': 'boolean'} for key in ('restrict_b', 'pets', 'parking', 'inside_only', 'listing_only_within')},
        'layout': {'type': 'string', 'enum': ['', '1R', '1K', '1DK', '1LDK', '2K', '2DK', '2LDK', '3LDK']},
        'property_type': {'type': 'string', 'enum': ['', 'rent', 'sale']},
        'sort': {'type': 'string', 'enum': ['distance', 'price', 'area']},
        'listing_provider': {'type': 'string', 'enum': ['auto', 'bing', 'brave']},
        'listing_area': {'type': 'string', 'maxLength': 90},
        'listing_keywords': {'type': 'string', 'maxLength': 65},
    },
}
UPDATE_SCHEMA = {
    'type': 'object', 'additionalProperties': False, 'required': ['expected_revision'],
    'properties': {
        'expected_revision': {'type': 'integer', 'minimum': 0},
        'criteria': CRITERIA_SCHEMA,
        'draft_message': {'type': 'string', 'maxLength': 2000},
    },
}


def validate_patch(patch):
    if not isinstance(patch, dict) or set(patch) - CRITERIA_SCHEMA['properties'].keys():
        raise ValueError('Unknown search criteria')
    for key, value in patch.items():
        spec = CRITERIA_SCHEMA['properties'][key]
        if value is None and (key == 'origin_b' or 'null' in spec.get('type', [])):
            continue
        kind = spec.get('type')
        if isinstance(kind, list):
            kind = kind[0]
        if key in ('origin_a', 'origin_b'):
            if not isinstance(value, dict) or set(value) != {'name', 'lat', 'lon'}:
                raise ValueError('Invalid place')
            if not isinstance(value['name'], str) or not 1 <= len(value['name'].strip()) <= 300:
                raise ValueError('Invalid place name')
            for coordinate, bound in [('lat', 85), ('lon', 180)]:
                n = value[coordinate]
                if type(n) not in (int, float) or not math.isfinite(n) or abs(n) > bound:
                    raise ValueError('Invalid coordinates')
        elif kind in ('number', 'integer'):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError('Invalid number: ' + key)
            if kind == 'integer' and int(value) != value:
                raise ValueError('Expected integer: ' + key)
            if not spec['minimum'] <= value <= spec['maximum']:
                raise ValueError('Out of range: ' + key)
            if 'multipleOf' in spec and not math.isclose(value / spec['multipleOf'], round(value / spec['multipleOf'])):
                raise ValueError('Invalid increment: ' + key)
        elif kind == 'boolean':
            if type(value) is not bool:
                raise ValueError('Expected boolean: ' + key)
        elif kind == 'string':
            if not isinstance(value, str) or len(value) > spec.get('maxLength', 300):
                raise ValueError('Invalid text: ' + key)
            if 'enum' in spec and value not in spec['enum']:
                raise ValueError('Invalid choice: ' + key)
    return deepcopy(patch)


class RevisionConflict(Exception):
    def __init__(self, session):
        self.session = session


class SessionStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS archived_chats (owner TEXT NOT NULL, id TEXT NOT NULL, title TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(owner,id))')
                yield db
        finally:
            db.close()

    @staticmethod
    def upgrade(session):
        session['schema_version'] = SCHEMA_VERSION
        session.setdefault('chat_id', 'chat_' + sha256(session['created_at'].encode()).hexdigest()[:24])
        for key, value in {
            'conversation': [], 'memory': {'facts': {}, 'places': {}}, 'result_sets': [],
            'saved_candidates': [], 'history': [], 'chat': {'status': 'idle'},
            '_agent': {}, '_tool_results': {},
        }.items():
            session.setdefault(key, deepcopy(value))
        return session

    def mutate(self, session_id, operation):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise KeyError('Session not found')
            session = self.upgrade(json.loads(row[0]))
            result = operation(session)
            session['updated_at'] = datetime.now(timezone.utc).isoformat()
            db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(session), session_id))
        return session, result

    def get(self, session_id):
        with self.connect() as db:
            row = db.execute('SELECT data FROM sessions WHERE id=?', (session_id,)).fetchone()
        return self.upgrade(json.loads(row[0])) if row else None

    def create(self):
        session_id = secrets.token_urlsafe(32)
        now = datetime.now(timezone.utc).isoformat()
        session = {'schema_version': SCHEMA_VERSION, 'revision': 0,
                   'criteria': deepcopy(DEFAULT_CRITERIA), 'draft_message': '',
                   'created_at': now, 'updated_at': now}
        self.upgrade(session)
        with self.connect() as db:
            db.execute('INSERT INTO sessions VALUES (?, ?)', (session_id, json.dumps(session)))
        return session_id, session

    def update(self, session_id, payload):
        if not isinstance(payload, dict) or set(payload) - {'expected_revision', 'criteria', 'draft_message'}:
            raise ValueError('Invalid session update')
        revision = payload.get('expected_revision')
        if type(revision) is not int or revision < 0:
            raise ValueError('expected_revision is required')
        patch = validate_patch(payload.get('criteria', {}))
        if 'draft_message' in payload and (not isinstance(payload['draft_message'], str) or len(payload['draft_message']) > 2000):
            raise ValueError('Draft exceeds 2000 characters')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM sessions WHERE id=?', (session_id,)).fetchone()
            if not row:
                raise KeyError('Session not found')
            session = self.upgrade(json.loads(row[0]))
            if session['chat'].get('status') == 'resetting':
                raise RevisionConflict(session)
            if revision != session['revision']:
                raise RevisionConflict(session)
            criteria = {**session['criteria'], **patch}
            draft = payload.get('draft_message', session['draft_message'])
            if criteria != session['criteria'] or draft != session['draft_message']:
                if criteria != session['criteria']:
                    session['history'] = (session['history'] + [deepcopy(session['criteria'])])[-20:]
                session.update(criteria=criteria, draft_message=draft, revision=revision + 1,
                               updated_at=datetime.now(timezone.utc).isoformat())
                db.execute('UPDATE sessions SET data=? WHERE id=?', (json.dumps(session), session_id))
        return session


    @staticmethod
    def chat_title(session):
        first = next((m['text'] for m in session.get('conversation', []) if m['role'] == 'user'), '')
        return ' '.join(first.split())[:80]

    def list_chats(self, owner):
        current = self.get(owner)
        if not current:
            raise KeyError('Session not found')
        with self.connect() as db:
            rows = db.execute('SELECT id,title,updated_at FROM archived_chats WHERE owner=? AND id!=? ORDER BY updated_at DESC', (owner,current['chat_id'])).fetchall()
        return [{'id':current['chat_id'],'title':self.chat_title(current),'updated_at':current['updated_at'],'active':True}] + [
            {'id':row[0],'title':row[1],'updated_at':row[2],'active':False} for row in rows]

    def switch_chat(self, owner, revision, chat_id=None):
        if type(revision) is not int:
            raise ValueError('Revision required')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM sessions WHERE id=?',(owner,)).fetchone()
            if not row:
                raise KeyError('Session not found')
            current = self.upgrade(json.loads(row[0]))
            if current['revision'] != revision or current['chat'].get('status') in ('running','cancelling','resetting') or (current['chat'].get('status') == 'error' and current['chat'].get('recoverable')):
                raise RevisionConflict(current)
            if chat_id == current['chat_id']:
                return current
            if chat_id:
                target = db.execute('SELECT data FROM archived_chats WHERE owner=? AND id=?',(owner,chat_id)).fetchone()
                if not target:
                    raise ValueError('Unknown chat')
                selected = self.upgrade(json.loads(target[0]))
                db.execute('DELETE FROM archived_chats WHERE owner=? AND id=?',(owner,chat_id))
            else:
                now = datetime.now(timezone.utc).isoformat()
                selected = self.upgrade({'schema_version':SCHEMA_VERSION,'revision':0,'criteria':deepcopy(DEFAULT_CRITERIA),
                                         'draft_message':'','created_at':now,'updated_at':now})
            db.execute('INSERT OR REPLACE INTO archived_chats VALUES (?,?,?,?,?)',
                       (owner,current['chat_id'],self.chat_title(current),json.dumps(current),current['updated_at']))
            selected['revision'] = current['revision'] + 1
            selected['updated_at'] = datetime.now(timezone.utc).isoformat()
            db.execute('UPDATE sessions SET data=? WHERE id=?',(json.dumps(selected),owner))
        return selected


def public_session(session):
    """Provider IDs, idempotency records and tool journals never go to the browser."""
    result = {key: value for key, value in session.items() if not key.startswith('_')}
    result['chat'] = {key: value for key, value in session.get('chat', {}).items()
                      if key in ('status', 'request_id', 'progress', 'error', 'message', 'recoverable', 'operation', 'reset_search')}
    return result
