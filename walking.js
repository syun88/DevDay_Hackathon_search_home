/* GeoJSON walking areas, shared by the map and its dependency-free tests. */
(function(root, factory) {
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else root.homeWalking = factory();
})(typeof globalThis === 'object' ? globalThis : this, function() {
  'use strict';
  const RAD = Math.PI / 180, EARTH_KM = 6371.0088;
  const polygons = geometry => geometry?.type === 'Polygon' ? [geometry.coordinates]
    : geometry?.type === 'MultiPolygon' ? geometry.coordinates : [];

  function inRing(point, ring) {
    const x = point.lon, y = point.lat;
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      const [ax, ay] = ring[j], [bx, by] = ring[i];
      // Include the outer boundary, even on horizontal and vertical edges.
      const cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax);
      if (Math.abs(cross) < 1e-12 && x >= Math.min(ax, bx) && x <= Math.max(ax, bx)
          && y >= Math.min(ay, by) && y <= Math.max(ay, by)) return true;
      if ((ay > y) !== (by > y) && x < (bx - ax) * (y - ay) / (by - ay) + ax) inside = !inside;
    }
    return inside;
  }

  function contains(geometry, point) {
    if (!Number.isFinite(point?.lat) || !Number.isFinite(point?.lon)) return false;
    return polygons(geometry).some(rings => inRing(point, rings[0])
      && !rings.slice(1).some(ring => inRing(point, ring)));
  }

  function ringArea(ring) {
    let sum = 0;
    for (let i = 1; i < ring.length; i++) {
      const [ax, ay] = ring[i - 1], [bx, by] = ring[i];
      const delta = ((bx - ax + 540) % 360) - 180;
      sum += delta * RAD * (2 + Math.sin(ay * RAD) + Math.sin(by * RAD));
    }
    return Math.abs(sum * EARTH_KM ** 2 / 2);
  }

  function areaKm2(geometry) {
    return polygons(geometry).reduce((total, rings) => total + Math.max(0,
      ringArea(rings[0]) - rings.slice(1).reduce((sum, ring) => sum + ringArea(ring), 0)), 0);
  }

  function svgPath(geometry, project) {
    return polygons(geometry).flatMap(rings => rings.map(ring => ring.map(([lon, lat], i) => {
      const {x, y} = project({lat, lon});
      return `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(' ') + ' Z')).join(' ');
  }

  function networkPath(network, project) {
    if (network?.type !== 'MultiLineString') return '';
    return network.coordinates.map(line => line.map(([lon, lat], i) => {
      const {x, y} = project({lat, lon});
      return `${i ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(' ')).join(' ');
  }

  function nearNetwork(network, point, toleranceKm) {
    if (!Number.isFinite(point?.lat) || !Number.isFinite(point?.lon)) return false;
    const scale = Math.cos(point.lat * RAD);
    const relative = ([lon, lat]) => [(((lon-point.lon+540)%360)-180)*RAD*EARTH_KM*scale, (lat-point.lat)*RAD*EARTH_KM];
    for (const line of network?.coordinates || []) for (let i=1;i<line.length;i++) {
      const [ax,ay]=relative(line[i-1]),[bx,by]=relative(line[i]);
      const dx=bx-ax,dy=by-ay,den=dx*dx+dy*dy;
      const ratio=den?Math.max(0,Math.min(1,-(ax*dx+ay*dy)/den)):0;
      if (Math.hypot(ax+ratio*dx,ay+ratio*dy)<=toleranceKm) return true;
    }
    return false;
  }

  function networkLengthKm(network) {
    let total = 0;
    for (const line of network?.coordinates || []) for (let i = 1; i < line.length; i++) {
      const [ax, ay] = line[i - 1], [bx, by] = line[i];
      const h = Math.sin((by - ay) * RAD / 2) ** 2
        + Math.cos(ay * RAD) * Math.cos(by * RAD) * Math.sin((bx - ax) * RAD / 2) ** 2;
      total += 2 * EARTH_KM * Math.asin(Math.min(1, Math.sqrt(h)));
    }
    return total;
  }

  function searchRadiusKm(geometry, origin, distance) {
    let radius = 0.5;
    for (const rings of polygons(geometry)) for (const [lon, lat] of rings[0]) {
      radius = Math.max(radius, distance(origin, {lat, lon}) + 0.05);
    }
    return radius;
  }

  function bounds(geometry) {
    const points = polygons(geometry).flatMap(rings => rings[0]);
    return points.reduce((box, [lon, lat]) => ({
      west: Math.min(box.west, lon), east: Math.max(box.east, lon),
      south: Math.min(box.south, lat), north: Math.max(box.north, lat),
    }), {west: Infinity, east: -Infinity, south: Infinity, north: -Infinity});
  }

  // A changed origin or time immediately invalidates the old area. Late responses
  // cannot replace a newer selection, even if the network ignores cancellation.
  function createAreaLoader({fetchArea, onChange, delayMs = 450, timeoutMs = 120000}) {
    const entries = new Map();
    function cancel(entry) {
      clearTimeout(entry.timer);
      entry.controller.abort();
    }
    function update(requests, {immediate = false, force = false} = {}) {
      for (const [name, entry] of entries) if (!requests[name]) {
        cancel(entry);
        entries.delete(name);
      }
      for (const [name, request] of Object.entries(requests)) {
        const key = JSON.stringify([request.lat, request.lon, request.minutes ?? null, request.distance_km ?? null, request.mode ?? null]);
        const previous = entries.get(name);
        if (!force && previous?.key === key) continue;
        if (previous) cancel(previous);
        const entry = {key, request, status: 'loading', geometry: null, network: null, controller: new AbortController()};
        entries.set(name, entry);
        entry.timer = setTimeout(async () => {
          const timeout = setTimeout(() => entry.controller.abort(), timeoutMs);
          try {
            const result = await fetchArea(request, entry.controller.signal);
            if (entries.get(name) !== entry) return;
            if (!polygons(result.geometry).length) throw Error('Missing walking polygon');
            if (result.network?.type !== 'MultiLineString' || !result.network.coordinates.length) throw Error('Missing walking network');
            if (request.mode === 'transit' && (result.rail_network?.type !== 'MultiLineString'
                || !Array.isArray(result.stations) || !Number.isFinite(result.coverage_radius_km))) throw Error('Missing transit data');
            entry.geometry = result.geometry;
            entry.network = result.network;
            entry.rail_network = result.rail_network;
            entry.stations = result.stations;
            entry.coverage_radius_km = result.coverage_radius_km;
            entry.status = 'ready';
          } catch (error) {
            if (entries.get(name) !== entry) return;
            entry.status = 'error';
          } finally {
            clearTimeout(timeout);
            if (entries.get(name) === entry) onChange();
          }
        }, immediate ? 0 : delayMs);
      }
      onChange();
    }
    return {update, get: name => entries.get(name)};
  }

  return {contains, areaKm2, svgPath, networkPath, networkLengthKm, nearNetwork, searchRadiusKm, bounds, createAreaLoader};
});
