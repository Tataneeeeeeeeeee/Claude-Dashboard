/* Unit tests for the bundled Markdown renderer and highlighter.
 *
 * Run with:  node --test tests/js/
 *
 * The renderer is loaded by evaluating the browser file in a context that
 * provides a `window`, which keeps the shipped file free of any module
 * system it would not otherwise need.
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, '..', '..', 'claude_dashboard', 'web', 'markdown.js'), 'utf8');
const sandbox = { window: {} };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const md = sandbox.window.md;

test('exposes the expected surface', () => {
  assert.equal(typeof md.render, 'function');
  assert.equal(typeof md.highlight, 'function');
  assert.equal(typeof md.escapeHtml, 'function');
});

/* ----------------------------------------------------------- escaping */

test('escapes every HTML-significant character', () => {
  assert.equal(md.escapeHtml(`<&>"'`), '&lt;&amp;&gt;&quot;&#39;');
  assert.equal(md.escapeHtml(null), '');
  assert.equal(md.escapeHtml(undefined), '');
});

test('never emits unescaped HTML from transcript text', () => {
  const html = md.render('A <script>alert(1)</script> tag and <img onerror=x>');
  assert.ok(!html.includes('<script'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;script&gt;'));
  // The literal text "onerror=" may survive, but only as escaped content,
  // never as a real attribute: no tag other than the ones we emit exists.
  const tags = (html.match(/<\/?([a-zA-Z][\w-]*)/g) || []).map((t) => t.replace(/<\/?/, ''));
  assert.deepEqual([...new Set(tags)].sort(), ['p']);
});

test('escapes HTML inside code fences too', () => {
  const html = md.render('```html\n<script>bad()</script>\n```');
  assert.ok(!html.includes('<script>bad'));
  assert.ok(html.includes('&lt;script'));
});

test('rejects javascript: and data: link targets', () => {
  const bad = md.render('[click](javascript:alert(1))');
  assert.ok(!bad.includes('href'));
  assert.equal(md.safeUrl('data:text/html,<script>'), '');
  assert.equal(md.safeUrl('https://example.com/x'), 'https://example.com/x');
  assert.equal(md.safeUrl('mailto:a@b.c'), 'mailto:a@b.c');
});

/* -------------------------------------------------------------- blocks */

test('renders headings at every level', () => {
  assert.ok(md.render('# One').includes('<h1>One</h1>'));
  assert.ok(md.render('###### Six').includes('<h6>Six</h6>'));
  assert.ok(md.render('## Closed ##').includes('<h2>Closed</h2>'));
  // Seven hashes is not a heading.
  assert.ok(!md.render('####### Seven').includes('<h7'));
});

test('renders paragraphs and keeps soft breaks', () => {
  const html = md.render('line one\nline two\n\nsecond paragraph');
  assert.equal((html.match(/<p>/g) || []).length, 2);
  assert.ok(html.includes('line one<br>line two'));
});

test('renders unordered and ordered lists', () => {
  const bullets = md.render('- alpha\n- beta\n- gamma');
  assert.ok(bullets.startsWith('<ul>'));
  assert.equal((bullets.match(/<li>/g) || []).length, 3);

  const numbers = md.render('1. first\n2. second');
  assert.ok(numbers.startsWith('<ol>'));
  assert.ok(numbers.includes('<li>first</li>'));
});

test('renders a fenced code block with language and copy button', () => {
  const html = md.render('```python\nprint("hi")\n```');
  assert.ok(html.includes('class="code-block"'));
  assert.ok(html.includes('>python<'));
  assert.ok(html.includes('data-copy'));
  assert.ok(html.includes('<pre><code>'));
});

test('an unlabelled fence is still a code block', () => {
  const html = md.render('```\nplain\n```');
  assert.ok(html.includes('>text<'));
  assert.ok(html.includes('plain'));
});

test('an unterminated fence does not swallow the renderer', () => {
  const html = md.render('```js\nconst a = 1;');
  assert.ok(html.includes('code-block'));
  assert.ok(html.includes('const'));
});

test('renders block quotes recursively', () => {
  const html = md.render('> quoted **bold**');
  assert.ok(html.includes('<blockquote>'));
  assert.ok(html.includes('<strong>bold</strong>'));
});

test('renders a table with alignment', () => {
  const html = md.render('| a | b |\n|:--|--:|\n| 1 | 2 |');
  assert.ok(html.includes('<table>'));
  assert.ok(html.includes('text-align:left'));
  assert.ok(html.includes('text-align:right'));
  assert.equal((html.match(/<td/g) || []).length, 2);
});

test('renders horizontal rules', () => {
  assert.ok(md.render('---').includes('<hr>'));
  assert.ok(md.render('***').includes('<hr>'));
});

/* -------------------------------------------------------------- inline */

test('renders emphasis, strong and strikethrough', () => {
  assert.ok(md.render('**bold**').includes('<strong>bold</strong>'));
  assert.ok(md.render('an *italic* word').includes('<em>italic</em>'));
  assert.ok(md.render('~~gone~~').includes('<del>gone</del>'));
});

test('inline code is protected from other inline rules', () => {
  const html = md.render('use `a_b_c` and `**not bold**`');
  assert.ok(html.includes('<code>a_b_c</code>'));
  assert.ok(html.includes('<code>**not bold**</code>'));
  assert.ok(!html.includes('<em>b</em>'));
});

test('underscores inside identifiers are not emphasis', () => {
  const html = md.render('call some_long_name now');
  assert.ok(!html.includes('<em>'));
});

test('renders links and bare URLs', () => {
  const linked = md.render('[docs](https://example.com/a)');
  assert.ok(linked.includes('href="https://example.com/a"'));
  const bare = md.render('see https://example.com/b now');
  assert.ok(bare.includes('<a href="https://example.com/b"'));
});

/* -------------------------------------------------------- highlighting */

test('highlights python keywords, strings and comments', () => {
  const html = md.highlight('def f():\n    # note\n    return "x"', 'python');
  assert.ok(html.includes('tok-k'));
  assert.ok(html.includes('tok-c'));
  assert.ok(html.includes('tok-s'));
});

test('highlights json keys separately from string values', () => {
  const html = md.highlight('{"a": "b", "n": 12, "t": true}', 'json');
  assert.ok(html.includes('tok-key'));
  assert.ok(html.includes('tok-n'));
  assert.ok(html.includes('tok-k'));
});

test('highlights diffs by line polarity', () => {
  const html = md.highlight('@@ -1 +1 @@\n-old\n+new', 'diff');
  assert.ok(html.includes('tok-add'));
  assert.ok(html.includes('tok-del'));
});

test('language aliases resolve', () => {
  assert.ok(md.highlight('const a = 1;', 'ts').includes('tok-k'));
  assert.ok(md.highlight('echo hi', 'zsh').includes('tok-'));
});

test('an unknown language is escaped but not highlighted', () => {
  const html = md.highlight('<x> & y', 'brainfuck');
  assert.equal(html, '&lt;x&gt; &amp; y');
});

test('highlighting never loses or duplicates characters', () => {
  const samples = [
    ['python', 'x = {"k": [1, 2]}  # trailing'],
    ['javascript', 'const s = `a${b}c`; // done'],
    ['bash', 'for f in *.txt; do echo "$f"; done'],
    ['json', '{"a":[1,2,{"b":null}]}'],
  ];
  for (const [language, code] of samples) {
    const stripped = md.highlight(code, language)
      .replace(/<[^>]+>/g, '')
      .replace(/&lt;/g, '<').replace(/&gt;/g, '>')
      .replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/&amp;/g, '&');
    assert.equal(stripped, code, `round trip failed for ${language}`);
  }
});

/* --------------------------------------------------------------- edges */

test('handles empty and odd input', () => {
  assert.equal(md.render(''), '');
  assert.equal(md.render(null), '');
  assert.equal(md.render('   \n\n  '), '');
});

test('handles a very long line without hanging', () => {
  const long = 'word '.repeat(50000);
  const started = Date.now();
  const html = md.render(long);
  assert.ok(html.length > 0);
  assert.ok(Date.now() - started < 3000, 'renderer took too long');
});

test('renders a realistic assistant message end to end', () => {
  const source = [
    'Here is the fix.',
    '',
    '## Steps',
    '',
    '1. Edit `config.py`',
    '2. Run the tests',
    '',
    '```bash',
    'pytest -q tests/',
    '```',
    '',
    '> Note: this touches **two** modules.',
  ].join('\n');
  const html = md.render(source);
  assert.ok(html.includes('<h2>Steps</h2>'));
  assert.ok(html.includes('<ol>'));
  assert.ok(html.includes('<code>config.py</code>'));
  assert.ok(html.includes('code-block'));
  assert.ok(html.includes('<blockquote>'));
  assert.ok(html.includes('<strong>two</strong>'));
});
