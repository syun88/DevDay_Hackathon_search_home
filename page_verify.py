"""Bounded verification of pages returned by web search.

One polite GET per candidate page (robots.txt respected, public HTTPS hosts only, size and
time limited) checks that the page still exists, where it redirects, its canonical URL and
title, whether it reads like an ended listing, and which search-summary facts actually
appear on the page. Hosts without reuse permission are never fetched. Page text is
untrusted data: only fixed fields are read and nothing on the page is followed as an
instruction.
"""
from datetime import datetime, timezone
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener
from urllib import robotparser
import ipaddress
import json
import re
import socket
import threading
import time
import unicodedata

USER_AGENT = 'SearchHomeLocal/3.0 (+listing page check for a user-initiated search)'
MAX_BYTES = 600_000
TIMEOUT = 8
ENDED = ('掲載終了', '掲載は終了', '公開を終了', '公開終了', '成約済', 'お探しの物件は見つかりません', 'ページが見つかりません',
         '物件が見つかりません', '募集を終了', '募集終了', 'この物件は現在', 'listing has ended', 'no longer available')
SEARCH_PATH = re.compile(r'/(list|search|ensen|area|theme|category|station|city)(/|$)|[?&](page|sort)=', re.I)
_robots, _robots_lock = {}, threading.Lock()


def public_host(host):
    """Reject loopback, private, link-local and reserved targets (also after DNS)."""
    if not host or host.endswith('.local') or host == 'localhost':
        return False
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, OSError):
        return False
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split('%')[0])
        if not address.is_global:
            return False
    return bool(infos)


class SafeRedirects(HTTPRedirectHandler):
    max_redirections = 4

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parts = urlsplit(newurl)
        if parts.scheme != 'https' or not public_host(parts.hostname):
            raise URLError('unsafe redirect')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = build_opener(SafeRedirects, HTTPSHandler)


def fetch(url, limit=MAX_BYTES, timeout=TIMEOUT):
    request = Request(url, headers={'User-Agent': USER_AGENT, 'Accept': 'text/html,application/xhtml+xml', 'Accept-Language': 'ja,en;q=0.5'})
    with _opener.open(request, timeout=timeout) as response:
        kind = response.headers.get('Content-Type', '')
        body = response.read(limit)
        charset = (re.search(r'charset=([\w-]+)', kind) or [None, 'utf-8'])[1]
        try:
            text = body.decode(charset, 'replace')
        except LookupError:
            text = body.decode('utf-8', 'replace')
        return response.status, response.url, kind, text


def robots_allows(url):
    parts = urlsplit(url)
    origin = f'{parts.scheme}://{parts.hostname}'
    with _robots_lock:
        cached = _robots.get(origin)
    if not cached or time.monotonic() - cached[0] > 3600:
        parser = robotparser.RobotFileParser()
        try:
            status, _, _, text = fetch(origin + '/robots.txt', 200_000, 5)
            parser.parse(text.splitlines() if status == 200 else [])
        except HTTPError as error:
            parser.parse([] if error.code in (404, 410) else ['User-agent: *', 'Disallow: /'])
        except (URLError, OSError, ValueError):
            parser.parse(['User-agent: *', 'Disallow: /'])  # Unknown policy: do not fetch.
        cached = (time.monotonic(), parser)
        with _robots_lock:
            if len(_robots) > 200:
                _robots.clear()
            _robots[origin] = cached
    return cached[1].can_fetch(USER_AGENT, url)


def normalize(text):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text or ''))


def page_text(html):
    html = re.sub(r'(?is)<(script|style|noscript)\b(?![^>]*application/ld\+json).*?</\1>', ' ', html)
    return normalize(unescape(re.sub(r'(?s)<[^>]+>', ' ', html)))


def json_ld_addresses(html):
    found = []
    for block in re.findall(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html)[:10]:
        try:
            data = json.loads(unescape(block.strip()))
        except ValueError:
            continue
        stack = [data]
        while stack and len(found) < 5:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node[:50])
            elif isinstance(node, dict):
                address = node.get('address')
                if isinstance(address, dict):
                    parts = [address.get(k) for k in ('addressRegion', 'addressLocality', 'streetAddress')]
                    text = ''.join(p for p in parts if isinstance(p, str))
                    if text:
                        found.append(normalize(text)[:160])
                elif isinstance(address, str) and address.strip():
                    found.append(normalize(address)[:160])
                stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
    return found


def money_forms(yen):
    yen = int(yen)
    forms = {f'{yen:,}円', f'{yen}円'}
    if yen % 100 == 0:
        man = yen / 10000
        forms |= {f'{man:g}万円', f'{man:.1f}万円', f'{man:.2f}万円'.rstrip('0').rstrip('.') + '万円'}
    return {normalize(f) for f in forms}


