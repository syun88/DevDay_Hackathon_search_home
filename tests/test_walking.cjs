const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const walking = require('../walking.js');
const propertyCore = require('../property_core.js');

const square = (west = 0, south = 0, size = 1) => [
  [west, south], [west + size, south], [west + size, south + size],
  [west, south + size], [west, south],
];
const area = {type: 'Polygon', coordinates: [square()]};
const network = {type: 'MultiLineString', coordinates: [[[0, 0], [1, 0]]]};
const concave = {type: 'Polygon', coordinates: [[
  [0, 0], [3, 0], [3, 1], [1, 1], [1, 3], [0, 3], [0, 0],
]]};

test('road-network contours exclude points in a concavity, including nearby points', () => {
  assert.equal(walking.contains(concave, {lon: 0.5, lat: 2}), true);
  assert.equal(walking.contains(concave, {lon: 2, lat: 2}), false);
  assert.equal(walking.contains(concave, {lon: 1, lat: 2}), true);
  assert.equal(walking.contains(concave, {lon: NaN, lat: 0.5}), false);
});

test('holes are excluded and separate MultiPolygon islands remain usable', () => {
  const geometry = {type: 'MultiPolygon', coordinates: [[square(), square(0.25, 0.25, 0.5)], [square(2, 2)]]};
  for (const [point, expected] of [
    [{lon: 0.1, lat: 0.1}, true], [{lon: 0.5, lat: 0.5}, false],
    [{lon: 0.25, lat: 0.5}, false], [{lon: 2.5, lat: 2.5}, true], [{lon: 1.5, lat: 1.5}, false],
  ]) assert.equal(walking.contains(geometry, point), expected);
  assert.equal(walking.contains(null, {lon: 0, lat: 0}), false);
  const svg = walking.svgPath(geometry, ({lat, lon}) => ({x: lon * 10, y: lat * 10}));
  assert.equal((svg.match(/M/g) || []).length, 3);
  assert.equal((svg.match(/Z/g) || []).length, 3);
  assert.ok(!svg.includes('NaN'));
});

test('area uses spherical square kilometres, subtracts holes and ignores ring direction', () => {
  const expected = 6371.0088 ** 2 * Math.PI / 180 * Math.sin(Math.PI / 180);
  assert.ok(Math.abs(walking.areaKm2(area) - expected) < 1e-6);
  const hole = square(0.25, 0.25, 0.5);
  const withHole = {type: 'Polygon', coordinates: [square().reverse(), hole.reverse()]};
  assert.ok(Math.abs(walking.areaKm2(withHole)
    - (expected - walking.areaKm2({type: 'Polygon', coordinates: [hole]}))) < 1e-6);
  assert.equal(walking.areaKm2(null), 0);
});

test('building query radius and viewport bounds cover the actual contour', () => {
  const distance = (a, b) => Math.hypot(a.lon - b.lon, a.lat - b.lat);
  const radius = walking.searchRadiusKm(concave, {lat: 0, lon: 0}, distance);
  for (const [lon, lat] of concave.coordinates[0]) assert.ok(radius > distance({lon: 0, lat: 0}, {lon, lat}));
  assert.deepEqual(walking.bounds(concave), {west: 0, east: 3, south: 0, north: 3});
});

test('reachable roads render as open paths without painting the enclosed land or water', () => {
  const roads = {type: 'MultiLineString', coordinates: [[[0, 0], [1, 0]], [[0, 1], [1, 1]]]};
  const svg = walking.networkPath(roads, ({lat, lon}) => ({x: lon, y: lat}));
  assert.equal((svg.match(/M/g) || []).length, 2);
  assert.ok(!svg.includes('Z'));
  assert.equal(walking.networkPath(null, () => {}), '');
  const equatorLength = 6371.0088 * Math.PI / 180;
  assert.ok(Math.abs(walking.networkLengthKm(network) - equatorLength) < 1e-6);
});

const tick = () => new Promise(resolve => setTimeout(resolve, 5));
async function waitUntil(predicate) {
  for (let index = 0; index < 200; index++) {
    if (predicate()) return;
    await tick();
  }
  assert.fail('Timed out waiting for area-loader state');
}
const request = (minutes = 15, lat = 35) => ({lat, lon: 139, minutes});

test('rapid slider changes are debounced; duplicate selections do not refetch', async () => {
  const calls = [];
  const loader = walking.createAreaLoader({delayMs: 15, onChange() {}, async fetchArea(value) {
    calls.push(value); return {geometry: area, network};
  }});
  loader.update({a: request(5)});
  loader.update({a: request(10)});
  loader.update({a: request(15)});
  await waitUntil(() => loader.get('a').status === 'ready');
  assert.deepEqual(calls.map(value => value.minutes), [15]);
  loader.update({a: request(15)});
  await tick();
  assert.equal(calls.length, 1);
});

