#!/usr/bin/env python3
"""Local property-portal search bridge.

- Keyless discovery: Bing web-search RSS (best-effort, may be unavailable/rate-limited).
- Optional official search provider: Brave Search API, configured in environment.
- Never claims indexing results are currently vacant or the coordinates exact.
- Doesn't crawl or scrape property sites themselves.

Start: python3 property-server.py ; visit http://127.0.0.1:8765/
Optional: BRAVE_SEARCH_API_KEY=... python3 property-server.py
"""
from __future__ import annotations

from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
from copy import deepcopy
from hashlib import sha256
from uuid import uuid4
from html import unescape
from datetime import datetime, timezone
from contextlib import contextmanager
import xml.etree.ElementTree as ET
import os, json, re, math, threading, time
from listing_feed import normalize_feed
from session_store import SessionStore, RevisionConflict, open_store, CRITERIA_SCHEMA, UPDATE_SCHEMA, SCHEMA_VERSION, public_session
from home_agent import HomeAgent, AgentError
from listing_details import read_details
from suumo_bridge import SuumoError, search as search_suumo, validate_search, validate_filters, validate_page, RENT_STEPS, PAGE_SIZE as SUUMO_PAGE_SIZE
import public_housing
import web_search
import page_verify

BASE = Path(__file__).resolve().parent


def load_env(path):
    """Load simple KEY=value settings without overriding the process environment."""
    if not path.is_file():
        return
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:].lstrip()
        key, separator, value = line.partition('=')
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
            continue
        if value.startswith(('"', "'")):
            quote = value[0]
            end = value.find(quote, 1)
            remainder = value[end + 1:].strip() if end != -1 else ''
            if end == -1 or (remainder and not remainder.startswith('#')):
                continue
            value = value[1:end]
        else:
            value = value.split(' #', 1)[0].rstrip()
        os.environ.setdefault(key, value)


load_env(BASE / '.env')
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
SEARCH_KEY = os.environ.get('BRAVE_SEARCH_API_KEY', '').strip()
FEED_PATH = os.environ.get('LISTINGS_FEED_PATH', '').strip()
FEED_URL = os.environ.get('LISTINGS_FEED_URL', '').strip()
SUUMO_REUSE_PERMITTED = os.environ.get('SUUMO_REUSE_PERMITTED', '') == '1'
PORT = int(os.environ.get('PORT', '8765'))
ALLOWED_DOMAINS = ('suumo.jp', 'homes.co.jp', 'athome.co.jp')
USER_AGENT = 'PropertyFinderLocal/2.0 (+browser-side user initiated search)'
CACHE_TTL = 120
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_suumo_cache: dict[tuple[float, float, float, float | None, str | None, int], tuple[float, dict]] = {}
MAX_BYTES = 1_000_000
STREAM_SECONDS = 270 if os.environ.get('VERCEL') else 600
DAILY_TURNS = int(os.environ.get('AGENT_DAILY_TURN_LIMIT') or 0)  # 0 = unlimited (local default)
SESSION_STORE = open_store(BASE / '.local' / 'sessions.sqlite3')
VALHALLA_URL = os.environ.get('VALHALLA_URL', 'https://valhalla1.openstreetmap.de').rstrip('/')
WALKING_SPEED_KMH = 4.8
WALKING_FERRY_BOARDING_SECONDS = 43200
WALKING_CACHE_TTL = 900
_walking_cache: dict[tuple, tuple[float, dict]] = {}
_walking_cache_lock = threading.Lock()
_walking_lock = threading.Lock()
_walking_last_request = 0.0

_route_lock = threading.Lock()
_last_route_request = 0.0


def load_feed():
    if FEED_PATH:
        with open(FEED_PATH, 'rb') as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError('Feed exceeds 1 MB')
        data = json.loads(raw)
    elif FEED_URL:
        parsed = urlparse(FEED_URL)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Feed URL must use HTTPS')
        data = json.loads(get_text(FEED_URL))
    else:
        raise ValueError('No permitted listing feed configured')
    return normalize_feed(data.get('records') if isinstance(data, dict) else data)


def route_time(payload):
    global _last_route_request
    mode = payload.get('mode')
    if mode not in ('foot', 'bike', 'car'):
        raise ValueError('Unsupported route mode')
    coordinates = []
    for name in ('origin', 'destination'):
        point = payload.get(name)
        if not isinstance(point, dict):
            raise ValueError('Coordinates required')
        latitude, longitude = point.get('lat'), point.get('lon')
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (latitude, longitude)):
            raise ValueError('Invalid coordinates')
        if abs(latitude) > 85 or abs(longitude) > 180:
            raise ValueError('Invalid coordinates')
        coordinates.append(f'{longitude:.6f},{latitude:.6f}')
    with _route_lock:
        delay = max(0, 1.1 - (time.monotonic() - _last_route_request))
        if delay:
            time.sleep(delay)
        _last_route_request = time.monotonic()
    url = f'https://routing.openstreetmap.de/routed-{mode}/route/v1/{mode}/' + ';'.join(coordinates)
    try:
        data = json.loads(get_text(url + '?overview=false&steps=false&radiuses=100;100', timeout=15))
    except HTTPError as error:
        if error.code != 400:
            raise
        with error:
            data = json.loads(error.read(10000))
        if data.get('code') not in ('NoRoute', 'NoSegment'):
            raise
    if not isinstance(data, dict):
        raise ValueError('Invalid route response')
    routes = data.get('routes', [])
    if data.get('code') != 'Ok' or not routes:
        return {'status': 'unavailable', 'mode': mode, 'duration_seconds': None, 'reason': data.get('code')}
    if not isinstance(routes, list) or not isinstance(routes[0], dict):
        raise ValueError('Invalid route response')
    duration, distance = routes[0].get('duration'), routes[0].get('distance')
    if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 for value in (duration, distance)):
        raise ValueError('Invalid route response')
    return {'status': 'ok', 'mode': mode, 'duration_seconds': duration, 'distance_meters': distance,
            'checked_at': datetime.now(timezone.utc).isoformat(), 'source': 'FOSSGIS OSRM / OpenStreetMap',
            'source_url': 'https://routing.openstreetmap.de/about.html', 'live_traffic': False}


