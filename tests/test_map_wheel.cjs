const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const geometry = ['project', 'unproject', 'geoFromScreen', 'screen'].map(name => {
  const match = html.match(new RegExp(`^function ${name}\\(.*$`, 'm'));
  assert.ok(match, `Missing ${name}`);
  return match[0];
}).join('\n');
const handler = html.slice(html.indexOf('function handleMapWheel('), html.indexOf('function renderSoon('));
assert.ok(handler.startsWith('function handleMapWheel('));

function createMap(initialZoom = 12) {
  const context = vm.createContext({
    zoom: initialZoom,
    center: {lat: 35.681236, lon: 139.767125},
    RAD: Math.PI / 180,
    clamp: (value, lower, upper) => Math.min(upper, Math.max(lower, value)),
    $: () => ({getBoundingClientRect: () => ({left: 390, top: 20, width: 900, height: 700})}),
    renderSoon: () => {},
  });
  vm.runInContext(geometry + '\n' + handler, context);
  return context;
}

function wheel(context, overrides = {}) {
  let prevented = false;
  context.handleMapWheel({
    deltaX: 0, deltaY: -100, deltaMode: 0, clientX: 1050, clientY: 250,
    preventDefault() { prevented = true; },
    ...overrides,
  });
  return prevented;
}

test('small trackpad deltas change zoom proportionally', () => {
  const context = createMap();
  assert.equal(wheel(context, {deltaY: -1}), true);
  assert.ok(Math.abs(context.zoom - (12 + 1 / 300)) < 1e-10);
});

test('splitting a gesture into small events preserves its zoom amount', () => {
  const single = createMap();
  const burst = createMap();
  wheel(single);
  for (let index = 0; index < 100; index++) wheel(burst, {deltaY: -1});
  assert.ok(Math.abs(single.zoom - burst.zoom) < 1e-10);
});

test('the geographic location under the cursor stays fixed in both directions', () => {
  const context = createMap();
  const anchor = context.geoFromScreen(1050, 250);
  for (const deltaY of [-100, 40, 100]) {
    wheel(context, {deltaY});
    const actual = context.geoFromScreen(1050, 250);
    assert.ok(Math.abs(actual.lat - anchor.lat) < 1e-9);
    assert.ok(Math.abs(actual.lon - anchor.lon) < 1e-9);
  }
});

test('line and page wheel units normalize to pixels', () => {
  for (const [deltaY, deltaMode, pixels] of [[-3, 1, -48], [-0.1, 2, -70]]) {
    const context = createMap();
    wheel(context, {deltaY, deltaMode});
    assert.ok(Math.abs(context.zoom - (12 - pixels / 300)) < 1e-10);
  }
});

test('horizontal, zero and invalid deltas do not zoom', () => {
  for (const input of [{deltaY: 0}, {deltaY: NaN}, {deltaY: Infinity}, {deltaX: 100, deltaY: 1}]) {
    const context = createMap();
    assert.equal(wheel(context, input), false);
    assert.equal(context.zoom, 12);
  }
});

test('large deltas are capped and zoom respects its limits', () => {
  const context = createMap();
  wheel(context, {deltaY: -10000});
  assert.equal(context.zoom, 13);
  for (const [initialZoom, deltaY, expected] of [[17.9, -100, 18], [3.1, 100, 3], [18, -100, 18], [3, 100, 3]]) {
    const limited = createMap(initialZoom);
    const before = {...limited.center};
    wheel(limited, {deltaY});
    assert.equal(limited.zoom, expected);
    if (initialZoom === expected) assert.deepEqual({...limited.center}, before);
  }
});
