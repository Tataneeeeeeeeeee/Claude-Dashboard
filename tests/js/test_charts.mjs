/* Tests for the SVG chart primitives.
 *
 * These check the specifications that are easy to break silently: the bar
 * thickness cap, the rounded data-end, the surface gap between stacked
 * segments, tick rounding, legend rules, and label-fit decisions.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import { makeDom } from './dom-stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, '..', '..', 'agentboard', 'web');

/** Load charts.js against a fresh stub DOM. */
function load() {
  const dom = makeDom();
  const sandbox = {
    window: dom.window,
    document: dom.document,
    getComputedStyle: () => ({ getPropertyValue: () => '#2a78d6' }),
    JSON, Math, Number, String, Array, Object, Set, Map, Date, console,
  };
  sandbox.window.innerWidth = 1400;
  sandbox.window.innerHeight = 900;
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(join(web, 'charts.js'), 'utf8'), sandbox);
  return { dom, charts: dom.window.charts };
}

/** Sample periods. */
function periods(count = 10) {
  return Array.from({ length: count }, (_, i) => ({
    period: `2026-03-${String(i + 1).padStart(2, '0')}`,
    input: 100 * (i + 1),
    output: 50 * (i + 1),
    cache_write: 20 * (i + 1),
    cache_read: 900 * (i + 1),
    cost: (i + 1) * 1.5,
    messages: i * 3,
    sessions: (i % 4) + 1,
    tool_calls: i * 7,
    total_tokens: 1070 * (i + 1),
  }));
}

const SERIES = [
  { key: 'input', label: 'Input', color: '#2a78d6' },
  { key: 'output', label: 'Output', color: '#eb6834' },
  { key: 'cache_write', label: 'Cache write', color: '#1baf7a' },
  { key: 'cache_read', label: 'Cache read', color: '#eda100' },
];

/* ------------------------------------------------------------ numbers */

test('niceTicks rounds to clean values covering the range', () => {
  const { charts } = load();
  // Arrays cross a vm realm boundary, so compare contents, not identity.
  assert.deepEqual([...charts.niceTicks(0)], [0]);
  const ticks = [...charts.niceTicks(970)];
  assert.equal(ticks[0], 0);
  assert.ok(ticks[ticks.length - 1] >= 970, 'the top tick must cover the max');
  // Every step is the same, and a round number.
  const step = ticks[1] - ticks[0];
  for (let i = 1; i < ticks.length; i += 1) {
    assert.ok(Math.abs((ticks[i] - ticks[i - 1]) - step) < 1e-9);
  }
  assert.ok([100, 200, 250, 500].includes(step), `unexpected step ${step}`);
});

test('compact and money read sensibly at every magnitude', () => {
  const { charts } = load();
  assert.equal(charts.compact(999), '999');
  assert.equal(charts.compact(1500), '1.5k');
  assert.equal(charts.compact(1_500_000), '1.5M');
  assert.equal(charts.compact(2_500_000_000), '2.5B');
  assert.equal(charts.money(0), '$0');
  assert.equal(charts.money(0.004), '<$0.01');
  assert.equal(charts.money(12.345), '$12.35');
  assert.equal(charts.money(-5), '$-5.00');
});

test('shortPeriod formats days, ISO weeks and months', () => {
  const { charts } = load();
  // Month names are locale-dependent, so assert the shape, not English.
  const day = charts.shortPeriod('2026-03-02');
  assert.ok(day.includes('2') && day !== '2026-03-02', `unexpected day label ${day}`);
  assert.equal(charts.shortPeriod('2026-W10'), 'W10');
  const month = charts.shortPeriod('2026-03');
  assert.ok(month.length <= 10 && month !== '2026-03', `unexpected month label ${month}`);
  assert.equal(charts.shortPeriod('whatever'), 'whatever');
});

test('resolveWidth rejects a width too small to be a real measurement', () => {
  const { charts } = load();
  assert.equal(charts.resolveWidth(880), 880);
  assert.equal(charts.resolveWidth(0), 720);
  assert.equal(charts.resolveWidth(undefined), 720);
  assert.equal(charts.resolveWidth(10), 720);
});

/* --------------------------------------------------------- column chart */

test('the svg is sized 1:1 so mark specs stay literal', () => {
  const { charts } = load();
  const node = charts.columnChart({ rows: periods(), series: SERIES, width: 900, height: 250 });
  const root = node.children[0];
  assert.equal(root.tagName, 'SVG');
  assert.equal(root.getAttribute('width'), '900');
  assert.equal(root.getAttribute('height'), '250');
  assert.equal(root.getAttribute('viewBox'), '0 0 900 250');
  // A stretched viewBox would scale a 24px bar to something else.
  assert.equal(root.getAttribute('preserveAspectRatio'), null);
});