def get_text(url: str, headers: dict | None = None, timeout=12, max_bytes=MAX_BYTES) -> str:
    req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/json;q=0.8', **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        body = response.read(max_bytes + 1)
        if len(body) > max_bytes:
            raise ValueError('検索結果が大きすぎます')
        encoding = response.headers.get_content_charset() or 'utf-8'
        return body.decode(encoding, errors='replace')


def walking_parameters(lat, lon, minutes):
    """Reject invalid coordinates and unsupported times before making a request."""
    try:
        lat, lon, minutes = float(lat), float(lon), float(minutes)
    except (ValueError, TypeError):
        raise ValueError('緯度・経度・徒歩時間は数値で指定してください') from None
    if not all(math.isfinite(v) for v in (lat, lon, minutes)):
        raise ValueError('緯度・経度・徒歩時間が不正です')
    if not -85 <= lat <= 85 or not -180 <= lon <= 180:
        raise ValueError('緯度・経度が範囲外です')
    if not 5 <= minutes <= 60 or not minutes.is_integer():
        raise ValueError('徒歩時間は5〜60分の整数で指定してください')
    return round(lat, 6), round(lon, 6), int(minutes)


def walking_distance_parameters(lat, lon, distance_km):
    lat, lon, _ = walking_parameters(lat, lon, 15)
    try:
        if isinstance(distance_km, bool):
            raise ValueError
        distance_km = float(distance_km)
    except (ValueError, TypeError):
        raise ValueError('徒歩距離はkm単位の数値で指定してください') from None
    if not math.isfinite(distance_km) or not 0.1 <= distance_km <= 5:
        raise ValueError('徒歩距離は0.1〜5kmで指定してください')
    return lat, lon, round(distance_km, 3)


def walking_geometry(data, limit, metric='time'):
    """Accept only a matching polygon contour, including holes and islands."""
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection':
        raise ValueError('徒歩圏の応答が不正です')
    for feature in data.get('features', []):
        if not isinstance(feature, dict):
            continue
        properties = feature.get('properties')
        if not isinstance(properties, dict):
            continue
        contour = properties.get('contour')
        if properties.get('metric') != metric or isinstance(contour, bool) or not isinstance(contour, (int, float)) or not math.isclose(contour, limit, rel_tol=1e-6):
            continue
        geometry = feature.get('geometry')
        if not isinstance(geometry, dict):
            continue
        kind, coordinates = geometry.get('type'), geometry.get('coordinates')
        if kind not in ('Polygon', 'MultiPolygon') or not isinstance(coordinates, list) or not coordinates:
            continue
        polygons = [coordinates] if kind == 'Polygon' else coordinates
        for polygon in polygons:
            if not isinstance(polygon, list) or not polygon:
                raise ValueError('徒歩圏の多角形が不正です')
            for ring in polygon:
                if not isinstance(ring, list) or len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError('徒歩圏の境界が不正です')
                for point in ring:
                    if not isinstance(point, list) or len(point) != 2 or any(
                        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point
                    ) or not -180 <= point[0] <= 180 or not -85 <= point[1] <= 85:
                        raise ValueError('徒歩圏の座標が不正です')
        return {'type': kind, 'coordinates': coordinates}
    raise ValueError('指定条件の徒歩圏がありません')


def walking_network(data, limit, metric='time'):
    """Only fully reached pedestrian edges, never the interpolated land surface.

    Expansion includes search-frontier edges beyond the requested limit. Keep
    settled edges within the cumulative duration or distance budget.
    """
    if not isinstance(data, dict) or data.get('type') != 'FeatureCollection' or not isinstance(data.get('features'), list):
        raise ValueError('徒歩道路網の応答が不正です')
    lines = []
    for feature in data['features']:
        if not isinstance(feature, dict):
            continue
        properties, geometry = feature.get('properties'), feature.get('geometry')
        if not isinstance(properties, dict) or not isinstance(geometry, dict):
            continue
        value = properties.get('duration' if metric == 'time' else 'distance')
        budget = limit * (60 if metric == 'time' else 1000)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            continue
        if properties.get('edge_status') != 's' or not 0 <= value <= budget:
            continue
        if metric == 'distance':
            # The distance budget alone does not exclude ferries. The artificial
            # boarding duration marks ferry paths and their descendants too.
            duration = properties.get('duration')
            if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 0 <= duration < WALKING_FERRY_BOARDING_SECONDS:
                continue
        points = geometry.get('coordinates')
        if geometry.get('type') != 'LineString' or not isinstance(points, list) or len(points) < 2:
            raise ValueError('徒歩道路網の形状が不正です')
        for point in points:
            if not isinstance(point, list) or len(point) != 2 or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in point
            ) or not -180 <= point[0] <= 180 or not -85 <= point[1] <= 85:
                raise ValueError('徒歩道路網の座標が不正です')
        lines.append(points)
    if not lines:
        raise ValueError('指定条件内に到達した徒歩道路がありません')
    return {'type': 'MultiLineString', 'coordinates': lines}


@contextmanager
def walking_request_slot():
    # Keep queued slider requests from outliving the browser's 120s timeout.
    if not _walking_lock.acquire(timeout=10):
        raise TimeoutError('Walking service is busy')
    try:
        yield
    finally:
        _walking_lock.release()


