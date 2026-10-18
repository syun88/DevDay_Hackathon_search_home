import math
from datetime import datetime, timezone
from urllib.parse import urlparse

MAX_AGE_SECONDS = 3600


def nonnegative(value):
    if value is None or isinstance(value, bool) or value == '':
        return None
    try:
        parsed = float(value)
    except (ValueError, TypeError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def normalize_feed(records, now=None):
    if not isinstance(records, list) or len(records) > 500:
        raise ValueError('records must be a list of at most 500 listings')
    now = now or datetime.now(timezone.utc)
    results, rejected, seen = [], 0, set()
    for record in records:
        try:
            if not isinstance(record, dict):
                raise ValueError('object required')
            verified = datetime.fromisoformat(str(record.get('last_verified_at', '')).replace('Z', '+00:00'))
            if verified.tzinfo is None or not 0 <= (now - verified).total_seconds() <= MAX_AGE_SECONDS:
                raise ValueError('stale or missing verification time')
            if record.get('availability_status') != 'confirmed':
                raise ValueError('availability not confirmed by source')
            url = record.get('source_url', '')
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError('HTTPS source URL required')
            title, source = record.get('title'), record.get('source_name')
            if not isinstance(title, str) or not title.strip() or not isinstance(source, str) or not source.strip():
                raise ValueError('title and source required')
            if record.get('kind') not in ('rent', 'sale'):
                raise ValueError('kind required')
            kind = record['kind']
            price_yen = nonnegative(record.get('rent_yen' if kind == 'rent' else 'sale_price_yen'))
            latitude, longitude = record.get('lat'), record.get('lon')
            coordinates = (latitude, longitude)
            if not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in coordinates):
                latitude = longitude = None
            elif abs(latitude) > 85 or abs(longitude) > 180:
                latitude = longitude = None
            identity = (url, str(record.get('source_record_id', '')))
            if identity in seen:
                continue
            seen.add(identity)
            results.append({
                'title': title.strip()[:180], 'description': str(record.get('description', ''))[:550],
                'url': url, 'source': source.strip()[:180], 'source_record_id': identity[1], 'kind': kind,
                'lat': latitude, 'lon': longitude,
                'geo_precision': record.get('geo_precision') if latitude is not None and record.get('geo_precision') in ('exact', 'building', 'approximate') else 'unknown',
                'availability_status': 'confirmed', 'last_verified_at': verified.isoformat(),
                'retrieved_at': now.isoformat(), 'data_quality': 'source_provided',
                'extracted': {
                    'price': price_yen / 10000 if price_yen is not None else None,
                    'price_unit': '万円/月' if kind == 'rent' else '万円',
                    'management_fee_yen': nonnegative(record.get('management_fee_yen')),
                    'mandatory_fee_yen': nonnegative(record.get('mandatory_fee_yen')),
                    'area': nonnegative(record.get('area_m2')), 'age': nonnegative(record.get('age')),
                    'layout': str(record.get('layout') or '')[:20], 'address': str(record.get('address') or '')[:180],
                    'pets': record.get('pets') if isinstance(record.get('pets'), bool) else None,
                    'parking': record.get('parking') if isinstance(record.get('parking'), bool) else None,
                },
            })
        except (ValueError, TypeError, OverflowError):
            rejected += 1
    return {'results': results, 'rejected': rejected, 'provider': 'feed', 'checked_at': now.isoformat()}
