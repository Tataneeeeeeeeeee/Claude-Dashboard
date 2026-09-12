/* Tests for the conversation viewer's virtual scrolling and block rendering.
 *
 * Run with:  node --test tests/js/test_viewer.mjs
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';
import { makeDom } from './dom-stub.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const web = join(here, '..', '..', 'claude_dashboard', 'web');

/** Load viewer.js against a fresh stub DOM. */
function load() {
  const dom = makeDom();
  const sandbox = {
    window: dom.window,
    document: dom.document,
    requestAnimationFrame: dom.window.requestAnimationFrame,
    ResizeObserver: undefined,
    setTimeout,
    JSON,
    Math,
    Number,
    String,
    Array,
    Object,
    Set,
    Map,
    Date,
    console,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(readFileSync(join(web, 'viewer.js'), 'utf8'), sandbox);
  return { dom, Viewer: dom.window.ConversationViewer, helpers: dom.window.viewerHelpers };
}

/** Build a conversation payload of `count` alternating entries. */
function conversation(count, kindFor = (i) => (i % 2 ? 'assistant' : 'user')) {
  const messages = [];
  for (let i = 0; i < count; i += 1) {
    const kind = kindFor(i);
    messages.push({
      index: i,
      line: i + 1,
      uuid: `u${i}`,
      parent_uuid: i ? `u${i - 1}` : null,
      kind,
      role: kind === 'assistant' ? 'assistant' : 'user',
      timestamp: `2026-03-02T09:${String(i % 60).padStart(2, '0')}:00.000Z`,
      model: kind === 'assistant' ? 'claude-opus-5' : null,
      is_sidechain: false,
      is_meta: false,
      is_error: false,
      blocks: [{ type: 'text', text: `message ${i}` }],
      usage: kind === 'assistant' ? { input: 1, output: 2 } : null,
      extra: {},
    });
  }
  return {
    info: { title: 'Test session', cwd: '/tmp/x', git_branch: 'main', versions: ['2.1.263'] },
    meta: { message_count: count, tool_calls: 0, total_tokens: 10, estimated_cost: 0.1 },
    messages,
    errors: [],
  };
}

/* ------------------------------------------------------------- helpers */

test('toolSummary picks the most informative argument', () => {
  const { helpers } = load();
  assert.equal(helpers.toolSummary({ command: 'ls -la', description: 'list' }), 'ls -la');
  assert.equal(helpers.toolSummary({ file_path: '/a/b.py' }), '/a/b.py');
  assert.equal(helpers.toolSummary({ zzz: 1, yyy: 2 }), 'zzz, yyy');
  assert.equal(helpers.toolSummary(null), '');
  assert.equal(helpers.toolSummary('raw string'), 'raw string');
});

test('toolSummary collapses whitespace and caps the length', () => {
  const { helpers } = load();
  const summary = helpers.toolSummary({ command: 'a\n\n   b\tc' });
  assert.equal(summary, 'a b c');
  assert.ok(helpers.toolSummary({ command: 'x'.repeat(500) }).length <= 200);
});

test('resultText flattens strings, lists and image descriptors', () => {
  const { helpers } = load();
  assert.equal(helpers.resultText('plain'), 'plain');
  assert.equal(
    helpers.resultText([{ type: 'text', text: 'a' }, { type: 'text', text: 'b' }]),
    'a\nb',
  );
  const image = helpers.resultText([{ type: 'image', media_type: 'image/png', bytes: 2048 }]);
  assert.match(image, /image\/png/);
  // The separator is locale-dependent, so match the digits only.
  assert.match(image, /2\D?048 bytes/);
  assert.equal(helpers.resultText(null), '');
});

test('languageForTool maps Bash and file extensions', () => {
  const { helpers } = load();
  assert.equal(helpers.languageForTool('Bash', { command: 'ls' }), 'bash');
  assert.equal(helpers.languageForTool('Read', { file_path: 'a/b.py' }), 'python');
  assert.equal(helpers.languageForTool('Edit', { file_path: 'x.tsx' }), 'javascript');
  assert.equal(helpers.languageForTool('Write', { file_path: 'x.unknown' }), 'json');
  assert.equal(helpers.languageForTool('Other', {}), 'json');
});

/* ------------------------------------------------------- virtualisation */

test('only the visible window plus overscan is rendered', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(2000));
  // 800px of viewport over rows estimated at 110-190px: a few dozen at most.
  assert.ok(viewer.rendered.size > 0, 'nothing rendered');
  assert.ok(viewer.rendered.size < 60, `rendered ${viewer.rendered.size} rows`);
});