def fetch_walking_area(lat, lon, minutes=None, *, distance_km=None):
    """Pedestrian road-network isochrone. Never substitute a distance circle."""
    global _walking_last_request
    if (minutes is None) == (distance_km is None):
        raise ValueError('徒歩時間または徒歩距離を1つだけ指定してください')
    if distance_km is not None:
        lat, lon, limit = walking_distance_parameters(lat, lon, distance_km)
        metric = 'distance'
    else:
        lat, lon, limit = walking_parameters(lat, lon, minutes)
        metric = 'time'
    key = (lat, lon, metric, limit)
    def cached_area():
        with _walking_cache_lock:
            cached = _walking_cache.get(key)
            if cached and time.monotonic() - cached[0] < WALKING_CACHE_TTL:
                return {**cached[1], 'cached': True}
        return None
    # A slow, large-area request must not block already cached smaller areas.
    cached = cached_area()
    if cached is not None:
        return cached
    # Serialize public-service calls, deduplicate concurrent requests, and cache
    # results so slider changes and repeat visits do not burden the demo service.
    with walking_request_slot():
        cached = cached_area()
        if cached is not None:
            return cached
        now = time.monotonic()
        wait = max(0, 1 - (now - _walking_last_request))
        if wait:
            time.sleep(wait)
        payload = {
            'locations': [{'lat': lat, 'lon': lon, 'search_filter': {'exclude_ferry': True}}],
            'costing': 'pedestrian',
            # Public servers may disable hard exclusions. A boarding time above
            # our time limit prevents ferry travel; distance results also reject
            # paths with this boarding duration. Do not snap origins to ferries.
            'costing_options': {'pedestrian': {'walking_speed': WALKING_SPEED_KMH,
                                                'use_ferry': 0, 'ferry_cost': WALKING_FERRY_BOARDING_SECONDS}},
            'contours': [{metric: limit}], 'polygons': True,
            'denoise': 0.5, 'generalize': 25,
        }
        _walking_last_request = time.monotonic()
        headers = {
            'Accept': 'application/json',
            'X-Client-Id': 'github.com/syun88/DevDay_Hackathon_search_home',
        }
        data = json.loads(get_text(VALHALLA_URL + '/isochrone?' + urlencode({
            'json': json.dumps(payload, separators=(',', ':')),
        }), headers=headers, timeout=30))
        geometry = walking_geometry(data, limit, metric)
        time.sleep(max(0, 1 - (time.monotonic() - _walking_last_request)))
        _walking_last_request = time.monotonic()
        expansion = json.loads(get_text(VALHALLA_URL + '/expansion?' + urlencode({
            'json': json.dumps({**payload, 'action': 'isochrone', 'skip_opposites': True,
                                'dedupe': True, 'expansion_properties': ['duration', 'edge_status'] + (['distance'] if metric == 'distance' else [])},
                               separators=(',', ':')),
        }), headers=headers, timeout=60, max_bytes=16_000_000))
        result = {
            'geometry': geometry, 'network': walking_network(expansion, limit, metric), 'metric': metric,
            **({'minutes': limit} if metric == 'time' else {'distance_km': limit}),
            'origin': {'lat': lat, 'lon': lon}, 'walking_speed_kmh': WALKING_SPEED_KMH,
            'source': 'Valhalla / OpenStreetMap',
            'attribution': '© OpenStreetMap contributors',
        }
        with _walking_cache_lock:
            if len(_walking_cache) >= 12:
                _walking_cache.clear()
            _walking_cache[key] = (time.monotonic(), result)
        return result


def clean(value, limit=500):
    if not isinstance(value, str):
        return ''
    s = re.sub(r'<[^>]+>', ' ', unescape(value))
    return re.sub(r'\s+', ' ', s).strip()[:limit]


def portal_host(url: str) -> str | None:
    try:
        p = urlparse(url)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443):
            return None
        if any(host == d or host.endswith('.' + d) for d in ALLOWED_DOMAINS):
            return host
    except (ValueError, TypeError):
        return None
    return None


def canonical_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse(('https', p.netloc.lower(), p.path.rstrip('/') or '/', '', '', ''))


PREFECTURES = '(?:北海道|東京都|京都府|大阪府|青森県|岩手県|宮城県|秋田県|山形県|福島県|茨城県|栃木県|群馬県|埼玉県|千葉県|神奈川県|新潟県|富山県|石川県|福井県|山梨県|長野県|岐阜県|静岡県|愛知県|三重県|滋賀県|兵庫県|奈良県|和歌山県|鳥取県|島根県|岡山県|広島県|山口県|徳島県|香川県|愛媛県|高知県|福岡県|佐賀県|長崎県|熊本県|大分県|宮崎県|鹿児島県|沖縄県)'
ADDRESS_RE = re.compile(r'(?:所在地[：:]?\s*|住所[：:]?\s*)?(' + PREFECTURES + r'[\w\-－−丁目番地号・／/\u3000 ]{4,84})')
PRICE_RE = re.compile(r'(?:賃料|家賃|月額)[：:\s]*([\d,]+(?:\.\d+)?)\s*(万\s*円|円)')
PRICE_FALLBACK = re.compile(r'(?<!\d)(\d{1,3}(?:\.\d{1,2})?)\s*万\s*円(?:/月|／月|\s*月)?')
LAYOUT_RE = re.compile(r'(?<![A-Z0-9])(ワンルーム|[1-5](?:R|K|DK|LDK))(?![A-Z])', re.IGNORECASE)
AREA_RE = re.compile(r'(?<!\d)(\d{1,3}(?:\.\d{1,2})?)\s*(?:㎡|m²|m2|平米)')
AGE_RE = re.compile(r'築\s*(\d{1,2})\s*年')


def parse_fields(title: str, description: str, kind: str) -> dict:
    # Snippet heuristics are deliberately labelled as unverified in the UI.
    snippet = clean(title + ' ' + description, 1500)
    price = None
    price_unit = '万円/月' if kind != 'sale' else '万円'
    match = PRICE_RE.search(snippet)
    if match and kind != 'sale':
        n = float(match.group(1).replace(',', ''))
        price = round(n / 10000 if match.group(2) == '円' else n, 2)
    elif kind != 'sale':
        match = PRICE_FALLBACK.search(snippet)
        if match:
            n = float(match.group(1))
            if 0.2 <= n <= 150: price = n
    elif kind == 'sale':
        match = re.search(r'(?<!\d)([\d,]+)\s*万\s*円', snippet)
        if match:
            n = float(match.group(1).replace(',', ''))
            if 100 <= n < 1e7: price = n
    match = LAYOUT_RE.search(snippet)
    layout = (match.group(1).upper().replace('ワンルーム', '1R') if match else None)
    match = AREA_RE.search(snippet)
    area = float(match.group(1)) if match and float(match.group(1)) < 2000 else None
    match = AGE_RE.search(snippet)
    age = int(match.group(1)) if match else None
    match = ADDRESS_RE.search(snippet)
    address = None
    if match:
        # Avoid incorporating sentences and extra promotional text as address.
        possible = match.group(1).strip(' /・　,.。')
        possible = re.split(r'\s*(?:交通|間取り|賃料|家賃|物件情報|特徴|築|詳細|アクセス|駅徒歩|地図|徒歩\d+分)', possible, maxsplit=1)[0]
        # A prefecture+municipality without number may be geocoded only approximately.
        address = possible[:95]
    return {'price': price, 'price_unit': price_unit, 'layout': layout, 'area': area, 'age': age, 'address': address}