test('bars never exceed the 24px cap however wide the band', () => {
  const { charts } = load();
  // Three columns across 900px gives a very wide band.
  const node = charts.columnChart({ rows: periods(3), series: SERIES, width: 900 });
  const bars = node.children[0].all('bar');
  assert.ok(bars.length > 0);
  for (const bar of bars) {
    const width = pathWidth(bar.getAttribute('d'));
    assert.ok(width <= 24 + 0.001, `bar width ${width} exceeds the 24px cap`);
  }
});

/** Extract the drawn width from a rounded-top-rect path. */
function pathWidth(d) {
  const move = /^M([-\d.]+),([-\d.]+)/.exec(d);
  const plain = /^M[-\d.]+,[-\d.]+h([-\d.]+)/.exec(d);
  if (plain) return Math.abs(Number(plain[1]));
  const horizontal = /h([-\d.]+)a/.exec(d);
  const radius = /a([-\d.]+),/.exec(d);
  if (horizontal && radius) return Number(horizontal[1]) + Number(radius[1]) * 2;
  const last = /h(-[\d.]+)Z$/.exec(d);
  if (last) return Math.abs(Number(last[1]));
  assert.fail(`could not read a width from ${d} (${move})`);
  return 0;
}

test('only the topmost segment of a stack gets the rounded data-end', () => {
  const { charts } = load();
  const node = charts.columnChart({ rows: periods(4), series: SERIES, width: 900 });
  const bars = node.children[0].all('bar');
  // Four series per column, so every fourth path is the top of a stack.
  const rounded = bars.filter((b) => b.getAttribute('d').includes('a'));
  const square = bars.filter((b) => !b.getAttribute('d').includes('a'));
  assert.equal(rounded.length, 4, 'one rounded cap per column');
  assert.ok(square.length > 0, 'interior segments stay square');
});

test('a surface gap separates touching segments', () => {
  const { charts } = load();
  const node = charts.columnChart({
    rows: [{ period: 'p', a: 100, b: 100 }],
    series: [{ key: 'a', label: 'A', color: '#111' }, { key: 'b', label: 'B', color: '#222' }],
    width: 600, height: 200,
  });
  const bars = node.children[0].all('bar');
  assert.equal(bars.length, 2);
  const tops = bars.map((b) => Number(/^M[-\d.]+,([-\d.]+)/.exec(b.getAttribute('d'))[1]));
  const heights = bars.map(segmentHeight);
  // The upper segment starts 2px above where the lower one ends.
  const lowerTop = Math.max(...tops);
  const upperBottom = Math.min(...tops) + heights[tops.indexOf(Math.min(...tops))];
  assert.ok(Math.abs(lowerTop - upperBottom) >= 1.9, 'expected a ~2px surface gap');
});

/** Height of a segment path. */
function segmentHeight(node) {
  const d = node.getAttribute('d');
  const plain = /v([-\d.]+)h/.exec(d);
  return plain ? Math.abs(Number(plain[1])) : 0;
}

test('a legend appears for several series and not for one', () => {
  const { charts } = load();
  const many = charts.columnChart({ rows: periods(), series: SERIES, width: 800 });
  assert.ok(many.querySelector('.chart-legend'), 'multi-series charts need a legend');

  const single = charts.columnChart({
    rows: periods(), series: [SERIES[0]], width: 800,
  });
  assert.equal(single.querySelector('.chart-legend'), null,
    'a single series is named by the title, not a one-swatch box');
});

test('only the final column is direct-labelled', () => {
  const { charts } = load();
  const node = charts.columnChart({ rows: periods(8), series: SERIES, width: 900 });
  const labels = node.children[0].all('value-label');
  assert.equal(labels.length, 1, 'a number on every column is noise');
});

test('an empty range says so instead of drawing an empty axis', () => {
  const { charts } = load();
  const node = charts.columnChart({ rows: [], series: SERIES });
  assert.match(node.textContent, /No activity/);
});

test('x-axis labels are thinned so they cannot collide', () => {
  const { charts } = load();
  const node = charts.columnChart({ rows: periods(60), series: SERIES, width: 600 });
  const texts = node.children[0].all('axis-text');
  // Ticks plus x labels; the x labels alone must be far fewer than 60.
  assert.ok(texts.length < 40, `too many axis labels: ${texts.length}`);
});

/* ------------------------------------------------------------- area */

test('the area chart draws a 2px line, a wash and an end marker', () => {
  const { charts } = load();
  const node = charts.areaChart({ rows: periods(), yKey: 'cost', color: '#2a78d6', width: 800 });
  const root = node.children[0];
  assert.equal(root.all('line-mark').length, 1);
  assert.equal(root.all('area-fill').length, 1);
  const dot = root.all('end-dot')[0];
  assert.ok(Number(dot.getAttribute('r')) >= 4, 'markers must be at least 8px across');
});