test('offsets are a strictly increasing prefix sum', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(500));
  assert.equal(viewer.offsets.length, 501);
  assert.equal(viewer.offsets[0], 0);
  for (let i = 1; i < viewer.offsets.length; i += 1) {
    assert.ok(viewer.offsets[i] > viewer.offsets[i - 1], `offset ${i} did not increase`);
  }
  assert.equal(viewer.totalHeight, viewer.offsets[500]);
});

test('indexAt binary-searches the row at a scroll position', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(100));
  assert.equal(viewer.indexAt(0), 0);
  for (const index of [1, 17, 42, 99]) {
    const middle = (viewer.offsets[index] + viewer.offsets[index + 1]) / 2;
    assert.equal(viewer.indexAt(middle), index);
  }
  // Past the end clamps to the last row rather than throwing.
  assert.equal(viewer.indexAt(viewer.totalHeight + 5000), 99);
});

test('scrolling moves the rendered window', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(1000));
  const firstWindow = [...viewer.rendered.keys()].sort((a, b) => a - b);

  dom.host.scrollTop = viewer.offsets[400];
  viewer.draw();
  const laterWindow = [...viewer.rendered.keys()].sort((a, b) => a - b);

  assert.ok(laterWindow[0] > firstWindow[0], 'window did not advance');
  assert.ok(laterWindow.includes(400) || laterWindow.includes(401));
});

test('rows far outside the window are discarded so memory stays flat', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(3000));
  for (let position = 0; position < 3000; position += 137) {
    dom.host.scrollTop = viewer.offsets[position];
    viewer.draw();
  }
  assert.ok(viewer.rendered.size < 150, `retained ${viewer.rendered.size} rows`);
});

test('measured heights replace the estimates and reflow the offsets', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(200));
  const before = viewer.totalHeight;

  // Model every rendered row as much shorter than the estimate.
  for (const node of viewer.rendered.values()) node.offsetHeight = 30;
  assert.equal(viewer.measureNow(), true);

  assert.ok(viewer.totalHeight < before, 'total height did not shrink');
  for (const [index, node] of viewer.rendered) {
    assert.equal(viewer.heights[index], 30);
    assert.equal(node.style.top, viewer.offsets[index] + 'px');
  }
  assert.equal(viewer.measureNow(), false, 'a second pass should be a no-op');
});

test('estimates for unmeasured rows are refined from measured ones', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(400, () => 'tool_result'));
  for (const node of viewer.rendered.values()) node.offsetHeight = 26;
  viewer.measureNow();

  const unmeasured = 399;
  assert.ok(!viewer.measured.has(unmeasured));
  assert.equal(viewer.heights[unmeasured], 26,
    'the far end of the list should inherit the measured average');
});

test('filters change which entries are visible', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(30, (i) => ['user', 'assistant', 'tool_result', 'attachment'][i % 4]);
  viewer.load(data);

  const withTools = viewer.visible.length;
  assert.ok(viewer.visible.every((m) => m.kind !== 'attachment'),
    'attachments are hidden by default');

  viewer.filter.tools = false;
  viewer.applyFilter();
  assert.ok(viewer.visible.length < withTools);
  assert.ok(viewer.visible.every((m) => m.kind !== 'tool_result'));

  viewer.filter.attachments = true;
  viewer.applyFilter();
  assert.ok(viewer.visible.some((m) => m.kind === 'attachment'));
});

/* ------------------------------------------------------------ jumping */

test('jumpTo finds a message by uuid and by line', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(500));

  assert.equal(viewer.jumpTo({ uuid: 'u321' }), true);
  assert.ok(dom.host.scrollTop > 0);

  dom.host.scrollTop = 0;
  assert.equal(viewer.jumpTo({ line: 77 }), true);
  assert.ok(dom.host.scrollTop > 0);
});

test('jumping converges on the target even when estimates are wrong', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(1200));

  // Model reality: every row is far shorter than the initial estimate, so
  // the first scroll attempt necessarily overshoots.
  const realHeight = 34;
  const originalRow = viewer.row.bind(viewer);
  viewer.row = (message, index) => {
    const node = originalRow(message, index);
    node.offsetHeight = realHeight;
    return node;
  };
  for (const node of viewer.rendered.values()) node.offsetHeight = realHeight;

  viewer.jumpTo({ uuid: 'u900' });

  // After convergence the target sits at the top of the viewport.
  const expected = Math.max(0, viewer.offsets[900] + viewer.headerHeight() - 12);
  assert.ok(
    Math.abs(dom.host.scrollTop - expected) <= 2,
    `scrollTop ${dom.host.scrollTop} did not settle on ${expected}`,
  );
  // And the target row is actually in the rendered window.
  assert.ok(viewer.rendered.has(900), 'the target row was not rendered');
});