def normalize_results(raw: list[dict], kind='rent') -> list[dict]:
    results, seen = [], set()
    for item in raw:
        url = str(item.get('url', ''))
        source = portal_host(url)
        if not source:
            continue
        norm = canonical_url(url)
        if norm in seen: continue
        seen.add(norm)
        title = clean(item.get('title', ''), 180)
        description = clean(item.get('description', ''), 550)
        if not title: continue
        results.append({'title':title, 'description':description, 'url':url, 'source':source,
                        'extracted': parse_fields(title, description, kind),
                        'data_quality':'検索結果の抜粋から推定・未確認', 'retrieved_at':datetime.now(timezone.utc).isoformat(),
                        'availability_status':'unknown', 'geo_precision':'unknown', 'kind':kind,
                        'last_verified_at':None})
        if len(results) >= 30:break
    return results


def parse_bing_rss(xml: str) -> list[dict]:
    root = ET.fromstring(xml)
    if root.tag.lower().endswith('html'):
        raise ValueError('BingがRSSの代わりにHTMLを返しました（利用制限の可能性）')
    records = []
    for item in root.findall('.//item'):
        records.append({'title':item.findtext('title') or '', 'url':item.findtext('link') or '',
                        'description':item.findtext('description') or ''})
    return records


def bing_rss(query: str) -> list[dict]:
    url = 'https://www.bing.com/search?' + urlencode({'q':query, 'format':'rss', 'setlang':'ja-JP'})
    return parse_bing_rss(get_text(url, timeout=14))


def brave_search(query: str) -> list[dict]:
    if not SEARCH_KEY:
        raise ValueError('BRAVE_SEARCH_API_KEY が未設定です')
    url = 'https://api.search.brave.com/res/v1/web/search?' + urlencode({
        'q':query, 'country':'jp', 'search_lang':'ja', 'count':20})
    doc = json.loads(get_text(url, headers={'Accept':'application/json', 'X-Subscription-Token': SEARCH_KEY}, timeout=16))
    return [{'title':x.get('title',''), 'url':x.get('url',''), 'description':x.get('description','')}
            for x in doc.get('web',{}).get('results',[])]


def make_queries(place: str, kind: str, details: dict) -> list[str]:
    query_type = '中古マンション 売買' if kind == 'sale' else '賃貸 マンション アパート'
    focus = [place, query_type]
    if details.get('layout'): focus.append(details['layout'])
    # Limit filters to keep listings found; numerical filters are indicative, not verified.
    if details.get('keywords'):focus.append(details['keywords'])
    target = ' '.join(focus)
    domain = '(site:suumo.jp OR site:homes.co.jp OR site:athome.co.jp)'
    return [f'{target} {domain}', f'{target} site:suumo.jp']


def fetch_listings(place: str, kind: str, details: dict, provider='auto') -> dict:
    qs = make_queries(place, kind, details)
    engines = ['brave','bing'] if provider == 'auto' and SEARCH_KEY else ['bing'] if provider == 'auto' else [provider]
    errors = []
    for engine in engines:
        for i,query in enumerate(qs):
            try:
                raw = brave_search(query) if engine == 'brave' else bing_rss(query)
                listings = normalize_results(raw, kind)
                if listings:
                    return {'results':listings, 'query':query, 'provider':engine,
                            'checked_at':datetime.now(timezone.utc).isoformat(),
                            'note':'検索エンジンが見つけたサイトの抜粋です。掲載終了・条件不一致が含まれます。'}
                errors.append(f'{engine}: 関連サイトの結果が0件')
            except (ValueError, ET.ParseError, HTTPError, URLError, OSError, TimeoutError, json.JSONDecodeError) as e:
                errors.append(f'{engine}: {type(e).__name__} {str(e)[:110]}')
            if i == 0 and provider == 'auto' and engine == 'brave':
                # Try Bing instead of spending two Brave API calls on an empty set.
                break
    return {'results':[], 'provider':'none', 'query':qs[0], 'errors':errors,
            'note':'検索先が0件または利用制限中です。公式サイトで検索するかBrave APIを設定してください。'}




def decision_classify(title: str, description: str) -> dict:
    """Optional user-triggered classification; not listing/availability verification."""
    if not OPENAI_KEY:
        raise ValueError('OPENAI_API_KEY が未設定です')
    text = f"タイトル: {clean(title,180)}\n抜粋: {clean(description,600)}"
    request_body = {
        'model':'gpt-6-luna',
        'input':text,
        'questions':[{
            'type':'choice', 'name':'page_kind',
            'instructions':'検索抜粋の種類を分類してください。空室状況や最新性は推定せず、具体的な一住戸の条件が十分ある場合だけdetailとする。',
            'choices':[
                {'value':'detail','description':'単一住戸の家賃・間取り等が書かれた具体的な掲載'},
                {'value':'list','description':'物件の一覧、検索ページ、地域情報'},
                {'value':'other','description':'物件掲載ではない、または情報不足'}
            ]
        }]
    }
    body = json.dumps(request_body,ensure_ascii=False).encode('utf-8')
    req=Request('https://api.openai.com/v1/decisions',data=body,headers={
        'Authorization':'Bearer '+OPENAI_KEY,'Content-Type':'application/json','User-Agent':USER_AGENT},method='POST')
    with urlopen(req,timeout=14) as resp:
        result=json.loads(resp.read(100_000))
    answer=next((x for x in result.get('answers',[]) if x.get('name')=='page_kind' and x.get('type')=='choice'),None)
    if not answer:
        return {'choice':'unknown', 'confidence':None}
    choice=answer.get('choice')
    if isinstance(choice,dict):choice=choice.get('value')
    if choice not in ('detail','list','other'):choice='unknown'
    return {'choice':choice,'confidence':answer.get('confidence'),'is_verified':False}

