"""Small local proof of SUUMO's anonymous map-page bootstrap flow.

This is not an official API client or permission for public reuse. No cookies are
stored or forwarded; page-issued values stay in memory for one bounded request.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import cos, isfinite, radians
from subprocess import run, TimeoutExpired
from urllib.parse import urlencode
import json
import re

MAP_PAGE = 'https://suumo.jp/map/tokyo/sc_shinjuku/'
API_URL = 'https://suumo.jp/jj/JJ903FC020/'
PAGE_SIZE = 20  # Matches the ordinary public site's listing page size.
PAGE_VALUES = re.compile(
    r"'bkApi':\s*\{\s*'UID':\s*'([A-Za-z0-9]+)',\s*'STMP':\s*'(\d+)',\s*'ATT':\s*'([0-9a-fA-F]+)'",
    re.S,
)
MONEY = re.compile(r'^(\d+(?:\.\d+)?)万円$|^(\d+)円$')
AREA = re.compile(r'^(\d+(?:\.\d+)?)平米$')
SOURCE_ID = re.compile(r'^\d{6,20}$')
LAYOUT_CODES = {'1R': '01', '1K': '02', '1DK': '03', '1LDK': '04',
                '2K': '05', '2DK': '06', '2LDK': '07', '3K': '08',
                '3DK': '09', '3LDK': '10', '4K': '11', '4DK': '12', '4LDK': '13'}
# Values supported by the public map page's rental price slider, in 万円.
RENT_STEPS = (3, 3.5, 4, 4.5, 5, 5.5, 6, 6.5, 7, 7.5, 8, 8.5, 9, 9.5,
              10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22,
              23, 24, 25, 26, 27, 28, 29, 30, 35, 40, 50, 100)


class SuumoError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def validate_search(lat: float, lon: float, radius_km: float) -> tuple[float, float, float]:
    if not all(isfinite(x) for x in (lat, lon, radius_km)):
        raise ValueError('座標と半径は有限の数値が必要です')
    # The prototype supports the Tokyo metro map only, not all Tokyo islands.
    if not (35.50 <= lat <= 35.90 and 139.30 <= lon <= 140.10):
        raise ValueError('この試作は東京都内の地図中心のみ対応します')
    if not 0.1 <= radius_km <= 2:
        raise ValueError('半径は0.1〜2kmで指定してください')
    return lat, lon, radius_km


def validate_filters(max_rent_man: float | None, layout: str | None) -> tuple[float | None, str | None]:
    if max_rent_man is not None:
        if not isfinite(max_rent_man) or max_rent_man not in RENT_STEPS:
            raise ValueError('家賃上限はSUUMO地図の選択値から指定してください')
    if layout is not None and layout not in LAYOUT_CODES:
        raise ValueError('この間取りはSUUMO地図の選択値に対応していません')
    return max_rent_man, layout


def validate_page(page: int) -> int:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError('ページは1以上の整数で指定してください')
    return page


def map_bounds(lat: float, lon: float, radius_km: float) -> dict[str, str]:
    dy = radius_km / 111.32
    dx = radius_km / (111.32 * cos(radians(lat)))
    return {
        'KUKEIPT1LT': f'{lat + dy:.7f}', 'KUKEIPT1LG': f'{lon + dx:.7f}',
        'KUKEIPT2LT': f'{lat - dy:.7f}', 'KUKEIPT2LG': f'{lon - dx:.7f}',
    }


def _curl_get(url: str, *, max_bytes: int, timeout: int) -> bytes:
    # URL is supplied through stdin so page-issued values never appear in argv.
    config = f'url = "{url}"\n'
    try:
        result = run(
            ['curl', '-K', '-', '--silent', '--show-error', '--max-time', str(timeout),
             '--max-filesize', str(max_bytes), '--write-out', '%{stderr}HTTP_STATUS:%{http_code}'],
            input=config.encode(), capture_output=True, timeout=timeout + 2,
            check=False,
        )
    except (OSError, TimeoutExpired) as exc:
        raise SuumoError('transport', 'SUUMOに接続できませんでした') from exc
    if result.returncode != 0:
        raise SuumoError('transport', 'SUUMOへの通信に失敗しました')
    status = re.search(rb'HTTP_STATUS:(\d{3})', result.stderr)
    if not status or status.group(1) != b'200':
        raise SuumoError('http', 'SUUMOが正常なHTTP応答を返しませんでした')
    if len(result.stdout) > max_bytes:
        raise SuumoError('size', 'SUUMOの応答が大きすぎます')
    return result.stdout


def parse_page_values(html: str) -> tuple[str, str, str]:
    found = PAGE_VALUES.search(html)
    if not found:
        raise SuumoError('bootstrap', 'SUUMOの公開地図ページから検索設定を取得できませんでした')
    return found.groups()


def yen(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = MONEY.fullmatch(value.strip())
    if not match:
        return None
    if match.group(1) is not None:
        return round(float(match.group(1)) * 10_000)
    return int(match.group(2))


def normalize_item(item: dict, fetched_at: str) -> dict | None:
    source_id = str(item.get('bukken_cd', ''))
    if not SOURCE_ID.fullmatch(source_id):
        return None
    source_url = f'https://suumo.jp/chintai/bc_{source_id}/'
    if item.get('link') != source_url:
        return None
    address = str(item.get('jusho') or '').strip()
    rent = yen(item.get('kakaku'))
    fee = yen(item.get('kanrihi'))
    match = AREA.fullmatch(str(item.get('menseki') or '').strip())
    area = float(match.group(1)) if match else None
    layout = str(item.get('madori') or '').strip()[:20] or None
    return {
        'source_id': source_id, 'source_url': source_url, 'address': address,
        'rent_jpy': rent, 'management_fee_jpy': fee,
        'known_monthly_subtotal_jpy': rent + fee if rent is not None and fee is not None else None,
        'other_monthly_costs': 'unknown', 'layout': layout, 'area_sqm': area,
        'fetched_at': fetched_at, 'source_updated_at': None,
        'availability_status': 'unknown', 'within_radius_verified': False,
        'map_coordinate_status': 'unverified_not_displayed',
    }


def parse_search_response(body: bytes, fetched_at: str, max_rent_man: float | None = None,
                          layout: str | None = None, page: int = 1) -> dict:
    page = validate_page(page)
    try:
        doc = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SuumoError('format', 'SUUMOがJSON物件データを返しませんでした') from exc
    smatch = doc.get('smatch') if isinstance(doc, dict) else None
    if not isinstance(smatch, dict):
        raise SuumoError('schema', 'SUUMOの物件データ形式が不明です')
    if 'errors' in smatch:
        errors = smatch.get('errors') or {}
        messages = errors.get('error') if isinstance(errors, dict) else None
        auth = isinstance(messages, list) and any('認証' in str(x.get('message', '')) for x in messages if isinstance(x, dict))
        raise SuumoError('authentication' if auth else 'upstream', 'SUUMOが検索を拒否しました' if not auth else 'SUUMOが認証エラーを返しました')
    resultset = smatch.get('resultset')
    if not isinstance(resultset, dict):
        raise SuumoError('schema', 'SUUMOの物件データ形式が不明です')
    items = resultset.get('item', [] if resultset.get('totalhits') == 0 else None)
    if not isinstance(items, list):
        raise SuumoError('schema', 'SUUMOの物件データ形式が不明です')
    raw = items[:PAGE_SIZE]
    normalized = [normalize_item(x, fetched_at) for x in raw if isinstance(x, dict)]
    in_region = [x for x in normalized if x and x['address'].startswith('東京都')]
    max_rent_jpy = round(max_rent_man * 10_000) if max_rent_man is not None else None
    valid = [x for x in in_region if
             (max_rent_jpy is None or (x['rent_jpy'] is not None and x['rent_jpy'] <= max_rent_jpy))
             and (layout is None or x['layout'] == layout or (layout == '1R' and x['layout'] == 'ワンルーム'))]
    return {
        'source': 'SUUMO anonymous map page', 'fetched_at': fetched_at,
        'source_updated_at': None, 'availability_status': 'unknown',
        'upstream_totalhits': resultset.get('totalhits'),
        'page': page, 'page_size': PAGE_SIZE,
        'has_next': (page * PAGE_SIZE < resultset['totalhits']) if isinstance(resultset.get('totalhits'), int) else len(items) >= PAGE_SIZE,
        'inspected_count': len(raw), 'excluded_out_of_region': sum(bool(x and not x['address'].startswith('東京都')) for x in normalized),
        'excluded_invalid': len(raw) - len([x for x in normalized if x]),
        'excluded_filter_mismatch_or_unknown': len(in_region) - len(valid),
        'filters': {'max_rent_man': max_rent_man, 'layout': layout},
        'results': valid,
        'note': 'このページの東京都住所かつ指定家賃・間取りを満たす物件を表示。続きは次ページで取得。半径内・空室・所在地座標・全費用は未確認。',
    }


def search(lat: float, lon: float, radius_km: float, max_rent_man: float | None = None,
           layout: str | None = None, page: int = 1) -> dict:
    page = validate_page(page)
    lat, lon, radius_km = validate_search(lat, lon, radius_km)
    max_rent_man, layout = validate_filters(max_rent_man, layout)
    html = _curl_get(MAP_PAGE, max_bytes=200_000, timeout=20).decode('utf-8', errors='replace')
    uid, stamp, att = parse_page_values(html)
    params = [
        ('UID', uid), ('STMP', stamp), ('ATT', att), ('FORMAT', '1'),
        ('CALLBACK', 'SUUMO.CALLBACK.FUNCTION'), ('P', str(page)), ('CNT', str(PAGE_SIZE)),
        ('GAZO', '2'), ('PROT', '1'), ('SE', '040'),
        *map_bounds(lat, lon, radius_km).items(),
    ]
    if max_rent_man is not None:
        params.append(('CHINRYOMAX', str(max_rent_man).removesuffix('.0')))
    if layout is not None:
        params.append(('CINM', LAYOUT_CODES[layout]))
    body = _curl_get(API_URL + '?' + urlencode(params), max_bytes=100_000, timeout=12)
    fetched_at = datetime.now(timezone.utc).isoformat()
    return parse_search_response(body, fetched_at, max_rent_man, layout, page)
