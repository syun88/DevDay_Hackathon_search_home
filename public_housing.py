"""Allowlisted open CSV adapters; public inventories are not live rental listings."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlparse, urljoin
from urllib.request import Request, urlopen

TOKYO = 'https://www.juutakuseisaku.metro.tokyo.lg.jp'
TOKYO_PAGE = TOKYO + '/toei_jutaku/kanri/toeidanchi'
TOKYO_LICENSE = 'https://creativecommons.org/licenses/by/4.0/'
TOKYO_CATALOG = 'https://catalog.data.metro.tokyo.lg.jp/dataset/t000030d0000000009'
TSUBATA = 'https://catalog-data.city.kanazawa.ishikawa.jp'
TSUBATA_PAGE = TSUBATA + '/dataset/173614_empty_bank'
RESOURCE = 'd6152659-e4f0-4670-a163-1742035e55ec'
TSUBATA_CSV = TSUBATA + '/dataset/c96c0f76-51e0-44d3-83be-66f3f12874c1/resource/' + RESOURCE + '/download/173614_empty_bank.csv'
MAX_BYTES = 2_000_000
CACHE_SECONDS = 120
_cache = {}
_lock = threading.Lock()
_geo_lock = threading.Lock()
_geocodes = {}
_last_geo = 0.0


def now():
    return datetime.now(timezone.utc).isoformat()


def read_url(url):
    request = Request(url, headers={'User-Agent': 'SearchHome/3.0 (user-initiated open-data lookup)'})
    with urlopen(request, timeout=15) as response:
        data = response.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError('Open data exceeds 2 MB')
    return data


def key(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def safe_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme == 'https' and bool(parsed.hostname) and not parsed.username and not parsed.password and parsed.port in (None, 443)
    except (ValueError, TypeError):
        return False


def amount(value):
    try:
        parsed = float(value)
        return parsed if not isinstance(value, bool) and math.isfinite(parsed) and parsed >= 0 else None
    except (ValueError, TypeError):
        return None


def valid_date(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.date() <= datetime.now(timezone.utc).date()
    except (ValueError, AttributeError, TypeError):
        return False


def reiwa_date(text):
    normalized = unicodedata.normalize('NFKC', re.sub('<[^>]+>', '', text))
    match = re.search(r'令和(\d+)年(\d+)月(\d+)日現在', normalized)
    if not match:
        raise ValueError('Source reference date missing')
    date = f'{2018 + int(match[1]):04}-{int(match[2]):02}-{int(match[3]):02}'
    if not valid_date(date):
        raise ValueError('Invalid source reference date')
    return date


def tokyo_csv_url(page):
    links = [urljoin(TOKYO_PAGE, url) for url, label in re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>([\s\S]*?)</a>', page)
             if 'オープンデータCSV' in re.sub('<[^>]+>', '', label)]
    if len(set(links)) != 1 or not links[0].startswith(TOKYO + '/documents/d/juutakuseisaku/'):
        raise ValueError('Tokyo CSV download link changed; review required')
    return links[0]


def rows(data):
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        text = data.decode('cp932')
    return list(csv.DictReader(io.StringIO(text)))


def record(source, identity, name, address, date, fetched, url, license_name, license_url):
    return {'id': source + ':' + hashlib.sha256(key(identity).encode()).hexdigest()[:20],
            'source': source, 'source_record_id': identity, 'name': name, 'address': address,
            'layer': 'inventory', 'kind': 'rent', 'price_yen': None, 'layout': None,
            'area': None, 'age': None, 'management_fee_yen': None, 'mandatory_fee_yen': None,
            'lat': None, 'lon': None, 'geo_precision': 'unknown',
            'availability_status': 'unknown', 'last_verified_at': None,
            'vacancy_public': None, 'vacancy_reserved': None,
            'data_as_of': date, 'fetched_at': fetched, 'source_url': url, 'official_url': url,
            'license': license_name, 'license_url': license_url}


def normalize_tokyo(data, date, fetched):
    output = []
    for row in rows(data):
        if not any(row.values()):
            continue
        if not all(row.get(field) for field in ('区市名', '団地名', '主な所在地')):
            raise ValueError('Tokyo CSV schema changed')
        address = '東京都' + row['主な所在地']
        identity = '|'.join(row.get(field, '') for field in ('団地名', '主な所在地', '面積', '間取', '建設年度'))
        item = record('tokyo', identity, row['団地名'], address, date, fetched, TOKYO_PAGE, 'CC BY 4.0 / 東京都住宅政策本部', TOKYO_LICENSE)
        item.update(municipality=row['区市名'], layout_text=row.get('間取'), area_text=row.get('面積'),
                    construction_year_text=row.get('建設年度'), managed_units=amount(row.get('管理戸数')),
                    station=row.get('主な駅'),
                    catalog_url=TOKYO_CATALOG)
        if re.fullmatch(r'\d+(?:R|K|DK|LDK)', row.get('間取', '')):
            item['layout'] = row['間取']
        if re.fullmatch(r'\d+(?:\.\d+)?', row.get('面積', '')):
            item['area'] = amount(row['面積'])
        output.append(item)
    if not output:
        raise ValueError('Empty Tokyo CSV')
    return deduplicate(output)


def normalize_tsubata(data, date, fetched):
    output, ended = [], 0
    for row in rows(data):
        if not all(field in row for field in ('ID', '所在地_連結表記', '契約形態', '受付状況', '金額', 'URL')):
            raise ValueError('Municipal CSV schema changed')
        if row['受付状況'] in ('成約済み', '募集終了', '受付終了'):
            ended += 1
            continue
        identity = row['ID']
        if not identity or not row['所在地_連結表記']:
            continue
        item = record('tsubata', identity, '津幡町 空き家 ' + identity, row['所在地_連結表記'], date, fetched,
                      TSUBATA_PAGE, 'CC BY / 津幡町（金沢市オープンデータポータル）', 'https://opendefinition.org/licenses/cc-by/')
        kind = {'売買': 'sale', '賃貸': 'rent'}.get(row['契約形態'])
        price = amount(row['金額'])
        if kind == 'rent' and '月額' not in row.get('備考', ''):
            price = None
        item.update(layer='municipal', kind=kind, price_yen=price, source_status=row['受付状況'],
                    availability_status='reported_open' if row['受付状況'] == '受付中' else 'unknown',
                    municipality='津幡町', data_url=TSUBATA_CSV, notes=row.get('備考', ''), data_as_of=None, source_updated_at=date,
                    official_url=row['URL'] if safe_url(row['URL']) else TSUBATA_PAGE)
        output.append(item)
    return deduplicate(output), ended


def deduplicate(items):
    return list({item['id']: item for item in items}.values())


def permitted_feed(path):
    with Path(path).open('rb') as source:
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError('Permitted feed exceeds 2 MB')
    feed = json.loads(raw)
    if not isinstance(feed, dict) or feed.get('permitted') is not True or not isinstance(feed.get('records'), list) or len(feed['records']) > 500:
        raise ValueError('Permitted public feed required')
    if not isinstance(feed.get('license'), str) or not feed['license'].strip() or not safe_url(feed.get('license_url')):
        raise ValueError('License and HTTPS license URL required')
    output = []
    fetched = now()
    for row in feed['records']:
        if not isinstance(row, dict) or not all(isinstance(row.get(field), str) and row[field].strip() for field in ('id', 'name', 'address')):
            raise ValueError('Feed identity required')
        if not valid_date(row.get('data_as_of')) or not safe_url(row.get('source_url')):
            raise ValueError('Source URL and reference date required')
        if row.get('availability_status') == 'ended':
            continue
        layer = row.get('layer')
        if layer not in ('inventory', 'vacancy', 'recruitment', 'municipal'):
            raise ValueError('Invalid public layer')
        item = record('permitted', layer + '|' + row['id'], row['name'], row['address'], row['data_as_of'], fetched,
                      row['source_url'], feed['license'], feed['license_url'])
        item.update(layer=layer, source_record_id=row['id'], kind=row.get('kind') if row.get('kind') in ('rent', 'sale') else None,
                    layout=row.get('layout'), availability_status='unknown',
                    official_url=row['official_url'] if safe_url(row.get('official_url')) else row['source_url'])
        for field in ('price_yen', 'area', 'age', 'management_fee_yen', 'mandatory_fee_yen', 'vacancy_public', 'vacancy_reserved'):
            item[field] = amount(row.get(field))
        latitude, longitude = row.get('lat'), row.get('lon')
        if all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in (latitude, longitude)) and abs(latitude) <= 85 and abs(longitude) <= 180:
            item.update(lat=latitude, lon=longitude, geo_precision='exact' if row.get('geo_precision') == 'exact' else 'approximate')
        verified = row.get('last_verified_at', '')
        try:
            timestamp = datetime.fromisoformat(verified.replace('Z', '+00:00'))
            fresh = timestamp.tzinfo is not None and 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= 3600
        except (ValueError, TypeError, AttributeError):
            fresh = False
        if layer == 'recruitment':
            if row.get('availability_status') != 'confirmed' or not fresh:
                continue
            item.update(availability_status='confirmed', last_verified_at=verified)
        output.append(item)
    return deduplicate(output)


def load(source):
    if source not in ('tokyo', 'tsubata', 'permitted'):
        raise ValueError('Unknown public source')
    with _lock:
        cached = _cache.get(source)
        if source != 'permitted' and cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        fetched, ended = now(), 0
        if source == 'tokyo':
            metadata = json.loads(read_url('https://catalog.data.metro.tokyo.lg.jp/api/3/action/package_show?id=t000030d0000000009'))
            if metadata['result'].get('license_id') != 'CC-BY-4.0':
                raise ValueError('Tokyo license changed')
            page = read_url(TOKYO_PAGE).decode('utf-8')
            date = reiwa_date(page)
            url = tokyo_csv_url(page)
            items = normalize_tokyo(read_url(url), date, fetched)
            for item in items:
                item['data_url'] = url
        elif source == 'tsubata':
            metadata = json.loads(read_url(TSUBATA + '/api/3/action/package_show?id=173614_empty_bank'))
            package = metadata['result']
            if package.get('license_id') != 'cc-by':
                raise ValueError('Municipal license changed')
            resource = next(item for item in package['resources'] if item['id'] == RESOURCE)
            date = resource['last_modified'][:10]
            if not valid_date(date):
                raise ValueError('Invalid municipal reference date')
            if resource['url'] != TSUBATA_CSV:
                raise ValueError('Municipal resource URL changed; review required')
            items, ended = normalize_tsubata(read_url(TSUBATA_CSV), date, fetched)
        else:
            path = os.environ.get('PUBLIC_HOUSING_FEED_PATH', '').strip()
            if not path:
                raise ValueError('No permitted public feed configured')
            items = permitted_feed(path)
        result = {'records': items, 'fetched_at': fetched, 'ended_excluded': ended,
                  'municipalities': sorted({item.get('municipality', '') for item in items} - {''})}
        _cache[source] = (time.monotonic(), result)
        return result


def search(source, area='', offset=0):
    if offset < 0 or offset > 10000 or len(area) > 100:
        raise ValueError('Invalid public search')
    result = load(source)
    items = [item for item in result['records'] if key(area) in key(item['name'] + item['address'])]
    page = [{**item, **(cached_position(item['address']) if item['geo_precision'] == 'unknown' else {})} for item in items[offset:offset + 20]]
    return {**{name: value for name, value in result.items() if name != 'records'},
            'records': page, 'total': len(items), 'offset': offset,
            'next_offset': offset + 20 if offset + 20 < len(items) else None}


def cached_position(address):
    with _geo_lock:
        cached = _geocodes.get(address)
        return cached[1] if cached and time.monotonic() - cached[0] < 3600 else {}


def address_key(value):
    normalized = key(value).replace('大字', '').replace('字', '').replace('地内', '')
    normalized = re.sub(r'(?<=[都道府県])[^都道府県市区町村]+郡', '', normalized)
    def numeric(match):
        digits = '〇一二三四五六七八九'
        parts = match[1].split('十')
        if len(parts) == 2:
            return str((digits.index(parts[0]) if parts[0] else 1) * 10 + (digits.index(parts[1]) if parts[1] else 0))
        return str(digits.index(parts[0]))
    normalized = re.sub(r'([一二三四五六七八九十]{1,3})(?=丁目|番)', numeric, normalized)
    return normalized.replace('番地', '').replace('丁目', '-').replace('番', '-').removesuffix('号').rstrip('-')


def geocode(source, identity):
    global _last_geo
    item = next((item for item in load(source)['records'] if item['id'] == identity), None)
    if not item:
        raise ValueError('Unknown public record')
    if item['geo_precision'] != 'unknown':
        return {field: item[field] for field in ('lat', 'lon', 'geo_precision')}
    return geocode_address(item['address'])


def geocode_address(address):
    """Shared validated, rate-limited address geocoding for owned listing records."""
    global _last_geo
    if not isinstance(address,str) or not address.strip() or len(address)>300: raise ValueError('Invalid address')
    cached = cached_position(address)
    if cached:
        return cached
    with _geo_lock:
        time.sleep(max(0, 1.1 - (time.monotonic() - _last_geo)))
        _last_geo = time.monotonic()
        url = 'https://msearch.gsi.go.jp/address-search/AddressSearch?' + urlencode({'q': address.replace('地内', '')})
        candidates = json.loads(read_url(url))
        position = {'lat': None, 'lon': None, 'geo_precision': 'unknown', 'geocoded_at': now(),
                    'geocode_source': '国土地理院 住所検索（協力：東大CSIS）', 'geocode_source_url': 'https://maps.gsi.go.jp/'}
        for candidate in candidates if isinstance(candidates, list) else []:
            if not isinstance(candidate, dict) or not isinstance(candidate.get('properties'), dict) or not isinstance(candidate.get('geometry'), dict):
                continue
            title = candidate.get('properties', {}).get('title', '')
            coordinates = candidate.get('geometry', {}).get('coordinates', [])
            if not isinstance(coordinates, list) or len(coordinates) != 2 or not isinstance(title, str) or not title or not address_key(address).startswith(address_key(title)):
                continue
            longitude, latitude = coordinates
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in coordinates) or abs(latitude) > 85 or abs(longitude) > 180:
                continue
            position.update(lat=latitude, lon=longitude, geo_precision='approximate', geocode_label=title)
            break
        if len(_geocodes) >= 1024:
            _geocodes.clear()
        _geocodes[address] = (time.monotonic(), position)
        return position