def agent_listings(place, kind, details, provider='auto'):
    if provider == 'feed' or (provider == 'auto' and (FEED_PATH or FEED_URL)):
        result = load_feed()
        result['results'] = [r for r in result['results'] if r['kind'] == kind]
        result['inventory_status'] = 'configured_verified_feed'
        return result
    # Discovery is useful even without verified vacancy inventory. Keep leads separate
    # from matching candidates and never reinterpret fetched_at as verified_at.
    result = {'results': [], 'leads': [], 'discovery_links': [], 'errors': [],
              'provider': 'discovery', 'inventory_status': 'no_verified_feed_configured'}
    origin = details.get('origin', {})
    if provider == 'auto' and kind == 'rent' and origin and SUUMO_REUSE_PERMITTED:
        try:
            radius = min(details.get('radius_km', 2), 2)
            lat, lon, radius = validate_search(origin['lat'], origin['lon'], radius)
            budget = details.get('max_price_yen')
            # The upstream slider is coarse. Search the next supported step and
            # reject known over-budget subtotals locally, without changing criteria.
            cap = next((x for x in RENT_STEPS if x * 10000 >= budget), None) if budget is not None else None
            layout = details.get('layout') or None
            validate_filters(cap, layout)
            raw = search_suumo(lat, lon, radius, cap, layout)
            result['coverage'] = {'source': 'SUUMO map prototype', 'origin': origin,
                                  'requested_radius_km': details.get('radius_km'), 'query_radius_km': radius,
                                  'max_inspected': SUUMO_PAGE_SIZE, 'page': 1, 'within_radius_verified': False,
                                  'citywide': False, 'supports_detached_house_filter': False}
            for row in raw['results']:
                known = row.get('known_monthly_subtotal_jpy')
                # Even when one fee is missing, known rent alone is a lower bound.
                lower_bound = (row.get('rent_jpy') or 0) + (row.get('management_fee_jpy') or 0)
                if budget is not None and lower_bound > budget: continue
                result['leads'].append({
                    'title': row.get('name') or 'SUUMO ' + row['source_id'], 'url': row['source_url'], 'source': 'SUUMO',
                    'kind': 'rent', 'discovery_only': True, 'availability_status': 'unknown',
                    'last_verified_at': None, 'retrieved_at': raw['fetched_at'],
                    'lat': None, 'lon': None, 'geo_precision': 'unknown',
                    'known_monthly_subtotal_yen': known,
                    'extracted': {'price': row['rent_jpy']/10000 if row['rent_jpy'] is not None else None,
                                  'management_fee_yen':row['management_fee_jpy'], 'mandatory_fee_yen':None,
                                  'area':row['area_sqm'], 'layout':row['layout'], 'address':row['address'],
                                  **{k:row.get(k) or None for k in ('floor','age_text','transport','deposit','key_money')}},
                    'limitations': ['availability unknown', 'other monthly fees unknown', 'exact location and radius unverified',
                                    'building type and other requested requirements unverified']})
        except (ValueError, KeyError, TypeError):
            result['errors'].append('SUUMO prototype supports Tokyo origins only and selected layouts; no citywide inventory is available.')
        except SuumoError as exc:
            result['errors'].append('SUUMO discovery unavailable: ' + exc.code)
    if not SUUMO_REUSE_PERMITTED:
        result['errors'].append('SUUMO reuse is not configured; use licensed public layers or source search links.')
    if not result['leads']:
        discovered = fetch_listings(place, kind, details, provider)
        result['discovery_links'] = [{'url': r['url'], 'source': r['source']} for r in discovered.get('results', [])]
        result['errors'].extend(discovered.get('errors', []))
    # Always give a working source search path, even when an index returns nothing.
    query = ' '.join(str(x) for x in (place, '賃貸' if kind == 'rent' else '売買', details.get('layout'), details.get('keywords')) if x)
    result['source_search_links'] = [{'source': name, 'url': 'https://www.google.com/search?' + urlencode({'q': 'site:' + host + ' ' + query})}
                                     for name, host in [('SUUMO', 'suumo.jp'), ("HOME’S", 'homes.co.jp'), ('at home', 'athome.co.jp')]]
    return result


def permitted_details(url):
    if not SUUMO_REUSE_PERMITTED: raise ValueError('Source reuse is not configured; use known fields and official source link')
    return read_details(url)


def restricted_hosts():
    # Link-only sources: no values are extracted and their pages are not fetched.
    return web_search.NO_REUSE_HOSTS + (() if SUUMO_REUSE_PERMITTED else ('suumo.jp',))


def agent_web_search(query, context='', refine=False):
    # Same configured model as the agent. While SUUMO reuse is not permitted, SUUMO pages
    # stay navigable links only: no rent, address or other values are extracted from them.
    portals = web_search.RENTAL_PORTALS + (('suumo.jp',) if SUUMO_REUSE_PERMITTED else ())
    return web_search.search(OPENAI_KEY, HOME_AGENT.api.model, query, context, restricted_hosts(),
                             allowed_domains=portals if refine else None)


def agent_page_check(url, extracted):
    return page_verify.verify(url, extracted, restricted_hosts())


