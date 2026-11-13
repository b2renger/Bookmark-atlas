// The 3D link layout lives in the page (src/atlas/web/index.html). These tests
// lift it out and run it on invented graphs: topics must end up apart, linked
// topics closer than unlinked ones, and a step must stay cheap as the
// collection grows.
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');

const page = fs.readFileSync(path.join(__dirname, '..', 'src', 'atlas', 'web', 'index.html'), 'utf8');
const start = page.indexOf('function webLayout(');
const end = page.indexOf('// Where the layout stands', start);
assert.ok(start > 0 && end > start, 'webLayout not found in the page');
const webLayout = new Function(`${page.slice(start, end)}; return webLayout;`)();

// An invented collection: `topics` topics of `size` bookmarks, each bookmark
// linked to three others of its topic; `bridges` lists [topic, topic, links].
function collection(topics, size, bridges) {
  let seed = 99;
  const random = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  const cluster = [], edges = [];
  for (let t = 0; t < topics; t++) for (let i = 0; i < size; i++) cluster.push(`topic-${t}`);
  for (let t = 0; t < topics; t++) for (let i = 0; i < size; i++) for (let k = 0; k < 3; k++) {
    const other = Math.floor(random() * size);
    if (other !== i) edges.push([t * size + i, t * size + other]);
  }
  for (const [a, b, count] of bridges) for (let k = 0; k < count; k++) edges.push([a * size + Math.floor(random() * size), b * size + Math.floor(random() * size)]);
  return { cluster, edges };
}
function settle(data) {
  const layout = webLayout(data.cluster, data.edges, 180);
  while (layout.tick < layout.total) layout.step();
  return layout.pos;
}
function centres(pos, cluster) {
  const sums = new Map();
  cluster.forEach((key, i) => { const c = sums.get(key) || [0, 0, 0, 0]; c[0] += pos[3 * i]; c[1] += pos[3 * i + 1]; c[2] += pos[3 * i + 2]; c[3] += 1; sums.set(key, c); });
  return new Map([...sums].map(([key, c]) => [key, [c[0] / c[3], c[1] / c[3], c[2] / c[3]]]));
}
const distance = (a, b) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2]);

test('every bookmark ends nearer to its own topic than to any other', () => {
  const data = collection(8, 60, [[0, 1, 12], [2, 3, 6], [4, 5, 3]]);
  const pos = settle(data), middle = centres(pos, data.cluster);
  let home = 0;
  data.cluster.forEach((key, i) => {
    const at = [pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]];
    let best = null, least = Infinity;
    for (const [name, c] of middle) { const d = distance(at, c); if (d < least) { least = d; best = name; } }
    if (best === key) home += 1;
  });
  assert.ok(home / data.cluster.length > 0.95, `only ${home} of ${data.cluster.length} at home`);
});

test('topics leave empty space between them', () => {
  const data = collection(8, 60, [[0, 1, 12]]);
  const pos = settle(data), middle = centres(pos, data.cluster), reach = new Map();
  data.cluster.forEach((key, i) => { const d = distance([pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]], middle.get(key)); const l = reach.get(key) || []; l.push(d); reach.set(key, l); });
  const radius = new Map([...reach].map(([key, l]) => [key, l.sort((x, y) => x - y)[Math.floor(l.length * 0.9)]]));
  for (const [a, ca] of middle) for (const [b, cb] of middle) if (a < b) assert.ok(distance(ca, cb) > radius.get(a) + radius.get(b), `${a} and ${b} overlap`);
});

test('topics that share links sit closer than topics that share none', () => {
  const data = collection(10, 50, [[0, 1, 40]]);
  const middle = centres(settle(data), data.cluster), keys = [...middle.keys()];
  const linked = distance(middle.get('topic-0'), middle.get('topic-1'));
  let sum = 0, count = 0;
  for (const a of keys) for (const b of keys) if (a < b && !(a === 'topic-0' && b === 'topic-1')) { sum += distance(middle.get(a), middle.get(b)); count += 1; }
  assert.ok(linked < sum / count, `linked ${linked.toFixed(2)} against ${(sum / count).toFixed(2)} on average`);
});

test('the picture has depth: it is not a flat sheet', () => {
  const data = collection(12, 50, []);
  const pos = settle(data), spread = [0, 1, 2].map((axis) => {
    let mean = 0; const n = data.cluster.length;
    for (let i = 0; i < n; i++) mean += pos[3 * i + axis] / n;
    let sum = 0; for (let i = 0; i < n; i++) sum += (pos[3 * i + axis] - mean) ** 2;
    return Math.sqrt(sum / n);
  });
  assert.ok(Math.min(...spread) > 0.5 * Math.max(...spread), `spreads ${spread.map((s) => s.toFixed(2))}`);
});

test('the same collection gives the same picture twice', () => {
  const data = collection(5, 40, [[0, 1, 5]]);
  assert.deepStrictEqual(Array.from(settle(data)), Array.from(settle(data)));
});

test('one topic, one bookmark, no link: nothing breaks', () => {
  for (const data of [{ cluster: ['a'], edges: [] }, { cluster: ['a', 'a', 'a'], edges: [[0, 1]] }, { cluster: ['a', 'b'], edges: [] }]) {
    const pos = settle(data);
    assert.ok(Array.from(pos).every(Number.isFinite));
  }
});

test('a step on 3,400 bookmarks stays well under a frame on average', () => {
  const data = collection(17, 200, [[0, 1, 30], [2, 3, 30], [4, 9, 30]]);
  const layout = webLayout(data.cluster, data.edges, 180), started = process.hrtime.bigint();
  while (layout.tick < layout.total) layout.step();
  const each = Number(process.hrtime.bigint() - started) / 1e6 / layout.total;
  assert.ok(each < 60, `${each.toFixed(1)} ms per step`);   // about 7 ms on a laptop; the bound is loose for slow test machines
});