test('a single-series area chart carries no legend', () => {
  const { charts } = load();
  const node = charts.areaChart({ rows: periods(), yKey: 'cost', color: '#2a78d6' });
  assert.equal(node.querySelector('.chart-legend'), null);
});

test('a one-point series does not divide by zero', () => {
  const { charts } = load();
  const node = charts.areaChart({ rows: periods(1), yKey: 'cost', color: '#2a78d6' });
  const d = node.children[0].all('line-mark')[0].getAttribute('d');
  assert.ok(!d.includes('NaN'), `path contains NaN: ${d}`);
});

/* -------------------------------------------------------- horizontal */

test('horizontal bars cap the row count and say so', () => {
  const { charts } = load();
  const rows = Array.from({ length: 30 }, (_, i) => ({ label: `row ${i}`, value: 30 - i }));
  const node = charts.barChart({ rows, color: '#2a78d6', maxRows: 12 });
  assert.equal(node.querySelector('.hbar-list').children.length, 12);
  assert.match(node.textContent, /top 12 of 30/);
});

test('a zero-value row still renders a visible sliver, not nothing', () => {
  const { charts } = load();
  const node = charts.barChart({
    rows: [{ label: 'big', value: 100 }, { label: 'tiny', value: 0 }],
    color: '#2a78d6',
  });
  const fills = node.all('hbar-fill');
  assert.equal(fills.length, 2);
  assert.ok(fills[1].getAttribute('style').includes('width:0.6%'));
});

/* --------------------------------------------------------- share bar */

test('the share bar labels only segments wide enough to hold the text', () => {
  const { charts } = load();
  const node = charts.shareBar([
    { label: 'Input', value: 1, color: '#2a78d6' },
    { label: 'Cache read', value: 99, color: '#eda100' },
  ]);
  const segments = node.all('sharebar-seg');
  assert.equal(segments.length, 2);
  assert.equal(segments[0].children.length, 0, '1% is too narrow for an inside label');
  assert.equal(segments[1].children.length, 1, '99% has room');
  assert.match(segments[1].textContent, /99%/);
});

test('the share bar skips empty classes and reports an empty total', () => {
  const { charts } = load();
  const node = charts.shareBar([
    { label: 'A', value: 0, color: '#111' },
    { label: 'B', value: 5, color: '#222' },
  ]);
  assert.equal(node.all('sharebar-seg').length, 1);

  const empty = charts.shareBar([{ label: 'A', value: 0, color: '#111' }]);
  assert.match(empty.textContent, /Nothing recorded/);
});

/* ----------------------------------------------------------- heatmap */

test('the heatmap is 7 rows of 24 cells on six discrete steps', () => {
  const { charts } = load();
  const grid = Array.from({ length: 7 }, (_, day) =>
    Array.from({ length: 24 }, (_, hour) => day * hour));
  const node = charts.heatmap({
    weekdays: ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'],
    grid, peak: 6 * 23, total: 1,
  });
  const rows = node.querySelector('.heatmap').children;
  assert.equal(rows.length, 8, 'an hour ruler plus seven weekdays');
  const cells = node.all('heatmap-cell').filter((c) => c.parent.className.includes('heatmap-row'));
  assert.equal(cells.length, 7 * 24);
  const steps = new Set(cells.map((c) => c.getAttribute('data-step')));
  for (const step of steps) {
    assert.ok(Number(step) >= 0 && Number(step) <= 6, `step ${step} out of range`);
  }
  assert.ok(steps.has('0'), 'empty hours stay at the surface');
});

/* -------------------------------------------------------- sparkline */

test('a sparkline needs at least two points', () => {
  const { charts } = load();
  assert.equal(charts.sparkline([5], '#000'), null);
  assert.equal(charts.sparkline([], '#000'), null);
  assert.ok(charts.sparkline([1, 2, 3], '#000'));
});

test('a sparkline keeps only the last twelve points', () => {
  const { charts } = load();
  const node = charts.sparkline(Array.from({ length: 40 }, (_, i) => i), '#000', 110);
  const d = node.all('spark-line')[0].getAttribute('d');
  assert.equal((d.match(/[ML]/g) || []).length, 12);
});

/* ------------------------------------------------------------- table */

test('the table twin renders a header and numeric alignment', () => {
  const { charts } = load();
  const node = charts.table(['Name', 'Count'], [['Bash', '3,720'], ['Read', '313']]);
  assert.match(node.textContent, /Bash/);
  assert.match(node.textContent, /3,720/);
  const numeric = node.all('num');
  assert.equal(numeric.length, 2, 'value columns align, the label column does not');
});
