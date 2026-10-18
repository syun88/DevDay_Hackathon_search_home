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
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path
from html import unescape
from datetime import datetime, timezone
import xml.etree.ElementTree as ET
import os, json, re, socket, threading, time

BASE = Path(__file__).resolve().parent
OPENAI_KEY = os.environ.get('OPENAI_API_KEY', '').strip()
SEARCH_KEY = os.environ.get('BRAVE_SEARCH_API_KEY', '').strip()
PORT = int(os.environ.get('PORT', '8765'))
ALLOWED_DOMAINS = ('suumo.jp', 'homes.co.jp', 'athome.co.jp')
USER_AGENT = 'PropertyFinderLocal/2.0 (+browser-side user initiated search)'
CACHE_TTL = 120
_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
MAX_BYTES = 1_000_000


def get_text(url: str, headers: dict | None = None, timeout=12) -> str:
    req = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'application/rss+xml, application/json;q=0.8', **(headers or {})})
    with urlopen(req, timeout=timeout) as response:
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError('検索結果が大きすぎます')
        encoding = response.headers.get_content_charset() or 'utf-8'
        return body.decode(encoding, errors='replace')


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
                        'availability_status':'unknown', 'geo_precision':'unknown'})
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

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE), **kwargs)

    def respond(self, code: int, obj: dict):
        payload = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        if urlparse(self.path).path != '/api/classify':
            return self.respond(404,{'error':'not found'})
        if not OPENAI_KEY:
            return self.respond(503,{'error':'Decisions APIは未設定です'})
        if self.headers.get('Content-Type','').split(';')[0].strip().lower() != 'application/json':
            return self.respond(415,{'error':'application/json が必要です'})
        try:
            size=int(self.headers.get('Content-Length','0'))
            if size<2 or size>3_000:return self.respond(413,{'error':'入力が大きすぎます'})
            payload=json.loads(self.rfile.read(size))
            if not isinstance(payload,dict):raise ValueError('JSON object required')
            title=payload.get('title','')
            description=payload.get('description','')
            if not isinstance(title,str) or not isinstance(description,str) or not title.strip():
                raise ValueError('タイトルが必要です')
            data=decision_classify(title,description)
            return self.respond(200,data)
        except (ValueError,json.JSONDecodeError):
            return self.respond(400,{'error':'JSON入力が不正です'})
        except (HTTPError,URLError,OSError,TimeoutError) as exc:
            return self.respond(502,{'error':'Decisions APIへの接続に失敗しました: '+type(exc).__name__})

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == '/':
            self.path = '/index.html'
            return super().do_GET()
        if u.path == '/api/health':
            return self.respond(200,{'listings_enabled':True,'providers':['bing'] + (['brave'] if SEARCH_KEY else []),
                    'provider':'Brave Search API + Bing RSS' if SEARCH_KEY else 'Bing RSS (APIキー不要、利用制限あり)',
                    'has_brave_key':bool(SEARCH_KEY), 'decisions_enabled':bool(OPENAI_KEY)})
        if u.path != '/api/listings':
            return super().do_GET()
        q = parse_qs(u.query)
        def v(k,n=100):return clean(q.get(k,[''])[0], n)
        place = v('place',90)
        if len(place) < 2: return self.respond(400,{'error':'地域・会社・駅名を2文字以上入力してください'})
        kind = 'sale' if v('kind') == 'sale' else 'rent'
        provider = v('provider',10) or 'auto'
        if provider not in ('auto', 'bing', 'brave'):
            return self.respond(400,{'error':'不明な検索先です'})
        if provider == 'brave' and not SEARCH_KEY:
            return self.respond(400,{'error':'Brave APIキーがありません'})
        details = {'layout':v('layout',10), 'keywords':v('keywords',65)}
        key = json.dumps([place,kind,details,provider],ensure_ascii=False)
        with _cache_lock:
            cached = _cache.get(key)
            if cached and time.monotonic()-cached[0] < CACHE_TTL:
                return self.respond(200,{**cached[1],'cached':True})
        out = fetch_listings(place,kind,details,provider)
        with _cache_lock:
            if len(_cache) >= 100:_cache.clear()
            _cache[key] = (time.monotonic(),out)
        return self.respond(200,out)


if __name__ == '__main__':
    server=ThreadingHTTPServer(('127.0.0.1',PORT),Handler)
    print(f'物件検索マップ http://127.0.0.1:{PORT}/ | search: Bing RSS (no key), Brave: {"available" if SEARCH_KEY else "not configured"}')
    try: server.serve_forever()
    except KeyboardInterrupt: server.server_close()