test('a changed origin immediately invalidates its area and late responses cannot restore it', async () => {
  const pending = [];
  const loader = walking.createAreaLoader({delayMs: 0, onChange() {}, fetchArea(value, signal) {
    return new Promise(resolve => pending.push({value, signal, resolve}));
  }});
  loader.update({a: request()});
  await waitUntil(() => pending.length === 1);
  loader.update({a: request(30, 36)});
  assert.equal(loader.get('a').status, 'loading');
  assert.equal(loader.get('a').geometry, null);
  assert.equal(pending[0].signal.aborted, true);
  await waitUntil(() => pending.length === 2);
  const newer = {type: 'Polygon', coordinates: [square(2, 2)]};
  pending[1].resolve({geometry: newer, network});
  await waitUntil(() => loader.get('a').status === 'ready');
  pending[0].resolve({geometry: area, network});
  await tick();
  assert.equal(loader.get('a').geometry, newer);
});

test('switching between walking time and walking distances invalidates the old metric', async () => {
  const calls = [];
  const loader = walking.createAreaLoader({delayMs: 0, onChange() {}, async fetchArea(value) {
    calls.push(value); return {geometry: area, network};
  }});
  loader.update({a: request(5)});
  await waitUntil(() => loader.get('a').status === 'ready');
  loader.update({a: {lat: 35, lon: 139, distance_km: 1}});
  assert.equal(loader.get('a').geometry, null);
  await waitUntil(() => loader.get('a').status === 'ready');
  loader.update({a: {lat: 35, lon: 139, distance_km: 2}});
  assert.equal(loader.get('a').geometry, null);
  await waitUntil(() => loader.get('a').status === 'ready');
  assert.equal(calls.length, 3);
  assert.equal(calls[0].minutes, 5);
  assert.equal(calls[1].distance_km, 1);
  assert.equal(calls[2].distance_km, 2);
});

test('failure and timeout stay unavailable until retry succeeds', async () => {
  let fail = true;
  const loader = walking.createAreaLoader({delayMs: 0, onChange() {}, async fetchArea() {
    if (fail) throw Error('upstream unavailable');
    return {geometry: area, network};
  }});
  loader.update({a: request()});
  await waitUntil(() => loader.get('a').status === 'error');
  assert.equal(loader.get('a').geometry, null);
  fail = false;
  loader.update({a: request()}, {force: true});
  await waitUntil(() => loader.get('a').status === 'ready');
  const timeout = walking.createAreaLoader({delayMs: 0, timeoutMs: 5, onChange() {}, fetchArea(value, signal) {
    return new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(Error('timeout'))));
  }});
  timeout.update({a: request()});
  await waitUntil(() => timeout.get('a').status === 'error');
});

test('a contour without the actual reachable road network is an error, never a filled area', async () => {
  const loader = walking.createAreaLoader({delayMs: 0, onChange() {}, async fetchArea() {
    return {geometry: area};
  }});
  loader.update({a: request()});
  await waitUntil(() => loader.get('a').status === 'error');
  assert.equal(loader.get('a').geometry, null);
  assert.equal(loader.get('a').network, null);
});