def title_facts(title):
    """Layout and floor area when the page title states them, e.g. [1LDK/2階/34.08㎡]."""
    layout = re.search(r'(?<![0-9A-Z])([1-9](?:S?LDK|DK|K|R))(?![A-Z])|ワンルーム', title.upper())
    area = re.search(r'([0-9]{1,4}(?:\.[0-9]{1,2})?)(?:㎡|M2|M²)', title.upper())
    facts = {}
    rent = re.search(r'([0-9]{1,3}(?:\.[0-9]{1,2})?)万円', title)
    if rent and 1 <= float(rent.group(1)) <= 500:
        facts['price'] = float(rent.group(1))  # Portal titles state the base rent in 万円.
    address = re.search(r'(東京都|北海道|京都府|大阪府|[^\s\d()（）|｜/、。]{2,3}県)[^\s()（）|｜/、。【】\[\]]{2,40}', title)
    if address:
        facts['address'] = re.sub(r'(?<=[0-9])[^0-9-]+$', '', address.group(0))[:80]
    if layout:
        facts['layout'] = '1R' if layout.group(0) == 'ワンルーム' else layout.group(1)
    if area and 5 <= float(area.group(1)) <= 2000:
        facts['area'] = float(area.group(1))
    return facts


def confirm_facts(text, extracted):
    """Which summary values appear on the page itself. Absence is 'not_found', never a contradiction."""
    result = {}
    price = extracted.get('price')
    if isinstance(price, (int, float)):
        result['price'] = 'page' if any(f in text for f in money_forms(round(price * 10000))) else 'not_found'
    fee = extracted.get('management_fee_yen')
    if isinstance(fee, int):
        result['management_fee_yen'] = 'page' if fee == 0 and ('管理費なし' in text or '管理費-' in text) or any(f in text for f in money_forms(fee)) else 'not_found'
    layout = extracted.get('layout')
    if isinstance(layout, str) and layout:
        result['layout'] = 'page' if normalize(layout).upper() in text.upper() else 'not_found'
    area = extracted.get('area')
    if isinstance(area, (int, float)):
        forms = {f'{area:g}m2', f'{area:g}㎡', f'{area:g}m²', f'{area:.2f}m2', f'{area:.2f}㎡'}
        result['area'] = 'page' if any(normalize(f) in text for f in forms) else 'not_found'
    address = extracted.get('address')
    if isinstance(address, str) and address:
        result['address'] = 'page' if normalize(address)[:10] in text else 'not_found'
    return result


def verify(url, extracted, restricted_hosts=(), fetcher=None):
    checked_at = datetime.now(timezone.utc).isoformat()
    parts = urlsplit(url)
    host = (parts.hostname or '').lower()
    base = {'checked_at': checked_at, 'url': url}
    if any(host == h or host.endswith('.' + h) for h in restricted_hosts):
        return {**base, 'status': 'not_permitted'}
    if parts.scheme != 'https' or not (fetcher or public_host(host)):
        return {**base, 'status': 'unsafe_url'}
    fetcher = fetcher or fetch
    try:
        if fetcher is fetch and not robots_allows(url):
            return {**base, 'status': 'robots_disallowed'}
        status, final, kind, html = fetcher(url)
    except HTTPError as error:
        return {**base, 'status': 'gone' if error.code in (404, 410) else 'blocked' if error.code in (401, 403, 429) else 'error', 'http_status': error.code}
    except (URLError, OSError, ValueError, TimeoutError):
        return {**base, 'status': 'unreachable'}
    if 'html' not in (kind or 'text/html'):
        return {**base, 'status': 'not_html', 'http_status': status}
    canonical = re.search(r'(?is)<link[^>]+rel=["\']canonical["\'][^>]*href=["\']([^"\']+)', html)
    canonical = urljoin(final, unescape(canonical.group(1))) if canonical else None
    title = re.search(r'(?is)<title[^>]*>(.*?)</title>', html)
    text = page_text(html)
    final_path, start_path = urlsplit(final).path.rstrip('/'), parts.path.rstrip('/')
    moved = urlsplit(final).hostname != parts.hostname or (final_path != start_path and final_path.split('/')[1:2] != start_path.split('/')[1:2])
    ended = any(normalize(marker) in text for marker in ENDED[:9]) and len(text) < 60_000 or any(normalize(m) in text[:3000] for m in ENDED)
    return {**base, 'status': 'ok', 'http_status': status, 'final_url': final if final.startswith('https://') else url,
            'canonical_url': canonical if canonical and canonical.startswith('https://') else None,
            'page_title': normalize(unescape(title.group(1)))[:160] if title else None,
            'moved': moved, 'looks_ended': ended, 'looks_like_search_page': bool(SEARCH_PATH.search(urlsplit(final).path + '?' + urlsplit(final).query)),
            'page_addresses': json_ld_addresses(html), 'facts': confirm_facts(text, extracted),
            'title_facts': title_facts(normalize(unescape(title.group(1))) if title else '')}