HOME_AGENT = HomeAgent(SESSION_STORE, OPENAI_KEY, agent_listings, router=route_time, detail_reader=permitted_details, public_search=public_housing.search, public_geocode=public_housing.geocode, web_search=agent_web_search, page_verifier=agent_page_check)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE), **kwargs)

    def send_head(self):
        # Serve only frontend assets, never .env, Git metadata, or source files.
        if urlparse(self.path).path not in ('/', '/index.html', '/i18n.js', '/session.js', '/workspace.js', '/workspace.css', '/chat.js', '/shell.js', '/shell.css', '/property_core.js', '/public_housing.js', '/map_agent.js', '/map_agent.css', '/walking.js'):
            self.send_error(404, 'Not found')
            return None
        return super().send_head()

    def respond(self, code: int, obj: dict, cookie=None):
        payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(payload)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            # Range changes and tab closes can disconnect even an error response.
            self.close_connection = True

    def session_id(self):
        try:
            cookies = SimpleCookie(self.headers.get('Cookie', ''))
            token = cookies.get('search_session')
            if token and re.fullmatch(r'[A-Za-z0-9_-]{43}', token.value):
                return token.value
        except CookieError:
            pass
        return None

    def remember_native_results(self, result):
        # Import/feed UI and chat share the same normalized records and stable IDs.
        owner = self.session_id()
        if not owner or not SESSION_STORE.get(owner): return result
        for item in result.get('results',[]):
            item['id']='listing_'+sha256((item['url']+'\0'+item.get('source_record_id','')).encode()).hexdigest()[:16]
        def save(s):
            snapshot={'id':'results_'+uuid4().hex[:16],'criteria':deepcopy(s['criteria']),
                      'items':deepcopy(result.get('results',[])[:30]),'provider':result.get('provider','feed'),
                      'created_at':datetime.now(timezone.utc).isoformat(),'note':'Imported via the original UI; source verification and unknown costs remain explicit.'}
            for item in snapshot['items']: item['match']=__import__('home_agent').evidence(item,s['criteria'])
            snapshot['counts']={'retrieved':len(snapshot['items']),'leads':0,'confirmed':sum(i['match']['status']=='confirmed' for i in snapshot['items'])}
            s['result_sets']=(s['result_sets']+[snapshot])[-5:]
        SESSION_STORE.mutate(owner,save)
        return result

    def update_session(self, action='session'):
        if not self.same_origin(writes=True):
            return self.respond(403, {'error': 'Same-origin request required'})
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'application/json':
            return self.respond(415, {'error': 'application/json required'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 2 <= size <= 16000:
                return self.respond(413, {'error': 'Invalid body size'})
            payload = json.loads(self.rfile.read(size))
            if not isinstance(payload, dict):
                raise ValueError('JSON object required')
            session_id = self.session_id()
            if not session_id or not SESSION_STORE.get(session_id):
                return self.respond(404, {'error': 'Session not found; reload the page'})
            if action == 'session':
                result = SESSION_STORE.update(session_id, payload)
            elif action == 'map':
                import map_agent
                refs = payload.get('public_records',[])
                if not isinstance(refs,list) or len(refs)>500: raise ValueError('Invalid public references')
                records=[]
                for ref in refs:
                    if not isinstance(ref,dict) or set(ref)!={'id','source'}: raise ValueError('Invalid reference')
                    item=next((r for r in public_housing.load(ref['source'])['records'] if r['id']==ref['id']),None)
                    if not item: raise ValueError('Unknown official record')
                    records.append({**item, **public_housing.cached_position(item['address'])})
                if 'public_version' in payload and type(payload['public_version']) is not int: raise ValueError('Invalid public version')
                def update_map(s):
                    stored = s.get('public_records', [])
                    key = lambda r: (r.get('source'), r['id'])
                    owned = {key(r): r for r in stored}
                    def keep_position(record):
                        # Coordinates validated earlier are owned session data; the
                        # in-memory geocode cache may have expired or restarted.
                        old = owned.get(key(record))
                        if old and old.get('lat') is not None and record.get('lat') is None:
                            return {**record, **{k: old[k] for k in ('lat', 'lon', 'geo_precision', 'geocode_label') if k in old}}
                        return record
                    incoming = [keep_position(r) for r in records]
                    version = s.get('public_version', 0)
                    if [key(r) for r in incoming] != [key(r) for r in stored] and payload.get('public_version', version) != version:
                        # A list built on an older version (before an agent load or a reset)
                        # must not replace the newer set; the browser adopts the stored one.
                        incoming = stored
                    outcome = map_agent.report({**s,'public_records':incoming},payload)
                    if not outcome.get('error'):
                        if [key(r) for r in incoming] != [key(r) for r in stored]:
                            s['public_version'] = version + 1
                        s['public_records']=incoming
                        # report mutates the nested owned map state and command.
                    return outcome
                result, outcome = SESSION_STORE.mutate(session_id, update_map)
                if outcome.get('error'): return self.respond(409,outcome)
            elif action == 'chat':
                current = SESSION_STORE.get(session_id)
                new_turn = not any(m['id'] == payload.get('request_id') for m in current['conversation'])
                # A shared daily allowance bounds spending on the server's API key when the app is public.
                if new_turn and DAILY_TURNS and not SESSION_STORE.take_quota('agent-turns-' + datetime.now(timezone.utc).strftime('%Y-%m-%d'), DAILY_TURNS):
                    return self.respond(429, {'error': 'daily_limit'})
                result = HOME_AGENT.start(session_id, payload)
            elif action == 'cancel':
                current = SESSION_STORE.get(session_id)
                if payload.get('request_id') != current['chat'].get('request_id'):
                    return self.respond(409, {'error': 'request_changed', 'session': public_session(current)})
                result = HOME_AGENT.cancel(session_id, payload.get('request_id'))
            elif action in ('new_chat', 'switch_chat'):
                if action == 'switch_chat' and not isinstance(payload.get('chat_id'), str):
                    raise ValueError('Chat ID required')
                result = SESSION_STORE.switch_chat(session_id, payload.get('expected_revision'), payload.get('chat_id') if action == 'switch_chat' else None)
            elif action == 'resume':
                result = HOME_AGENT.recover(session_id, payload.get('request_id'))
            elif action == 'reset':
                if (type(payload.get('expected_revision')) is not int
                        or type(payload.get('reset_search', False)) is not bool):
                    raise ValueError('Invalid reset')
                result = HOME_AGENT.reset(session_id, payload['expected_revision'], payload.get('reset_search', False))
            HOME_AGENT.notify(session_id)
            return self.respond(200, public_session(result))
        except RevisionConflict as exc:
            return self.respond(409, {'error': 'revision_conflict', 'session': public_session(exc.session)})
        except AgentError as exc:
            return self.respond(409 if str(exc) == 'busy' else 503, {'error': str(exc)})
        except (ValueError, UnicodeError):
            return self.respond(400, {'error': 'Invalid session update'})

    def origin_scheme(self):
        # Behind Vercel the platform terminates TLS; locally the server speaks plain HTTP.
        return 'https' if os.environ.get('VERCEL') and self.headers.get('X-Forwarded-Proto', 'https') == 'https' else 'http'

    def same_origin(self, writes=False):
        """Requests must target this app's own host; writes must also come from its own origin."""
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        if os.environ.get('VERCEL'):
            allowed |= {h for h in (os.environ.get(k, '') for k in ('VERCEL_URL', 'VERCEL_BRANCH_URL', 'VERCEL_PROJECT_PRODUCTION_URL')) if h}
            allowed |= {h.strip() for h in os.environ.get('APP_HOSTS', '').split(',') if h.strip()}
        if host not in allowed or self.headers.get('Sec-Fetch-Site') == 'cross-site':
            return False
        origin = self.origin_scheme() + '://' + host
        return not writes or self.headers.get('Origin', origin) == origin

    def stream_chat(self):
        """Server-sent events with streamed assistant text, tool activity and saved-state changes."""
        if not self.same_origin():
            return self.respond(403, {'error': 'Same-origin request required'})
        session_id = self.session_id()
        session = SESSION_STORE.get(session_id) if session_id else None
        if not session:
            return self.respond(404, {'error': 'Session not found; reload the page'})
        HOME_AGENT.resume(session_id)
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        def send(name, data):
            self.wfile.write(f'event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n'.encode('utf-8'))
        seen, state, sent, started = (-1, -1), None, {}, time.monotonic()
        try:
            self.wfile.write(b'retry: 1500\n\n'); self.wfile.flush()
            # A cycle ends before the platform's function duration; EventSource reconnects.
            while time.monotonic() - started < STREAM_SECONDS:
                version = HOME_AGENT.wait_change(session_id, seen, 15)
                changed, durable = version != seen, version[1] != seen[1]
                seen = version
                if durable:
                    session = SESSION_STORE.get(session_id)
                    if not session:
                        break
                    chat = session['chat']
                    current = {'chat_id': session['chat_id'], 'revision': session['revision'],
                               'map_revision': session['map_view']['revision'], 'status': chat.get('status'),
                               'request_id': chat.get('request_id'), 'progress': chat.get('progress'),
                               'error': chat.get('error'), 'recoverable': chat.get('recoverable')}
                    if current != state:
                        send('state', current); state = current
                live = HOME_AGENT.live_view(session_id, session)
                if live:
                    key = (live['chat_id'], live['request_id'])
                    if sent.get('key') != key:
                        sent = {'key': key}
                    payload, previous = {'chat_id': key[0], 'request_id': key[1]}, sent.get('text')
                    # Coalesced, offset-checked appends; anything else replaces the whole text.
                    if previous is None or not live['text'].startswith(previous):
                        payload['text'] = live['text']
                    elif len(live['text']) > len(previous):
                        payload.update(offset=len(previous), delta=live['text'][len(previous):])
                    for field in ('activity', 'note'):
                        if sent.get(field) != live[field]:
                            payload[field] = live[field]
                    if len(payload) > 2:
                        send('live', payload)
                        sent.update(text=live['text'], activity=live['activity'], note=live['note'])
                if not changed:
                    self.wfile.write(b': keepalive\n\n')
                    HOME_AGENT.resume(session_id)  # This open request can own a stalled turn.
                self.wfile.flush()
                time.sleep(.05)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def do_POST(self):
        action = {'/api/map': 'map', '/api/chat': 'chat', '/api/chat/cancel': 'cancel', '/api/chat/reset': 'reset', '/api/chat/resume': 'resume', '/api/chats/new': 'new_chat', '/api/chats/switch': 'switch_chat'}.get(urlparse(self.path).path)
        if action:
            return self.update_session(action)
        if urlparse(self.path).path == '/api/session':
            return self.update_session()
        endpoint = urlparse(self.path).path
        if endpoint not in ('/api/classify', '/api/import', '/api/route', '/api/public-geocode'):
            return self.respond(404,{'error':'not found'})
        if endpoint == '/api/classify' and not OPENAI_KEY:
            return self.respond(503,{'error':'Decisions APIは未設定です'})
        if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
            return self.respond(415,{'error':'application/json が必要です'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            limit = MAX_BYTES if endpoint == '/api/import' else 3_000
            if size<2 or size>limit:return self.respond(413,{'error':'入力が大きすぎます'})
            payload=json.loads(self.rfile.read(size))
            if not isinstance(payload,dict):raise ValueError('JSON object required')
            if endpoint == '/api/import':
                if payload.get('permitted') is not True:
                    raise ValueError('利用許可の確認が必要です')
                return self.respond(200, self.remember_native_results(normalize_feed(payload.get('records'))))
            if endpoint == '/api/route':
                return self.respond(200, route_time(payload))
            if endpoint == '/api/public-geocode':
                return self.respond(200, public_housing.geocode(payload.get('source'), payload.get('id')))
            title=payload.get('title','')
            description=payload.get('description','')
            if not isinstance(title,str) or not isinstance(description,str) or not title.strip():
                raise ValueError('タイトルが必要です')
            data=decision_classify(title,description)
            return self.respond(200,data)
        except (ValueError,json.JSONDecodeError):
            return self.respond(400,{'error':'JSON入力が不正です'})
        except (HTTPError,URLError,OSError,TimeoutError) as exc:
            return self.respond(502,{'error':'外部サービスへの接続に失敗しました: '+type(exc).__name__})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/api/chat/stream':
            return self.stream_chat()
        if u.path == '/api/chats':
            session_id = self.session_id()
            if not session_id or not SESSION_STORE.get(session_id):
                return self.respond(404, {'error': 'Session not found'})
            return self.respond(200, {'chats': SESSION_STORE.list_chats(session_id)})
        if u.path == '/api/session/schema':
            return self.respond(200, {'schema_version': SCHEMA_VERSION, 'criteria': CRITERIA_SCHEMA,
                                     'update': UPDATE_SCHEMA})
        if u.path == '/api/session':
            session_id = self.session_id()
            session = SESSION_STORE.get(session_id) if session_id else None
            if session:
                HOME_AGENT.resume(session_id)
                return self.respond(200, public_session(session))
            session_id, session = SESSION_STORE.create()
            return self.respond(200, public_session(session), cookie=f'search_session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=2592000' + ('; Secure' if self.origin_scheme() == 'https' else ''))
        if u.path == '/':
            self.path = '/index.html'
            return super().do_GET()
        if u.path == '/api/health':
            return self.respond(200,{'listings_enabled':True,'providers':['bing'] + (['brave'] if SEARCH_KEY else []),
                    'provider':'Brave Search API + Bing RSS' if SEARCH_KEY else 'Bing RSS (APIキー不要、利用制限あり)',
                    'has_brave_key':bool(SEARCH_KEY), 'decisions_enabled':bool(OPENAI_KEY), 'agent_enabled':bool(OPENAI_KEY), 'web_search_enabled':bool(OPENAI_KEY),
                    'walking_enabled':True, 'feed_enabled':bool(FEED_PATH or FEED_URL), 'routing_enabled':True,
                    'public_sources':['tokyo', 'tsubata'] + (['permitted'] if os.environ.get('PUBLIC_HOUSING_FEED_PATH') else []),
                    'suumo_reuse_permitted': SUUMO_REUSE_PERMITTED,
                    'listing_max_age_seconds':3600})
        if u.path == '/api/suumo-search':
            q = parse_qs(u.query, keep_blank_values=True)
            if not {'lat', 'lon', 'radius_km'} <= set(q) or not set(q) <= {'lat', 'lon', 'radius_km', 'max_rent_man', 'layout', 'page'} or any(len(v) != 1 for v in q.values()):
                return self.respond(400, {'error': 'lat, lon, radius_km を1つずつ指定してください'})
            try:
                lat, lon, radius = validate_search(float(q['lat'][0]), float(q['lon'][0]), float(q['radius_km'][0]))
                max_rent, layout = validate_filters(float(q['max_rent_man'][0]) if 'max_rent_man' in q else None,
                                                    q['layout'][0] if 'layout' in q else None)
                page = validate_page(int(q.get('page', ['1'])[0]))
            except ValueError as exc:
                return self.respond(400, {'error': str(exc)})
            if not SUUMO_REUSE_PERMITTED:
                return self.respond(403, {'error': 'SUUMOの取得・再利用許諾が未設定のため接続を停止しています'})
            key = (lat, lon, radius, max_rent, layout, page)
            with _cache_lock:
                cached = _suumo_cache.get(key)
                if cached and time.monotonic() - cached[0] < 120:
                    return self.respond(200, {**cached[1], 'cached': True})
            try:
                result = search_suumo(lat, lon, radius, max_rent, layout, page)
            except SuumoError as exc:
                return self.respond(502, {'error': str(exc), 'code': exc.code})
            with _cache_lock:
                if len(_suumo_cache) >= 20: _suumo_cache.clear()
                _suumo_cache[key] = (time.monotonic(), result)
            return self.respond(200, {**result, 'cached': False})
        if u.path == '/api/public-housing':
            query = parse_qs(u.query)
            try:
                return self.respond(200, public_housing.search(query.get('source', ['tokyo'])[0],
                                    query.get('area', [''])[0], int(query.get('offset', ['0'])[0])))
            except ValueError as error:
                return self.respond(400, {'error': str(error)})
            except (HTTPError, URLError, OSError, TimeoutError, KeyError, TypeError, StopIteration) as error:
                return self.respond(502, {'error': '公開データを取得できません: ' + type(error).__name__})
        if u.path == '/api/walking-area':
            q = parse_qs(u.query, keep_blank_values=True)
            try:
                if not {'lat', 'lon'} <= set(q) or not set(q) <= {'lat', 'lon', 'minutes', 'distance_km'} or any(len(value) != 1 for value in q.values()) or (('minutes' in q) == ('distance_km' in q)):
                    raise ValueError('緯度・経度と徒歩時間または徒歩距離を1つずつ指定してください')
                if 'distance_km' in q:
                    parameters = walking_distance_parameters(q['lat'][0], q['lon'][0], q['distance_km'][0])
                else:
                    parameters = walking_parameters(q['lat'][0], q['lon'][0], q['minutes'][0])
            except ValueError as exc:
                return self.respond(400, {'error': str(exc)})
            try:
                result = fetch_walking_area(*parameters) if 'minutes' in q else fetch_walking_area(*parameters[:2], distance_km=parameters[2])
            except TimeoutError:
                return self.respond(504, {'error': '徒歩圏サービスの応答待ちがタイムアウトしました。範囲を小さくするか、しばらく待って再試行してください。'})
            except (HTTPError, URLError, OSError, TimeoutError, ValueError, TypeError):
                return self.respond(502, {'error': '徒歩圏を取得できません。通信環境を確認して再試行してください。'})
            try:
                return self.respond(200, result)
            except (BrokenPipeError, ConnectionResetError):
                # A user may select another range before a large response ends.
                return
        if u.path != '/api/listings':
            if u.path not in ('/index.html', '/i18n.js', '/property_core.js', '/public_housing.js', '/session.js', '/chat.js', '/workspace.js', '/workspace.css', '/shell.js', '/shell.css', '/map_agent.js', '/map_agent.css', '/walking.js'):
                return self.respond(404, {'error':'not found'})
            return super().do_GET()
        q = parse_qs(u.query)
        def v(k,n=100):return clean(q.get(k,[''])[0], n)
        place = v('place',90)
        if len(place) < 2: return self.respond(400,{'error':'地域・会社・駅名を2文字以上入力してください'})
        kind = 'sale' if v('kind') == 'sale' else 'rent'
        provider = v('provider',10) or 'auto'
        if provider not in ('auto', 'bing', 'brave', 'feed'):
            return self.respond(400,{'error':'不明な検索先です'})
        if provider == 'brave' and not SEARCH_KEY:
            return self.respond(400,{'error':'Brave APIキーがありません'})
        if provider == 'feed' or (provider == 'auto' and (FEED_PATH or FEED_URL)):
            try:
                result = load_feed()
                result['results'] = [record for record in result['results'] if record['kind'] == kind]
                return self.respond(200, self.remember_native_results(result))
            except (ValueError, OSError, HTTPError, URLError) as exc:
                return self.respond(502, {'error':'許可済み募集データを取得できません: '+type(exc).__name__})
        details = {'layout':v('layout',10), 'keywords':v('keywords',65)}
        key = json.dumps([place,kind,details,provider],ensure_ascii=False)
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.monotonic()-cached[0] < CACHE_TTL:
                return self.respond(200,{**cached[1],'cached':True})
        out = fetch_listings(place,kind,details,provider)
        discoveries = out.pop('results', [])
        out['discovery_links'] = [{'url':record['url'], 'source':record['source']} for record in discoveries]
        out['results'] = []
        out['note'] = '検索索引だけでは確認日時と募集状況を検証できないため、物件情報は採用しません。掲載元リンクで最新情報を確認してください。'
        with _cache_lock:
            if len(_cache) >= 100:_cache.clear()
            _cache[key] = (time.monotonic(),out)
        return self.respond(200,out)


if __name__ == '__main__':
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    print(f'物件検索マップ http://127.0.0.1:{PORT}/ | search: Bing RSS (no key), Brave: {"available" if SEARCH_KEY else "not configured"}')
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
