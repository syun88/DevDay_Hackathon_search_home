"""Schedule-free rail + walking reference model from OSM topology.

This is not a journey planner. No timetables, service patterns or waits are known.
"""
import heapq
import zlib
import json
import math
import os
import threading
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

RADIUS_KM = 8
WALK_KMH = 4.8
RAIL_KMH = 30
LEG_MINUTES = 10
ENDPOINT = os.environ.get('TRANSIT_OVERPASS_URL', 'https://overpass-api.de/api/interpreter')
_cache = {}
_lock = threading.Lock()
_cache_lock = threading.Lock()


def distance(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 6371.0088 * 2 * math.asin(min(1, math.sqrt(h)))


def fetch_osm(lat, lon):
    query = f'''[out:json][timeout:25];(
      way(around:{RADIUS_KM*1000},{lat},{lon})[highway][highway!~"^(motorway|motorway_link|trunk|trunk_link|construction|proposed|raceway)$"];
      way(around:{RADIUS_KM*1000},{lat},{lon})[railway~"^(rail|subway|light_rail|tram)$"][service!~"^(yard|siding|spur)$"][usage!="industrial"][usage!="military"][usage!="test"];
      node(around:{RADIUS_KM*1000},{lat},{lon})[railway~"^(station|halt|stop)$"];
    );out body;>;out skel qt;'''
    request = Request(os.environ.get('TRANSIT_OVERPASS_URL', ENDPOINT), data=urlencode({'data': query}).encode(), headers={
        'User-Agent': 'SearchHome/1.0 (+https://github.com/syun88/DevDay_Hackathon_search_home)',
        'Content-Type': 'application/x-www-form-urlencoded', 'Accept': 'application/json', 'Accept-Encoding': 'gzip'})
    deadline = time.monotonic() + 75
    raw = bytearray()
    with urlopen(request, timeout=20) as response:
        decoder = zlib.decompressobj(16+zlib.MAX_WBITS) if response.headers.get('Content-Encoding') == 'gzip' else None
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError('鉄道・道路データの取得時間が上限を超えました')
            chunk = response.read1(65536)
            if not chunk:
                break
            raw.extend(decoder.decompress(chunk, 40_000_001-len(raw)) if decoder else chunk)
            if len(raw) > 40_000_000:
                raise ValueError('鉄道・道路データが大きすぎます')
        if decoder and not decoder.eof:
            raise ValueError('圧縮された鉄道・道路データが途中で切れました')
    data = json.loads(raw)
    if data.get('remark') or not isinstance(data.get('elements'), list):
        raise ValueError('鉄道・道路データを完全に取得できませんでした')
    return data


def graphs(data, origin):
    nodes, ways, stops = {}, [], {}
    # Preserve tagged nodes when recursion emits an untagged duplicate.
    for item in data['elements']:
        if item.get('type') == 'node':
            node_id = item['id']
            nodes[node_id] = (item['lat'], item['lon'])
            if item.get('tags', {}).get('railway') in ('station', 'halt', 'stop'):
                stops[node_id] = item.get('tags', {}).get('name', '駅')
        elif item.get('type') == 'way':
            ways.append(item)
    walking, rail = {}, {}
    for way in ways:
        tags = way.get('tags', {})
        is_rail = tags.get('railway') in ('rail', 'subway', 'light_rail', 'tram')
        if is_rail:
            if tags.get('service') in ('yard', 'siding', 'spur') or tags.get('usage') in ('industrial', 'military', 'test'):
                continue
        elif not tags.get('highway') or tags.get('highway') in ('motorway', 'motorway_link', 'trunk', 'trunk_link', 'construction', 'proposed', 'raceway'):
            continue
        elif tags.get('foot') in ('no', 'private') or (tags.get('access') in ('no', 'private') and tags.get('foot') not in ('yes', 'designated', 'permissive')):
            continue
        graph = rail if is_rail else walking
        for a, b in zip(way['nodes'], way['nodes'][1:]):
            if a not in nodes or b not in nodes or max(distance(origin, nodes[a]), distance(origin, nodes[b])) > RADIUS_KM:
                continue
            length = distance(nodes[a], nodes[b])
            if length == 0:
                continue
            graph.setdefault(a, []).append((b, length))
            graph.setdefault(b, []).append((a, length))
    return nodes, walking, rail, stops


class NodeIndex:
    """Local spatial buckets keep station snaps bounded on a city-sized graph."""
    def __init__(self, ids, nodes):
        self.nodes, self.cells = nodes, {}
        for node in ids:
            lat, lon = nodes[node]
            self.cells.setdefault((math.floor(lat*100), math.floor(lon*100)), []).append(node)

    def nearest(self, point, limit):
        x, y = (math.floor(value*100) for value in point)
        # 0.01 degree longitude is short at high latitudes.
        span = min(20, math.ceil(limit / max(.01, 1.11*math.cos(math.radians(point[0])))) + 1)
        candidates = (node for dx in range(-span, span+1) for dy in range(-span, span+1)
                      for node in self.cells.get((x+dx, y+dy), ()))
        best = min(((distance(point, self.nodes[node]), node) for node in candidates), default=(math.inf, None))
        return (best[1], best[0]) if best[0] <= limit else (None, math.inf)


def reach(graph, seeds, speed, budget, leg_cap=None):
    """Non-dominated (total minutes, distance on this walking leg) labels.

    A later arrival with a shorter walk must survive so the remaining walking
    allowance can reach a different branch. No station-to-station teleporting.
    """
    labels, queue = {}, []
    def offer(node, minutes, walked):
        if minutes > budget or (leg_cap is not None and walked > leg_cap):
            return
        previous = labels.get(node, [])
        if any(t <= minutes and w <= walked for t, w in previous):
            return
        labels[node] = [(t,w) for t,w in previous if not (minutes <= t and walked <= w)] + [(minutes, walked)]
        heapq.heappush(queue, (minutes, walked, node))
    for node, minutes, walked in seeds:
        offer(node, minutes, walked)
    count = 0
    while queue:
        minutes, walked, node = heapq.heappop(queue)
        if (minutes, walked) not in labels[node]:
            continue
        count += 1
        if count > 1_000_000:
            raise ValueError('鉄道範囲の計算量が上限を超えました')
        for other, length in graph.get(node, []):
            offer(other, minutes + length/speed*60, walked + length if leg_cap is not None else 0)
    return labels


def segments(graph, labels, nodes, speed, budget, leg_cap=None):
    result = []
    for a in labels:
        for b, length in graph.get(a, []):
            # Each reachable edge can have a different best label.
            remaining = max((min((budget-t)*speed/60, (leg_cap-w) if leg_cap is not None else math.inf)
                             for t,w in labels[a]), default=0)
            fraction = min(1, max(0, remaining / length))
            if fraction <= 0:
                continue
            if a > b and any(t+length/speed*60 <= budget and (leg_cap is None or w+length <= leg_cap) for t,w in labels.get(b, [])):
                continue
            start, end = nodes[a], nodes[b]
            result.append([[start[1], start[0]], [start[1]+(end[1]-start[1])*fraction, start[0]+(end[0]-start[0])*fraction]])
    return result


def calculate(data, lat, lon, minutes):
    origin = (lat, lon)
    nodes, walk, rail, stops = graphs(data, origin)
    wi, ri = NodeIndex(walk, nodes), NodeIndex(rail, nodes)
    start, snap = wi.nearest(origin, .15)
    if start is None:
        raise ValueError('起点の150m以内に歩行可能な道路がありません')
    leg = WALK_KMH * LEG_MINUTES / 60
    access = reach(walk, [(start, snap/WALK_KMH*60, snap)], WALK_KMH, minutes, leg)
    station_links = []
    for node, name in stops.items():
        road, road_gap = wi.nearest(nodes[node], .2)
        track, track_gap = ri.nearest(nodes[node], .2)
        if road is not None and track is not None:
            station_links.append((node, name, road, track, road_gap+track_gap))
    boarding = []
    for _, _, road, track, gap in station_links:
        for t,w in access.get(road, []):
            if w+gap <= leg:
                boarding.append((track, t+gap/WALK_KMH*60, 0))
    riding = reach(rail, boarding, RAIL_KMH, minutes)
    exits, stations = [], []
    for node, name, road, track, gap in station_links:
        if track not in riding:
            continue
        arrival = min(t for t,w in riding[track]) + gap/WALK_KMH*60
        if arrival <= minutes and gap <= leg:
            exits.append((road, arrival, gap))
            stations.append({'name': name, 'lat': nodes[node][0], 'lon': nodes[node][1], 'minutes': round(arrival, 1)})
    egress = reach(walk, exits, WALK_KMH, minutes, leg)
    roads = segments(walk, access, nodes, WALK_KMH, minutes, leg) + segments(walk, egress, nodes, WALK_KMH, minutes, leg)
    tracks = segments(rail, riding, nodes, RAIL_KMH, minutes)
    if not roads:
        raise ValueError('到達道路を計算できませんでした')
    # Geometry is only a viewport bounding box. Candidate checks use road proximity.
    coords = [p for line in roads+tracks for p in line]
    west, east = min(p[0] for p in coords), max(p[0] for p in coords)
    south, north = min(p[1] for p in coords), max(p[1] for p in coords)
    return {'metric': 'transit', 'minutes': minutes, 'origin': {'lat':lat,'lon':lon},
            'network': {'type':'MultiLineString','coordinates':roads},
            'rail_network': {'type':'MultiLineString','coordinates':tracks},
            'geometry': {'type':'Polygon','coordinates':[[[west,south],[east,south],[east,north],[west,north],[west,south]]]},
            'stations': stations, 'coverage_radius_km': RADIUS_KM, 'rail_speed_kmh':RAIL_KMH,
            'walking_speed_kmh': WALK_KMH, 'max_walk_per_leg_minutes': LEG_MINUTES,
            'source':'OpenStreetMap / Overpass', 'schedule_based':False}


def fetch_area(lat, lon, minutes):
    key = (lat, lon)
    # One shared graph is reused when only the time slider changes.
    with _cache_lock:
        cached = _cache.get(key)
    if cached and time.monotonic()-cached[0] < 1800:
        data = cached[1]
    else:
        if not _lock.acquire(timeout=5):
            raise TimeoutError('鉄道データの取得が混み合っています')
        try:
            cached = _cache.get(key)
            if cached and time.monotonic()-cached[0] < 1800:
                data = cached[1]
            else:
                data = fetch_osm(lat, lon)
                if len(_cache) >= 3:
                    _cache.clear()
                _cache[key] = (time.monotonic(), data)
        finally:
            _lock.release()
    return calculate(data, lat, lon, minutes)
