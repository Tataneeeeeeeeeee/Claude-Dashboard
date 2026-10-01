/* Minimal Markdown renderer and syntax highlighter.
 *
 * Written here rather than pulled from a CDN because the application must
 * work with no network at all.  It covers the subset that actually appears
 * in AI assistant transcripts: fenced and indented code, ATX headings,
 * lists, block quotes, tables, horizontal rules, and inline emphasis,
 * code, links and strikethrough.
 *
 * Safety: the input is untrusted transcript text, so nothing is ever
 * inserted as raw HTML.  Every leaf is escaped before being wrapped in
 * tags, and link targets are restricted to http, https, mailto and file.
 *
 * Exposes `window.md = { render, highlight, escapeHtml }`.
 */
'use strict';

(function () {
  /** Escape the five characters that matter inside HTML text and attributes. */
  function escapeHtml(text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  /** Allow only schemes that cannot execute script. */
  function safeUrl(url) {
    const trimmed = String(url || '').trim();
    if (/^(https?:|mailto:|file:)/i.test(trimmed)) return trimmed;
    if (/^[./#]/.test(trimmed)) return trimmed;
    return '';
  }

  /* ------------------------------------------------------- highlighting */

  /* Each language is a list of [regex, css-class] pairs, applied in order.
     The regexes are combined into one alternation so a match is claimed by
     exactly one rule, which avoids highlighting inside strings. */
  const LANGUAGES = {
    python: [
      [/(#[^\n]*)/, 'c'],
      [/("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/, 's'],
      [/\b(def|class|return|if|elif|else|for|while|import|from|as|with|try|except|finally|raise|yield|lambda|pass|break|continue|global|nonlocal|assert|del|async|await|in|is|not|and|or|None|True|False|self)\b/, 'k'],
      [/\b(0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?)\b/, 'n'],
      [/\b([A-Za-z_]\w*)(?=\s*\()/, 'f'],
    ],
    javascript: [
      [/(\/\/[^\n]*|\/\*[\s\S]*?\*\/)/, 'c'],
      [/(`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/, 's'],
      [/\b(const|let|var|function|return|if|else|for|while|do|switch|case|default|break|continue|class|extends|new|this|typeof|instanceof|in|of|try|catch|finally|throw|async|await|yield|import|export|from|as|delete|void|null|undefined|true|false)\b/, 'k'],
      [/\b(0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?)\b/, 'n'],
      [/\b([A-Za-z_$][\w$]*)(?=\s*\()/, 'f'],
    ],
    bash: [
      [/(#[^\n]*)/, 'c'],
      [/("(?:\\.|[^"\\])*"|'[^']*')/, 's'],
      [/(\$\{[^}]*\}|\$[A-Za-z_]\w*|\$\d)/, 'v'],
      [/\b(if|then|else|elif|fi|for|while|do|done|case|esac|function|return|in|select|until|local|export|source|alias|set|unset|trap|shift|exit)\b/, 'k'],
      [/(^|\n)\s*([a-zA-Z_][\w.-]*)(?=\s)/, 'f'],
    ],
    json: [
      [/("(?:\\.|[^"\\])*")(\s*:)/, 'key'],
      [/("(?:\\.|[^"\\])*")/, 's'],
      [/\b(true|false|null)\b/, 'k'],
      [/(-?\d+\.?\d*(?:[eE][+-]?\d+)?)/, 'n'],
    ],
    html: [
      [/(<!--[\s\S]*?-->)/, 'c'],
      [/(<\/?[A-Za-z][\w-]*)/, 'k'],
      [/([\w-]+)(?==")/, 'f'],
      [/("(?:\\.|[^"\\])*")/, 's'],
    ],
    css: [
      [/(\/\*[\s\S]*?\*\/)/, 'c'],
      [/("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/, 's'],
      [/(--[\w-]+|[a-z-]+)(?=\s*:)/, 'f'],
      [/(#[0-9a-fA-F]{3,8}\b|\b\d+\.?\d*(px|rem|em|%|vh|vw|s|ms)?\b)/, 'n'],
    ],
    sql: [
      [/(--[^\n]*)/, 'c'],
      [/('(?:''|[^'])*')/, 's'],
      [/\b(SELECT|FROM|WHERE|INSERT|INTO|VALUES|UPDATE|SET|DELETE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|CREATE|TABLE|INDEX|DROP|ALTER|AND|OR|NOT|NULL|AS|DISTINCT|COUNT|SUM|AVG|MIN|MAX)\b/i, 'k'],
      [/\b(\d+\.?\d*)\b/, 'n'],
    ],
    diff: [
      [/(^|\n)(\+[^\n]*)/, 'add'],
      [/(^|\n)(-[^\n]*)/, 'del'],
      [/(^|\n)(@@[^\n]*)/, 'k'],
    ],
  };

  /** Language aliases seen in real transcripts. */
  const ALIASES = {
    py: 'python', python3: 'python',
    js: 'javascript', jsx: 'javascript', ts: 'javascript', tsx: 'javascript',
    node: 'javascript', mjs: 'javascript',
    sh: 'bash', shell: 'bash', zsh: 'bash', console: 'bash', terminal: 'bash',
    yml: 'json', yaml: 'json', jsonc: 'json',
    xml: 'html', svg: 'html', vue: 'html',
    scss: 'css', less: 'css',
    postgres: 'sql', psql: 'sql', mysql: 'sql',
    patch: 'diff',
  };

  const compiled = new Map();

  /** Build (and memoise) the combined regex for a language. */
  function rulesFor(language) {
    const name = ALIASES[language] || language;
    if (!LANGUAGES[name]) return null;
    if (compiled.has(name)) return compiled.get(name);
    const rules = LANGUAGES[name];
    const source = rules.map(([regex]) => `(?:${regex.source})`).join('|');
    const flags = rules.some(([regex]) => regex.flags.includes('i')) ? 'gi' : 'g';
    const entry = { pattern: new RegExp(source, flags), rules };
    compiled.set(name, entry);
    return entry;
  }

  /**
   * Highlight a code string.
   * @returns escaped HTML, with `<span class="tok-*">` around matches.
   */
  function highlight(code, language) {
    const entry = language ? rulesFor(String(language).toLowerCase()) : null;
    if (!entry) return escapeHtml(code);

    // Index of each rule's first capture group inside the combined pattern.
    // `exec('')` on an always-matching copy reports 1 + the group count.
    const starts = [];
    let group = 1;
    for (const [regex] of entry.rules) {
      starts.push(group);
      group += new RegExp(regex.source + '|').exec('').length - 1;
    }

    let out = '';
    let last = 0;
    entry.pattern.lastIndex = 0;
    let match;
    while ((match = entry.pattern.exec(code)) !== null) {
      if (match[0] === '') { entry.pattern.lastIndex += 1; continue; }
      let ruleIndex = -1;
      for (let i = 0; i < starts.length; i += 1) {
        if (match[starts[i]] !== undefined) { ruleIndex = i; break; }
      }
      if (ruleIndex === -1) continue;
      const cls = entry.rules[ruleIndex][1];
      const text = match[0];
      // Some rules capture leading context (a newline, a colon); keep it plain.
      const captured = match[starts[ruleIndex]];
      const offset = text.indexOf(captured);
      out += escapeHtml(code.slice(last, match.index));
      if (offset > 0) out += escapeHtml(text.slice(0, offset));
      out += `<span class="tok-${cls}">${escapeHtml(captured)}</span>`;
      out += escapeHtml(text.slice(offset + captured.length));
      last = match.index + text.length;
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  /* ----------------------------------------------------------- inline */

  /** Render inline markup inside an already block-split line. */
  function inline(text) {
    // Inline code first: its contents must not be touched by other rules.
    const codes = [];
    let work = String(text).replace(/(`+)([\s\S]*?)\1/g, (_all, _ticks, body) => {
      codes.push(body);
      return `\u0000${codes.length - 1}\u0000`;
    });

    work = escapeHtml(work);

    // Links and bare URLs.
    work = work.replace(/\[([^\]]*)\]\(([^)\s]+)(?:\s+&quot;[^&]*&quot;)?\)/g,
      (all, label, url) => {
        const href = safeUrl(url.replace(/&amp;/g, '&'));
        return href ? `<a href="${escapeHtml(href)}" data-external="1">${label}</a>` : all;
      });
    work = work.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      (_all, lead, url) => `${lead}<a href="${escapeHtml(url)}" data-external="1">${url}</a>`);

    work = work.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    work = work.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    work = work.replace(/(^|\W)\*([^*\n]+)\*($|\W)/g, '$1<em>$2</em>$3');
    work = work.replace(/(^|\W)_([^_\n]+)_($|\W)/g, '$1<em>$2</em>$3');
    work = work.replace(/~~([^~]+)~~/g, '<del>$1</del>');

    return work.replace(/\u0000(\d+)\u0000/g,
      (_all, index) => `<code>${escapeHtml(codes[Number(index)])}</code>`);
  }

  /* ------------------------------------------------------------ blocks */

  /**
   * Render Markdown to HTML.
   * @param {string} source
   * @returns {string} HTML string; all text content is escaped.
   */
  function render(source) {
    const lines = String(source === null || source === undefined ? '' : source).split('\n');
    const out = [];
    let index = 0;

    /** Collect consecutive lines while `test` holds. */
    const take = (test) => {
      const collected = [];
      while (index < lines.length && test(lines[index])) {
        collected.push(lines[index]);
        index += 1;
      }
      return collected;
    };

    while (index < lines.length) {
      const line = lines[index];

      // Fenced code.
      const fence = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/.exec(line);
      if (fence) {
        const [, indent, marker, language] = fence;
        index += 1;
        const body = [];
        while (index < lines.length
               && !new RegExp(`^\\s*${marker[0]}{${marker.length},}\\s*$`).test(lines[index])) {
          body.push(lines[index].startsWith(indent) ? lines[index].slice(indent.length) : lines[index]);
          index += 1;
        }
        index += 1;                                  // closing fence
        const code = body.join('\n');
        out.push(codeBlock(code, language));
        continue;
      }

      if (!line.trim()) { index += 1; continue; }

      // ATX heading.
      const heading = /^(#{1,6})\s+(.*)$/.exec(line);
      if (heading) {
        const level = heading[1].length;
        out.push(`<h${level}>${inline(heading[2].replace(/\s+#+\s*$/, ''))}</h${level}>`);
        index += 1;
        continue;
      }

      // Horizontal rule.
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
        out.push('<hr>');
        index += 1;
        continue;
      }

      // Block quote.
      if (/^\s*>/.test(line)) {
        const quoted = take((l) => /^\s*>/.test(l) || (l.trim() && !/^\s*$/.test(l)));
        const inner = quoted.map((l) => l.replace(/^\s*>\s?/, '')).join('\n');
        out.push(`<blockquote>${render(inner)}</blockquote>`);
        continue;
      }

      // Table: a header row followed by a delimiter row.
      if (line.includes('|') && index + 1 < lines.length
          && /^\s*\|?[\s:-]*-[\s:|-]*\|?\s*$/.test(lines[index + 1])
          && lines[index + 1].includes('-')) {
        const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '')
          .split('|').map((c) => c.trim());
        const header = cells(line);
        const aligns = cells(lines[index + 1]).map((spec) => {
          if (/^:.*:$/.test(spec)) return 'center';
          if (/:$/.test(spec)) return 'right';
          return 'left';
        });
        index += 2;
        const body = take((l) => l.includes('|') && l.trim());
        const head = header
          .map((cell, i) => `<th style="text-align:${aligns[i] || 'left'}">${inline(cell)}</th>`)
          .join('');
        const rows = body.map((row) => '<tr>' + cells(row)
          .map((cell, i) => `<td style="text-align:${aligns[i] || 'left'}">${inline(cell)}</td>`)
          .join('') + '</tr>').join('');
        out.push(`<table><thead><tr>${head}</tr></thead><tbody>${rows}</tbody></table>`);
        continue;
      }

      // Lists.
      const bullet = /^(\s*)([-*+]|\d+[.)])\s+/.exec(line);
      if (bullet) {
        const ordered = /\d/.test(bullet[2]);
        const items = [];
        let current = null;
        while (index < lines.length) {
          const item = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/.exec(lines[index]);
          if (item) {
            if (current) items.push(current);
            current = item[3];
            index += 1;
          } else if (lines[index].trim() && /^\s{2,}/.test(lines[index]) && current !== null) {
            current += '\n' + lines[index].trim();   // lazy continuation
            index += 1;
          } else {
            break;
          }
        }
        if (current !== null) items.push(current);
        const tag = ordered ? 'ol' : 'ul';
        out.push(`<${tag}>` + items.map((i) => `<li>${inline(i)}</li>`).join('') + `</${tag}>`);
        continue;
      }

      // Indented code block.
      if (/^(\t| {4})/.test(line)) {
        const body = take((l) => /^(\t| {4})/.test(l) || !l.trim());
        while (body.length && !body[body.length - 1].trim()) body.pop();
        out.push(codeBlock(body.map((l) => l.replace(/^(\t| {4})/, '')).join('\n'), ''));
        continue;
      }

      // Paragraph.
      const paragraph = take((l) => l.trim()
        && !/^\s*(#{1,6}\s|>|```|~~~)/.test(l)
        && !/^(\s*)([-*+]|\d+[.)])\s+/.test(l));
      out.push(`<p>${inline(paragraph.join('\n')).replace(/\n/g, '<br>')}</p>`);
    }

    return out.join('\n');
  }

  /** A `<pre>` with a language label and a copy button. */
  function codeBlock(code, language) {
    const name = (language || '').toLowerCase();
    const label = name ? escapeHtml(name) : 'text';
    return (
      '<div class="code-block">'
      + `<div class="code-head"><span class="code-lang">${label}</span>`
      + '<button class="code-copy" type="button" data-copy>Copy</button></div>'
      + `<pre><code>${highlight(code, name)}</code></pre>`
      + '</div>'
    );
  }

  window.md = { render, highlight, escapeHtml, safeUrl };
}());