test('jumpTo reports failure for an unknown target', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(10));
  assert.equal(viewer.jumpTo({ uuid: 'nope' }), false);
});

test('jumping to a filtered-out entry lands on the nearest earlier one', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(40, (i) => (i % 2 ? 'tool_result' : 'assistant')));
  viewer.filter.tools = false;
  viewer.applyFilter();
  viewer.draw(true);
  // Line 20 is a hidden tool_result; the jump should still succeed.
  assert.equal(viewer.jumpTo({ line: 20 }), true);
});

/* ------------------------------------------------------------ content */

test('an empty thinking block renders as a marker, not a disclosure', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [{ type: 'thinking', text: '' }];
  viewer.load(data);

  const row = viewer.rendered.get(0);
  assert.ok(row.querySelector('.v-thinking-empty'), 'expected the quiet marker');
  assert.equal(row.querySelector('.v-disclosure'), null, 'should not be expandable');
});

test('a thinking block with text is a disclosure with a word count', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [{ type: 'thinking', text: 'one two three' }];
  viewer.load(data);

  const row = viewer.rendered.get(0);
  assert.ok(row.querySelector('.v-disclosure'));
  assert.match(row.textContent, /3 words/);
});

test('one word is not pluralised', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [{ type: 'thinking', text: 'solo' }];
  viewer.load(data);
  assert.match(viewer.rendered.get(0).textContent, /1 word(?!s)/);
});

test('tool calls and results start collapsed', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [
    { type: 'tool_use', id: 't1', name: 'Bash', input: { command: 'ls -la' }, caller: 'direct' },
  ];
  viewer.load(data);

  const row = viewer.rendered.get(0);
  const body = row.querySelector('.v-disc-body');
  assert.equal(body.children.length, 0, 'body should be empty until opened');
  assert.match(row.textContent, /Bash/);
  assert.match(row.textContent, /ls -la/);
});

test('opening a disclosure builds its content lazily', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [
    { type: 'tool_result', tool_use_id: 't1', name: 'Read', is_error: false, content: 'file body' },
  ];
  viewer.load(data);

  const row = viewer.rendered.get(0);
  const head = row.querySelector('.v-disc-head');
  const body = row.querySelector('.v-disc-body');
  assert.equal(body.children.length, 0);

  head.fire('click');
  assert.equal(body.children.length, 1, 'content should appear on first open');
  assert.equal(head.getAttribute('aria-expanded'), 'true');

  head.fire('click');
  assert.equal(body.children.length, 0, 'content should be released on close');
  assert.equal(head.getAttribute('aria-expanded'), 'false');
});

test('expandAll and collapseAll flip every section', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(6, () => 'assistant');
  for (const message of data.messages) {
    message.blocks = [{ type: 'tool_use', id: 'x', name: 'Bash', input: { command: 'ls' } }];
  }
  viewer.load(data);
  assert.equal(viewer.expanded.size, 0);

  viewer.expandAll();
  assert.ok(viewer.expanded.size >= 6);

  viewer.collapseAll();
  assert.equal(viewer.expanded.size, 0);
});

test('a failed tool result is marked as bad', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(1, () => 'assistant');
  data.messages[0].blocks = [
    { type: 'tool_result', tool_use_id: 't', name: 'Bash', is_error: true, content: 'boom' },
  ];
  viewer.load(data);
  const row = viewer.rendered.get(0);
  assert.ok(row.querySelector('.v-bad-block'));
  assert.match(row.textContent, /failed/);
});

test('sidechain and error rows carry their marker classes', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  const data = conversation(2, () => 'assistant');
  data.messages[0].is_sidechain = true;
  data.messages[1].is_error = true;
  viewer.load(data);
  assert.ok(viewer.rendered.get(0).className.includes('v-sidechain'));
  assert.ok(viewer.rendered.get(1).className.includes('v-error'));
});

test('an empty conversation does not crash', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load({ info: {}, meta: null, messages: [], errors: [] });
  assert.equal(viewer.visible.length, 0);
  assert.equal(viewer.totalHeight, 0);
  assert.equal(viewer.jumpTo({ line: 1 }), false);
});

test('destroy releases the scroll listener', () => {
  const { dom, Viewer } = load();
  const viewer = new Viewer(dom.host);
  viewer.load(conversation(10));
  assert.ok(dom.host.listeners.get('scroll').size > 0);
  viewer.destroy();
  assert.equal(dom.host.listeners.get('scroll').size, 0);
  assert.equal(viewer.rendered.size, 0);
});