test('disabling B or switching to distance cancels and removes unused walking areas', async () => {
  const pending = [];
  const loader = walking.createAreaLoader({delayMs: 0, onChange() {}, fetchArea(value, signal) {
    return new Promise(resolve => pending.push({signal, resolve}));
  }});
  loader.update({a: request(), b: request(20)});
  await waitUntil(() => pending.length === 2);
  loader.update({a: request()});
  assert.equal(loader.get('b'), undefined);
  assert.equal(pending[1].signal.aborted, true);
  loader.update({});
  assert.equal(loader.get('a'), undefined);
  for (const item of pending) item.resolve({geometry: area, network});
  await tick();
  assert.equal(loader.get('a'), undefined);
  assert.equal(loader.get('b'), undefined);
});

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
function mapContext() {
  const controls = Object.fromEntries(['maxPrice', 'minArea', 'maxAge', 'layout', 'type', 'pets', 'parking', 'sort', 'includeUnknown', 'maxCommute', 'commuteMode']
    .map(id => [id, {value: '', checked: false}]));
  controls.insideOnly = {checked: true}; controls.listingOnlyWithin = {checked: true}; controls.restrictB = {checked: false};
  const entries = {a: {status: 'ready', geometry: concave, network}, b: {status: 'ready', geometry: area, network}};
  const state = {a: {lat: 0, lon: 0}, b: {lat: 0, lon: 0}, ra: 10, rb: 10, properties: []};
  let walk = true;
  const context = vm.createContext({
    state, entries, controls, propertyCore, commuteResults: new Map(), transitMode: () => false, walkingMode: () => walk, walkingAreas: {get: which => entries[which]},
    requiredWalkingAnchors: () => controls.restrictB.checked ? ['a', 'b'] : ['a'],
    $: id => id === 'map' ? {getBoundingClientRect: () => ({width: 800, height: 600})} : controls[id], window: {homeWalking: walking},
    dist: (a, b) => Math.hypot(a.lon - b.lon, a.lat - b.lat), t: key => key,
    circleSVG: () => '<circle/>', screen: ({lat, lon}) => ({x: lon, y: lat}),
    center: {lat: 0, lon: 0}, project: (lat, lon) => ({x: lon, y: lat}),
  });
  const sections = [
    ['function rangesReady(', 'function rangeSearchRadius('],
    ['function currentFilters(', 'function filtered(){'],
    ['function filtered(){', 'function matched(){'],
    ['function resultFlags(', 'function locateImportedListing('],
    ['function rangeSVG(', 'function renderMap('],
  ];
  for (const [start, end] of sections) vm.runInContext(html.slice(html.indexOf(start), html.indexOf(end)), context);
  return {context, controls, state, entries, setWalk(value) {walk = value;}};
}

test('saved candidates and listing flags use the drawn walking polygon', () => {
  const {context, state, controls} = mapContext();
  const verified = {url: 'https://example.com/test-only', availability_status: 'confirmed', last_verified_at: new Date().toISOString(), geo_precision: 'exact'};
  const inside = {...verified, name: 'inside', lat: 2, lon: 0.5};
  const outside = {...verified, name: 'outside', lat: 2, lon: 2};
  state.properties = [inside, outside];
  assert.equal(context.filtered().length, 1);
  assert.equal(context.filtered()[0].name, 'inside');
  assert.equal(context.resultFlags(outside).within, false);
  assert.ok(context.resultFlags(outside).flags.includes('指定範囲外'));
  controls.insideOnly.checked = false;
  assert.equal(context.filtered().length, 2);
});

test('B applies an intersection and unavailable areas cannot produce false matches', () => {
  const {context, state, controls, entries} = mapContext();
  const verified = {url: 'https://example.com/test-only', availability_status: 'confirmed', last_verified_at: new Date().toISOString(), geo_precision: 'exact'};
  const aOnly = {...verified, lat: 2, lon: 0.5}, both = {...verified, lat: 0.5, lon: 0.5};
  state.properties = [aOnly, both]; controls.restrictB.checked = true;
  assert.equal(context.filtered().length, 1);
  assert.equal(context.withinRange(aOnly), false);
  entries.b.status = 'error';
  assert.equal(context.withinRange(both), null);
  assert.equal(context.filtered().length, 0);
  const flags = context.resultFlags(both);
  assert.equal(flags.within, false);
  assert.equal(flags.flags.length, 0);
  assert.ok(flags.unknown.includes('範囲未判定'));
});

test('walking rendering never fills the contour or falls back to circles; distance mode still draws circles', () => {
  const {context, entries, setWalk} = mapContext();
  const svg = context.rangeSVG('a', '#2563eb');
  assert.ok(svg.includes('data-walking-network="a"'));
  assert.ok(svg.includes('stroke-dasharray="5 5"'));
  assert.equal((svg.match(/fill="none"/g) || []).length, 2);
  assert.ok(!svg.includes('fill-opacity'));
  assert.ok(!context.rangeSVG('a', '#2563eb').includes('<circle'));
  entries.a.status = 'loading';
  assert.equal(context.rangeSVG('a', '#2563eb'), '');
  entries.a.status = 'error';
  assert.equal(context.rangeSVG('a', '#2563eb'), '');
  setWalk(false);
  assert.equal(context.rangeSVG('a', '#2563eb'), '<circle/>');
  assert.equal(context.withinRange({lat: 2, lon: 2}), true);
});

 test('transit candidate proximity uses reached road segments, not a bounding polygon', () => {
 const road={type:'MultiLineString',coordinates:[[[139,35],[139.01,35]]]};
 assert.equal(walking.nearNetwork(road,{lat:35.0002,lon:139.005},.05),true);
 assert.equal(walking.nearNetwork(road,{lat:35.002,lon:139.005},.05),false);
 assert.equal(walking.nearNetwork(road,{lat:35,lon:139.02},.05),false);
 });
