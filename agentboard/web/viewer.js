/* Chat-style conversation viewer with virtualised scrolling.
 *
 * The largest transcript on the reference install parses to 2 125 entries.
 * Rendering them all at once is slow and janky, so only the rows inside the
 * viewport (plus a margin) are in the DOM at any moment.
 *
 * Heights are unknown until a row is rendered, so the scroller starts from a
 * per-kind estimate and replaces it with the real measurement as soon as the
 * row has been on screen once.  That keeps the scrollbar honest without
 * needing to render everything up front.
 *
 * Exposes `window.ConversationViewer`.
 */
'use strict';

(function () {
  /** Rough starting heights in pixels, refined once a row is measured. */
  const ESTIMATED_HEIGHT = {
    user: 110,
    assistant: 190,
    tool_result: 52,
    system: 56,
    attachment: 44,
  };

  /** Rows rendered above and below the viewport, to absorb fast scrolling. */
  const OVERSCAN = 6;

  /** Tool input keys worth showing in the collapsed one-line summary. */
  const SUMMARY_KEYS = [
    'command', 'file_path', 'path', 'pattern', 'query', 'url',
    'prompt', 'description', 'old_string', 'skill', 'name',
  ];

  const $ = (selector, root = document) => root.querySelector(selector);

  /** Build an element; mirrors the helper in app.js. */
  function el(tag, attrs = {}, children = []) {
    const [name, ...classes] = tag.split('.');
    const node = document.createElement(name);
    if (classes.length) node.className = classes.join(' ');
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key === 'dataset') Object.assign(node.dataset, value);
      else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, value);
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Local time, seconds included: transcripts are dense. */
  function clockTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return '';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  /** Full local timestamp for tooltips. */
  function fullTime(iso) {
    if (!iso) return '';
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? '' : date.toLocaleString();
  }

  /** Compact integer, e.g. 12345 -> "12.3k". */
  function compact(value) {
    const n = Number(value) || 0;
    if (Math.abs(n) < 1000) return String(n);
    if (Math.abs(n) < 1e6) return (n / 1e3).toFixed(1) + 'k';
    return (n / 1e6).toFixed(1) + 'M';
  }

  /** One-line gist of a tool call's arguments. */
  function toolSummary(input) {
    if (input === null || input === undefined) return '';
    if (typeof input === 'string') return input.slice(0, 200);
    if (typeof input !== 'object') return String(input).slice(0, 200);
    for (const key of SUMMARY_KEYS) {
      const value = input[key];
      if (typeof value === 'string' && value.trim()) {
        return ' '.concat(value).replace(/\s+/g, ' ').trim().slice(0, 200);
      }
    }
    const keys = Object.keys(input);
    return keys.length ? keys.slice(0, 4).join(', ') : '';
  }

  /** Flatten a tool_result payload to text for display. */
  function resultText(content) {
    if (typeof content === 'string') return content;
    if (Array.isArray(content)) {
      return content.map((item) => {
        if (!item || typeof item !== 'object') return String(item);
        if (item.type === 'text') return String(item.text || '');
        if (item.type === 'image') {
          return `[image · ${item.media_type || 'image'} · ${(item.bytes || 0).toLocaleString()} bytes]`;
        }
        return JSON.stringify(item).slice(0, 2000);
      }).join('\n');
    }
    if (content === null || content === undefined) return '';
    try {
      return JSON.stringify(content, null, 2);
    } catch {
      return String(content);
    }
  }

  /** Guess a highlight language from a tool name and its arguments. */
  function languageForTool(name, input) {
    if (name === 'Bash') return 'bash';
    const path = input && (input.file_path || input.path || input.notebook_path);
    if (typeof path === 'string') {
      const extension = path.split('.').pop().toLowerCase();
      const map = {
        py: 'python', js: 'javascript', mjs: 'javascript', cjs: 'javascript',
        ts: 'javascript', tsx: 'javascript', jsx: 'javascript',
        json: 'json', html: 'html', htm: 'html', xml: 'html', svg: 'html',
        css: 'css', scss: 'css', sh: 'bash', bash: 'bash', zsh: 'bash',
        sql: 'sql', diff: 'diff', patch: 'diff',
      };
      if (map[extension]) return map[extension];
    }
    return 'json';
  }

  /** Conversation viewer bound to one scroll container. */
  class ConversationViewer {
    /**
     * @param {HTMLElement} host  the scrolling element to fill
     * @param {object} options    `onToast`, `onCopy`, `onOpenExternal`
     */
    constructor(host, options = {}) {
      this.host = host;
      this.options = options;
      this.messages = [];
      this.info = null;
      this.meta = null;
      this.errors = [];
      this.heights = [];
      this.offsets = [];
      this.expanded = new Set();
      this.rendered = new Map();
      this.filter = { thinking: true, tools: true, attachments: false };
      this.visible = [];
      this.measured = new Set();
      this.onScroll = this.onScroll.bind(this);
      this.measureQueued = false;
    }

    /* --------------------------------------------------------- loading */

    /**
     * Replace the contents with a parsed conversation.
     * @param {object} parsed  the `/api/sessions/{id}/messages` payload
     */
    load(parsed) {
      this.messages = parsed.messages || [];
      this.info = parsed.info || {};
      this.meta = parsed.meta || null;
      // Who the assistant is depends on the provider that wrote the session.
      const provider = this.meta && this.options.provider
        ? this.options.provider(this.meta.provider) : null;
      this.assistantLabel = (provider && provider.assistant_label) || 'Assistant';
      this.providerName = (provider && provider.name) || 'the tool';
      this.errors = parsed.errors || [];
      this.expanded = new Set();
      this.rendered.clear();
      this.applyFilter();
      this.host.scrollTop = 0;
      this.mount();
    }

    /** Recompute which entries are shown, and reset the height model. */
    applyFilter() {
      this.visible = this.messages.filter((message) => {
        if (message.kind === 'attachment') return this.filter.attachments;
        if (message.kind === 'tool_result') return this.filter.tools;
        return true;
      });
      this.heights = this.visible.map((m) => ESTIMATED_HEIGHT[m.kind] || 120);
      this.measured = new Set();
      this.recomputeOffsets();
    }

    /** Prefix-sum the row heights so a scroll position maps to an index. */
    recomputeOffsets() {
      this.offsets = new Array(this.heights.length + 1);
      this.offsets[0] = 0;
      for (let i = 0; i < this.heights.length; i += 1) {
        this.offsets[i + 1] = this.offsets[i] + this.heights[i];
      }
    }

    /** Total pixel height of the virtual list. */
    get totalHeight() {
      return this.offsets.length ? this.offsets[this.offsets.length - 1] : 0;
    }

    /* ---------------------------------------------------------- layout */

    /** Build the static chrome and attach the scroll listener. */
    mount() {
      this.host.replaceChildren();
      this.host.append(this.header());

      this.spacer = el('div.v-spacer');
      this.canvas = el('div.v-canvas');
      this.spacer.append(this.canvas);
      this.host.append(this.spacer);

      this.host.removeEventListener('scroll', this.onScroll);
      this.host.addEventListener('scroll', this.onScroll, { passive: true });

      if (!this.resizeObserver && typeof ResizeObserver !== 'undefined') {
        this.resizeObserver = new ResizeObserver(() => this.draw());
        this.resizeObserver.observe(this.host);
      }
      this.draw();
    }

    /**
     * A placeholder for the per-session action buttons.
     * They need an HTTP round trip to know whether the project folder still
     * exists, so they are filled in once that resolves.
     */
    actionSlot() {
      const slot = el('div.v-session-actions');
      const sessionId = this.meta && this.meta.session_id;
      if (sessionId && this.options.actions) {
        Promise.resolve(this.options.actions(sessionId))
          .then((node) => { if (node) slot.replaceChildren(node); })
          .catch(() => {});
      }
      return slot;
    }

    /** The sticky session header with counts and view toggles. */
    header() {
      const meta = this.meta || {};
      const info = this.info || {};
      const counts = el('div.v-counts', {}, [
        el('span', { text: `${meta.message_count ?? this.messages.length} messages` }),
        el('span', { text: `${meta.tool_calls ?? 0} tool calls` }),
        el('span', { text: `${compact(meta.total_tokens || 0)} tokens` }),
        meta.estimated_cost !== undefined
          ? el('span', {
              text: `$${Number(meta.estimated_cost).toFixed(2)} est.`,
              title: 'Local estimate from your pricing table, not billing data',
            })
          : null,
        this.errors.length
          ? el('span.v-bad', { text: `${this.errors.length} unparsable lines` })
          : null,
      ]);

      const toggle = (key, label, title) => el('button.v-toggle', {
        class: this.filter[key] ? 'v-toggle on' : 'v-toggle',
        text: label,
        title,
        onclick: (event) => {
          this.filter[key] = !this.filter[key];
          event.currentTarget.classList.toggle('on', this.filter[key]);
          this.applyFilter();
          this.draw(true);
        },
      });

      return el('div.v-header', {}, [
        el('div.v-title-row', {}, [
          el('h2.v-title', {}, [
            this.options.providerBadge && meta.provider
              ? this.options.providerBadge(meta.provider) : null,
            info.title || meta.title || 'Conversation',
          ]),
          el('div.v-actions', {}, [
            toggle('thinking', 'Thinking', 'Show or hide thinking blocks'),
            toggle('tools', 'Tools', 'Show or hide tool calls and results'),
            toggle('attachments', 'Context', 'Show or hide injected context'),
            el('button.bordered', {
              text: 'Copy as Markdown',
              title: 'Copy the whole conversation to the clipboard',
              onclick: () => this.options.onCopyMarkdown && this.options.onCopyMarkdown(),
            }),
            el('button.bordered', {
              text: 'Export',
              title: 'Save the conversation through a native dialog',
              onclick: (event) => this.options.onExport && this.options.onExport(event),
            }),
          ]),
        ]),
        this.actionSlot(),
        el('div.v-sub', {}, [
          el('span.mono.truncate', { text: info.cwd || meta.project_path || '' }),
          info.git_branch ? el('span.pill', { text: info.git_branch }) : null,
          el('span.faint', { text: (info.versions || []).join(', ') }),
        ]),
        counts,
      ]);
    }

    /* ----------------------------------------------------------- paint */

    /** Scroll handler: repaint only when the visible window changed. */
    onScroll() {
      if (this.scrollFrame) return;
      this.scrollFrame = requestAnimationFrame(() => {
        this.scrollFrame = null;
        this.draw();
      });
    }

    /**
     * Binary-search the first row whose bottom is past `y`.
     * Clamped to the last row, so a scroll position beyond the end of the
     * list never yields an index with no message behind it.
     */
    indexAt(y) {
      const lastRow = this.heights.length - 1;
      if (lastRow < 0) return 0;
      let low = 0;
      let high = lastRow;
      while (low < high) {
        const mid = (low + high) >> 1;
        if (this.offsets[mid + 1] <= y) low = mid + 1;
        else high = mid;
      }
      return low;
    }

    /**
     * Render the rows intersecting the viewport.
     * @param {boolean} force  rebuild even if the window has not moved
     */
    draw(force = false, depth = 0) {
      if (!this.canvas) return;
      const scrollTop = Math.max(0, this.host.scrollTop - (this.headerHeight() || 0));
      const viewport = this.host.clientHeight || 800;

      const first = Math.max(0, this.indexAt(scrollTop) - OVERSCAN);
      const last = Math.min(this.visible.length - 1,
                            this.indexAt(scrollTop + viewport) + OVERSCAN);

      if (!force && this.lastRange
          && this.lastRange[0] === first && this.lastRange[1] === last) {
        return;
      }
      this.lastRange = [first, last];
      this.spacer.style.height = this.totalHeight + 'px';

      if (!this.visible.length) {
        this.canvas.replaceChildren(
          el('div.v-empty', { text: 'Nothing to show with the current filters.' }),
        );
        return;
      }

      const keep = new Set();
      const fragment = document.createDocumentFragment();
      for (let i = first; i <= last; i += 1) {
        keep.add(i);
        let node = this.rendered.get(i);
        if (!node) {
          node = this.row(this.visible[i], i);
          this.rendered.set(i, node);
        }
        node.style.top = this.offsets[i] + 'px';
        fragment.append(node);
      }
      // Drop rows far outside the window so memory stays flat.
      for (const index of Array.from(this.rendered.keys())) {
        if (!keep.has(index) && (index < first - 40 || index > last + 40)) {
          this.rendered.delete(index);
        }
      }
      this.canvas.replaceChildren(fragment);
      // Measure synchronously.  Waiting for an animation frame leaves the
      // first paint laid out with the estimates, which shows as gaps
      // between rows; reading offsetHeight here costs one forced layout
      // for the ~20 rows on screen, which is cheap and always correct.
      if (this.measureNow() && depth < 2) {
        // Heights moved, so a different set of rows may now intersect the
        // viewport.  Re-run once; two passes always settle in practice.
        this.draw(true, depth + 1);
      }
    }

    /**
     * Replace estimated heights with measured ones for the rendered rows.
     * @returns {boolean} whether anything changed
     */
    measureNow() {
      let changed = false;
      for (const [index, node] of this.rendered) {
        if (!node.isConnected) continue;
        const height = node.offsetHeight;
        if (height && Math.abs(height - this.heights[index]) > 1) {
          this.heights[index] = height;
          this.measured.add(index);
          changed = true;
        }
      }
      if (!changed) return false;
      this.refineEstimates();
      this.recomputeOffsets();
      this.spacer.style.height = this.totalHeight + 'px';
      for (const [index, node] of this.rendered) {
        node.style.top = this.offsets[index] + 'px';
      }
      return true;
    }

    /**
     * Use the rows measured so far to improve the guess for the rest.
     *
     * Without this the scrollbar is wrong by however far the per-kind
     * constants miss: collapsed tool rows are roughly 30px against an
     * estimate of 190px for an assistant turn, so a long session would
     * claim several screens of height it does not have.
     */
    refineEstimates() {
      const totals = new Map();
      for (const index of this.measured) {
        const kind = this.visible[index] && this.visible[index].kind;
        if (!kind) continue;
        const entry = totals.get(kind) || { sum: 0, count: 0 };
        entry.sum += this.heights[index];
        entry.count += 1;
        totals.set(kind, entry);
      }
      for (let i = 0; i < this.heights.length; i += 1) {
        if (this.measured.has(i)) continue;
        const entry = totals.get(this.visible[i].kind);
        if (entry && entry.count >= 3) {
          this.heights[i] = entry.sum / entry.count;
        }
      }
    }

    /** Height of the sticky header, which sits above the virtual area. */
    headerHeight() {
      const header = this.host.firstElementChild;
      return header ? header.offsetHeight : 0;
    }

    /**
     * Re-measure on the next frame.
     * Used after a disclosure opens or closes, when the new height is not
     * known until the browser has laid the row out again.
     */
    queueMeasure() {
      if (this.measureQueued) return;
      this.measureQueued = true;
      requestAnimationFrame(() => {
        this.measureQueued = false;
        this.measureNow();
      });
    }

    /* ------------------------------------------------------------ rows */

    /** Build the DOM for one entry. */
    row(message, index) {
      const classes = ['v-row', 'v-' + message.kind];
      if (message.is_error) classes.push('v-error');
      if (message.is_sidechain) classes.push('v-sidechain');
      const node = el('div', { class: classes.join(' '), dataset: { index: String(index) } });

      node.append(this.gutter(message));

      const body = el('div.v-body');
      if (message.kind === 'attachment') {
        body.append(this.attachmentBlock(message));
      } else if (message.kind === 'system') {
        body.append(this.systemBlock(message));
      } else {
        for (const block of message.blocks || []) {
          const rendered = this.block(block, message, index);
          if (rendered) body.append(rendered);
        }
        if (!(message.blocks || []).length) {
          body.append(el('div.v-muted', { text: '(no content)' }));
        }
      }
      node.append(body);
      return node;
    }

    /** The left-hand role and timestamp column. */
    gutter(message) {
      const label = {
        user: 'You', assistant: this.assistantLabel || 'Assistant', tool_result: 'Result',
        system: 'System', attachment: 'Context',
      }[message.kind] || message.kind;

      const bits = [el('div.v-role', { text: label })];
      if (message.timestamp) {
        bits.push(el('div.v-time', {
          text: clockTime(message.timestamp),
          title: fullTime(message.timestamp),
        }));
      }
      if (message.is_sidechain) bits.push(el('div.v-tag', { text: 'sub-agent' }));

      const info = el('button.v-info', {
        text: 'i',
        title: 'Show message metadata',
        'aria-label': 'Show message metadata',
        onclick: (event) => {
          event.stopPropagation();
          this.options.onInspect && this.options.onInspect(message);
        },
      });
      bits.push(info);
      return el('div.v-gutter', {}, bits);
    }

    /** Render one content block. */
    block(block, message, index) {
      switch (block.type) {
        case 'text':
          return this.textBlock(block.text, message);
        case 'thinking':
          return this.filter.thinking ? this.thinkingBlock(block, index) : null;
        case 'tool_use':
          return this.filter.tools ? this.toolUseBlock(block, index) : null;
        case 'tool_result':
          return this.filter.tools ? this.toolResultBlock(block, index) : null;
        case 'image':
          return el('div.v-muted', {
            text: `[image · ${block.media_type || 'image'} · ${(block.bytes || 0).toLocaleString()} bytes]`,
          });
        default:
          return el('div.v-muted', { text: `[${block.type || 'unknown'} block]` });
      }
    }

    /** Prose, rendered as Markdown for the assistant and plain text for the user. */
    textBlock(text, message) {
      const value = String(text || '');
      if (!value.trim()) return null;
      if (message.role === 'assistant') {
        const node = el('div.v-text.v-markdown');
        node.innerHTML = window.md.render(value);
        return node;
      }
      return el('div.v-text.v-plain', { text: value });
    }

    /**
     * Thinking, collapsed by default.
     *
     * 98% of thinking blocks on the reference install carry a signature but
     * no text (SCHEMA.md 3.4.1).  Those get a quiet, non-expandable marker
     * instead of an empty disclosure nobody can open.
     */
    thinkingBlock(block, index) {
      const text = String(block.text || '');
      if (!text.trim()) {
        return el('div.v-thinking-empty', {
          text: 'Thinking \u00b7 not recorded in the transcript',
          title: 'The tool stored a marker for this reasoning block but not its text',
        });
      }
      const words = text.trim().split(/\s+/).length;
      return this.disclosure({
        key: `t${index}:${text.length}`,
        className: 'v-thinking',
        label: 'Thinking',
        hint: `${words} ${words === 1 ? 'word' : 'words'}`,
        build: () => {
          const node = el('div.v-markdown');
          node.innerHTML = window.md.render(text);
          return node;
        },
      });
    }

    /** A tool call, collapsed by default, showing name and gist. */
    toolUseBlock(block, index) {
      const language = languageForTool(block.name, block.input);
      const pretty = typeof block.input === 'string'
        ? block.input
        : JSON.stringify(block.input, null, 2);
      return this.disclosure({
        key: `u${index}:${block.id || ''}`,
        className: 'v-tooluse',
        label: block.name || 'tool',
        hint: toolSummary(block.input),
        badge: block.caller && block.caller !== 'direct' ? block.caller : null,
        build: () => {
          // Bash commands read better as shell than as a JSON blob.
          if (block.name === 'Bash' && block.input && typeof block.input.command === 'string') {
            const wrapper = el('div');
            wrapper.innerHTML = window.md.render('```bash\n' + block.input.command + '\n```');
            if (block.input.description) {
              wrapper.prepend(el('div.v-muted', { text: block.input.description }));
            }
            return wrapper;
          }
          const wrapper = el('div');
          wrapper.innerHTML = window.md.render('```' + language + '\n' + (pretty || '') + '\n```');
          return wrapper;
        },
      });
    }

    /** A tool result, collapsed by default, with a truncated preview. */
    toolResultBlock(block, index) {
      const text = resultText(block.content);
      const lines = text ? text.split('\n').length : 0;
      const hint = text
        ? `${lines} line${lines === 1 ? '' : 's'} · ${text.length.toLocaleString()} chars`
        : 'empty';
      return this.disclosure({
        key: `r${index}:${block.tool_use_id || ''}`,
        className: block.is_error ? 'v-toolresult v-bad-block' : 'v-toolresult',
        label: block.is_error ? `${block.name || 'tool'} failed` : `${block.name || 'tool'} result`,
        hint,
        preview: text ? text.split('\n')[0].slice(0, 120) : '',
        build: () => {
          const wrapper = el('div');
          wrapper.innerHTML = window.md.render('```\n' + text + '\n```');
          return wrapper;
        },
      });
    }

    /** Injected context, always collapsed. */
    attachmentBlock(message) {
      const attachment = (message.extra && message.extra.attachment) || {};
      return this.disclosure({
        key: `a${message.index}`,
        className: 'v-attachment',
        label: message.subtype || 'context',
        hint: `injected by ${this.providerName || 'the tool'}`,
        build: () => {
          const wrapper = el('div');
          const rendered = message.extra && message.extra.rendered;
          const body = typeof rendered === 'string'
            ? rendered
            : JSON.stringify(attachment, null, 2);
          wrapper.innerHTML = window.md.render('```json\n' + body.slice(0, 20000) + '\n```');
          return wrapper;
        },
      });
    }

    /** System notices render inline; they are short and matter. */
    systemBlock(message) {
      const text = (message.blocks || [])
        .filter((b) => b.type === 'text').map((b) => b.text).join('\n');
      const subtype = message.subtype || 'system';
      const extra = message.extra || {};
      const bits = [el('span.v-sys-kind', { text: subtype })];
      if (extra.durationMs) {
        bits.push(el('span.faint', { text: `${Math.round(extra.durationMs / 1000)}s` }));
      }
      const node = el('div.v-system-note', {}, [
        el('div.v-sys-head', {}, bits),
        text ? el('div.v-text.v-plain', { text }) : null,
      ]);
      return node;
    }

    /**
     * A collapsed section that builds its content only when first opened.
     * @param {{key:string, className:string, label:string, hint?:string,
     *          preview?:string, badge?:string, build:()=>HTMLElement}} spec
     */
    disclosure(spec) {
      const open = this.expanded.has(spec.key);
      const wrapper = el('div', { class: `v-disclosure ${spec.className}` });

      const chevron = el('span.v-chevron', { text: open ? '▾' : '▸' });
      const head = el('button.v-disc-head', {
        type: 'button',
        'aria-expanded': open ? 'true' : 'false',
      }, [
        chevron,
        el('span.v-disc-label', { text: spec.label }),
        spec.badge ? el('span.pill', { text: spec.badge }) : null,
        spec.hint ? el('span.v-disc-hint.truncate', { text: spec.hint }) : null,
      ]);

      const body = el('div.v-disc-body');
      if (open) body.append(spec.build());
      else if (spec.preview) {
        head.append(el('span.v-disc-preview.truncate', { text: spec.preview }));
      }

      head.addEventListener('click', () => {
        const nowOpen = !this.expanded.has(spec.key);
        if (nowOpen) {
          this.expanded.add(spec.key);
          body.replaceChildren(spec.build());
        } else {
          this.expanded.delete(spec.key);
          body.replaceChildren();
        }
        chevron.textContent = nowOpen ? '▾' : '▸';
        head.setAttribute('aria-expanded', nowOpen ? 'true' : 'false');
        const preview = head.querySelector('.v-disc-preview');
        if (preview) preview.remove();
        if (!nowOpen && spec.preview) {
          head.append(el('span.v-disc-preview.truncate', { text: spec.preview }));
        }
        // The row just changed height; re-measure and reflow.
        this.queueMeasure();
      });

      wrapper.append(head, body);
      return wrapper;
    }

    /* ------------------------------------------------------- navigation */

    /**
     * Scroll a message into view, by transcript line or by uuid.
     * Used by the search results' jump-to-message action.
     */
    jumpTo({ line, uuid }) {
      let target = -1;
      for (let i = 0; i < this.visible.length; i += 1) {
        const message = this.visible[i];
        if (uuid && message.uuid === uuid) { target = i; break; }
        if (line && message.line === line) { target = i; break; }
        if (line && message.line > line && target === -1) { target = Math.max(0, i - 1); }
      }
      if (target === -1) return false;
      this.scrollToIndex(target);
      this.flash(target);
      return true;
    }

    /**
     * Scroll so row `index` sits near the top of the viewport.
     *
     * The offset of a distant row is only an estimate until the rows around
     * it have been rendered once, so a single scroll usually lands in the
     * wrong place.  Each pass renders and measures wherever it landed,
     * which corrects the offsets nearby; repeating converges in two or
     * three passes.  The loop is bounded so a pathological layout cannot
     * hang the window.
     */
    scrollToIndex(index) {
      if (index < 0 || index >= this.heights.length) return;
      const headerOffset = this.headerHeight() - 12;
      let previous = -1;
      for (let attempt = 0; attempt < 8; attempt += 1) {
        const target = Math.max(0, this.offsets[index] + headerOffset);
        if (Math.abs(target - previous) <= 1) break;
        previous = target;
        this.host.scrollTop = target;
        this.draw(true);
      }
    }

    /** Briefly outline a row so the eye finds it after a jump. */
    flash(index) {
      requestAnimationFrame(() => {
        const node = this.rendered.get(index);
        if (!node) return;
        node.classList.add('v-flash');
        setTimeout(() => node.classList.remove('v-flash'), 1600);
      });
    }

    /** Expand every collapsible section currently in the list. */
    expandAll() {
      for (let i = 0; i < this.visible.length; i += 1) {
        const message = this.visible[i];
        for (const block of message.blocks || []) {
          if (block.type === 'thinking') this.expanded.add(`t${i}:${(block.text || '').length}`);
          if (block.type === 'tool_use') this.expanded.add(`u${i}:${block.id || ''}`);
          if (block.type === 'tool_result') this.expanded.add(`r${i}:${block.tool_use_id || ''}`);
        }
      }
      this.rendered.clear();
      this.draw(true);
    }

    /** Collapse everything again. */
    collapseAll() {
      this.expanded.clear();
      this.rendered.clear();
      this.draw(true);
    }

    /** Release listeners when the viewer is torn down. */
    destroy() {
      this.host.removeEventListener('scroll', this.onScroll);
      if (this.resizeObserver) this.resizeObserver.disconnect();
      this.resizeObserver = null;
      this.rendered.clear();
    }
  }

  window.ConversationViewer = ConversationViewer;
  window.viewerHelpers = { toolSummary, resultText, languageForTool, compact };
}());
