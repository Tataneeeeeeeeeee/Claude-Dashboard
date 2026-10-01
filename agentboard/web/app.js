/* Agentboard - UI shell.
 *
 * Plain ES modules-free JavaScript: no build step, no bundler, no CDN.  The
 * global `window.dashboard` object is also the surface the native menu bar
 * calls into from Python via `evaluate_js`.
 *
 * Stage 3 covers the shell: navigation, theming, the session list, search,
 * keyboard shortcuts, toasts and the indexing progress bar.  The full
 * conversation viewer, deletion and charts land in later stages.
 */
'use strict';

/* ----------------------------------------------------------------- utils */

/** Query one element. */
const $ = (selector, root = document) => root.querySelector(selector);

/** Query all elements as a real array. */
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

/**
 * Build an element.
 * @param {string} tag  tag name, optionally with `.class` suffixes
 * @param {object} [attrs]  properties and attributes
 * @param {Array|string} [children]
 */
function el(tag, attrs = {}, children = []) {
  const [name, ...classes] = tag.split('.');
  const node = document.createElement(name);
  if (classes.length) node.className = classes.join(' ');
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className += (node.className ? ' ' : '') + value;
    else if (key === 'text') node.textContent = value;
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

/** Debounce `fn` by `wait` milliseconds. */
function debounce(fn, wait) {
  let timer = null;
  return (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), wait);
  };
}

/** Compact number: 1234567 -> "1.2M". */
function compact(value) {
  const n = Number(value) || 0;
  if (Math.abs(n) < 1000) return String(Math.round(n));
  if (Math.abs(n) < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + 'k';
  if (Math.abs(n) < 1e9) return (n / 1e6).toFixed(n < 1e7 ? 1 : 0) + 'M';
  return (n / 1e9).toFixed(1) + 'B';
}

/** Bytes as a human string. */
function bytes(value) {
  const n = Number(value) || 0;
  if (n < 1024) return n + ' B';
  if (n < 1024 ** 2) return (n / 1024).toFixed(0) + ' KB';
  if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
  return (n / 1024 ** 3).toFixed(2) + ' GB';
}

/** Seconds as "2h 14m" / "45s". */
function duration(seconds) {
  const s = Math.max(0, Math.round(Number(seconds) || 0));
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s / 60) + 'm';
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  return minutes ? `${hours}h ${minutes}m` : `${hours}h`;
}

/** ISO timestamp as a short local string, with "today"/"yesterday". */
function when(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  const today = new Date();
  const sameDay = (a, b) => a.toDateString() === b.toDateString();
  const yesterday = new Date(today.getTime() - 86400000);
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay(date, today)) return 'today ' + time;
  if (sameDay(date, yesterday)) return 'yesterday ' + time;
  if (date.getFullYear() === today.getFullYear()) {
    return date.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + time;
  }
  return date.toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' });
}

/** US dollars, with more precision for very small amounts. */
function money(value) {
  const n = Number(value) || 0;
  if (n === 0) return '$0';
  if (n < 0.01) return '<$0.01';
  if (n < 10) return '$' + n.toFixed(2);
  return '$' + n.toFixed(n < 1000 ? 1 : 0);
}

/** Escape text for safe insertion into HTML. */
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * A provider's badge: its monogram on its own accent colour.
 * The name is always given as text to assistive technology and as a
 * tooltip, so identity never rests on colour alone.
 * @param {{id:string,name:string,monogram:string,color:string}|null} provider
 * @param {'sm'|'md'} [size]
 */
function providerBadge(provider, size = 'sm') {
  const info = provider || { id: 'unknown', name: 'Unknown provider', monogram: '?', color: '#8a8f98' };
  return el(`span.provider-badge.${size}`, {
    style: `--p-color:${info.color}`,
    title: info.name,
    role: 'img',
    'aria-label': info.name,
    text: info.monogram || info.name.slice(0, 1),
  });
}

/* ------------------------------------------------------------------ api */

/** Thin wrapper over the local HTTP API with consistent error reporting. */
const api = {
  /**
   * GET a JSON endpoint.
   * @throws {Error} carrying the server's `detail` message when present.
   */
  async get(path, params) {
    const url = new URL(path, window.location.origin);
    for (const [key, value] of Object.entries(params || {})) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, value);
      }
    }
    const response = await fetch(url, { headers: { Accept: 'application/json' } });
    const isJson = (response.headers.get('content-type') || '').includes('json');
    const body = isJson ? await response.json() : await response.text();
    if (!response.ok) {
      throw new Error((body && body.detail) || `${response.status} ${response.statusText}`);
    }
    return body;
  },

  /** Send a JSON body with an arbitrary method. */
  async send(method, path, body) {
    const response = await fetch(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const isJson = (response.headers.get('content-type') || '').includes('json');
    const payload = isJson ? await response.json() : await response.text();
    if (!response.ok) {
      throw new Error((payload && payload.detail) || `${response.status} ${response.statusText}`);
    }
    return payload;
  },
};

/* ---------------------------------------------------------------- toasts */

/**
 * Show a transient message.
 * @param {string} title
 * @param {string} [detail]
 * @param {'info'|'ok'|'warn'|'error'} [kind]
 */
function toast(title, detail = '', kind = 'info') {
  const host = $('#toasts');
  const node = el('div.toast' + (kind !== 'info' ? '.' + kind : ''), {}, [
    el('div.body', {}, [
      el('div.title', { text: title }),
      detail ? el('div.detail', { text: detail }) : null,
    ]),
    el('button', { text: '✕', 'aria-label': 'Dismiss', onclick: () => node.remove() }),
  ]);
  host.append(node);
  const life = kind === 'error' ? 9000 : 4200;
  setTimeout(() => {
    node.style.transition = 'opacity .2s';
    node.style.opacity = '0';
    setTimeout(() => node.remove(), 220);
  }, life);
  return node;
}

/* ----------------------------------------------------------------- modal */

/** Open the shared modal with a title and body node. */
function openModal(title, bodyNode) {
  $('#modal-title').textContent = title;
  const body = $('#modal-body');
  body.replaceChildren(bodyNode);
  $('#modal-backdrop').classList.add('visible');
}

/** Close the shared modal. */
function closeModal() {
  $('#modal-backdrop').classList.remove('visible');
}

/* ------------------------------------------------------------------ app */

/** Application state and behaviour, exposed as `window.dashboard`. */
const dashboard = {
  state: {
    view: 'sessions',
    sessions: [],
    selectedId: null,
    cursor: -1,
    bootstrap: null,
    conversation: null,
    loadedId: null,
    searchMode: false,
    theme: 'system',
    native: false,
    selection: new Set(),
    onlyFavorites: false,
    provider: 'all',
  },

  /* ---------------------------------------------------------- lifecycle */

  /** Boot: load config and data, wire events, render. */
  async init() {
    this.wireEvents();
    const initialTheme = this.readStoredTheme();
    this.themePinned = new URLSearchParams(window.location.search).has('theme');
    this.applyTheme(initialTheme);
    this.state.provider = this.readStoredProvider();
    // Transitions stay off until the real theme is painted, so a light
    // theme does not fade in from the dark default.
    requestAnimationFrame(() => requestAnimationFrame(
      () => document.documentElement.classList.remove('booting'),
    ));
    this.setStatus('busy', 'Loading');

    // The server inlines the first screen of data into the page, so render
    // it immediately and only fall back to fetching when it is absent.
    const preload = window.__PRELOAD__;
    if (preload && preload.bootstrap) {
      try {
        this.state.bootstrap = preload.bootstrap;
        if (!this.themePinned) this.applyTheme(preload.bootstrap.config.theme || 'system');
        this.renderProviderSwitch();
        this.fillFilters(preload.bootstrap);
        this.renderCorpus(preload.bootstrap.stats);
        if (preload.bootstrap.stats.session_count && preload.sessions) {
          if (this.state.provider === 'all') {
            this.state.sessions = preload.sessions.sessions;
            this.renderSessions(preload.sessions);
          } else {
            this.loadSessions();
          }
          this.setStatus('ok', 'Ready');
          const view = new URLSearchParams(window.location.search).get('view')
            || preload.bootstrap.config.last_view;
          if (view && view !== 'sessions') this.setView(view);
          if (preload.conversation) {
            this.mountConversation(
              preload.conversation,
              this.deepLinkJump(new URLSearchParams(window.location.search)),
            );
          } else {
            this.openDeepLink();
          }
          this.detectNative();
          this.pollIndexProgress();
          return;
        }
      } catch (error) {
        // Fall through to the network path below.
        this.state.bootstrap = null;
      }
    }

    try {
      const boot = await api.get('/api/bootstrap');
      this.state.bootstrap = boot;
      // The stored config only wins when the user has not pinned a theme
      // locally, either via ?theme= or a previous toggle.
      if (!this.themePinned) this.applyTheme(boot.config.theme || 'system');
      this.renderProviderSwitch();
      this.fillFilters(boot);
      this.renderCorpus(boot.stats);

      if (!boot.stats.session_count) {
        this.renderEmptyState(boot);
        this.setStatus('ok', 'No history yet');
        this.watchForChanges();
        return;
      }
      await this.loadSessions();
      this.setStatus('ok', 'Ready');
      this.openDeepLink();
    } catch (error) {
      this.setStatus('error', 'Failed to load');
      toast('Could not reach the local backend', error.message, 'error');
      return;
    }

    this.detectNative();
    this.pollIndexProgress();
    this.watchForChanges();
  },

  /**
   * Poll the revision counter the filesystem watcher bumps.
   *
   * One tiny request every few seconds is cheaper than re-fetching the
   * index, and it keeps the list current while a session is running.
   */
  async watchForChanges() {
    let known = null;
    const tick = async () => {
      try {
        const status = await api.get('/api/changes');
        if (!status.watching) return;          // watchdog unavailable
        if (known !== null && status.revision !== known) {
          await this.reload();
          if (this.usageReady && window.usageView) window.usageView.refresh();
          this.setStatus('ok', 'Updated');
          setTimeout(() => this.setStatus('ok', 'Ready'), 1500);
        }
        known = status.revision;
      } catch {
        return;                                 // the server has gone away
      }
      const config = (this.state.bootstrap && this.state.bootstrap.config) || {};
      const seconds = Number(config.refresh_interval_seconds) || 5;
      setTimeout(tick, Math.max(1, seconds) * 1000);
    };
    tick();
  },

  /** Note whether the pywebview bridge is present, for the status bar. */
  async detectNative() {
    const label = $('#status-native');
    if (window.pywebview && window.pywebview.api) {
      this.state.native = true;
      try {
        const info = await window.pywebview.api.get_info();
        label.textContent = `native window · python ${info.python}`;
      } catch {
        label.textContent = 'native window';
      }
    } else {
      label.textContent = 'browser mode';
    }
  },

  /* ------------------------------------------------------------- events */

  /** Attach every DOM listener once. */
  wireEvents() {
    $$('#nav button').forEach((button) => {
      button.addEventListener('click', () => this.setView(button.dataset.view));
    });

    $('#btn-refresh').addEventListener('click', () => this.rebuild());
    $('#btn-trash').addEventListener('click', () => this.showTrash());
    $('#btn-favorites').addEventListener('click', (event) => {
      this.state.onlyFavorites = !this.state.onlyFavorites;
      event.currentTarget.classList.toggle('active', this.state.onlyFavorites);
      this.loadSessions();
    });
    $('#btn-maintenance').addEventListener('click', () => this.showMaintenance());
    $('#btn-select-all').addEventListener('click', () => this.selectAll(true));
    $('#btn-select-none').addEventListener('click', () => this.selectAll(false));
    $('#btn-delete-selected').addEventListener('click',
      () => this.deleteSessions([...this.state.selection]));
    $('#btn-theme').addEventListener('click', () => this.cycleTheme());
    $('#modal-close').addEventListener('click', closeModal);
    $('#modal-backdrop').addEventListener('click', (event) => {
      if (event.target === $('#modal-backdrop')) closeModal();
    });

    const search = $('#search');
    search.addEventListener('input', debounce(() => this.runSearch(search.value), 220));
    search.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        search.value = '';
        search.blur();
        this.runSearch('');
      }
    });

    ['#filter-project', '#filter-model', '#filter-tool', '#sort'].forEach((selector) => {
      $(selector).addEventListener('change', () => this.loadSessions());
    });

    document.addEventListener('keydown', (event) => this.onKeydown(event));

    // Code-block copy buttons and external links are created dynamically,
    // so they are handled by one delegated listener.
    document.addEventListener('click', (event) => {
      const copyButton = event.target.closest('[data-copy]');
      if (copyButton) {
        const block = copyButton.closest('.code-block');
        const code = block && block.querySelector('code');
        if (code) {
          this.copyText(code.textContent).then(() => {
            const original = copyButton.textContent;
            copyButton.textContent = 'Copied';
            setTimeout(() => { copyButton.textContent = original; }, 1200);
          }).catch((error) => toast('Copy failed', error.message, 'error'));
        }
        return;
      }
      const link = event.target.closest('a[data-external]');
      if (link) {
        event.preventDefault();
        this.openExternal(link.getAttribute('href'));
      }
    });

    // Follow the OS theme while the user has not chosen one explicitly.
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    media.addEventListener('change', () => {
      if (this.state.theme === 'system') this.applyTheme('system');
    });
  },

  /**
   * Global keyboard shortcuts.
   * Ignored while typing in a field, except for Escape.
   */
  onKeydown(event) {
    const target = event.target;
    const typing = target && (
      target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable
    );

    if (event.key === 'Escape') {
      if ($('#modal-backdrop').classList.contains('visible')) closeModal();
      return;
    }
    if (typing || event.ctrlKey || event.metaKey || event.altKey) {
      if (event.key === 'F5') { event.preventDefault(); this.rebuild(); }
      return;
    }

    switch (event.key) {
      case '/':
        event.preventDefault();
        this.focusSearch();
        break;
      case 'j':
        event.preventDefault();
        this.moveCursor(1);
        break;
      case 'k':
        event.preventDefault();
        this.moveCursor(-1);
        break;
      case 'Enter':
        if (this.state.cursor >= 0) {
          event.preventDefault();
          this.openSession(this.state.sessions[this.state.cursor]);
        }
        break;
      case '?':
        event.preventDefault();
        this.showShortcuts();
        break;
      case 'r':
        event.preventDefault();
        this.resumeSession();
        break;
      case 'Delete':
      case 'Backspace': {
        event.preventDefault();
        const ids = this.state.selection.size
          ? [...this.state.selection]
          : [this.currentSessionId()].filter(Boolean);
        this.deleteSessions(ids);
        break;
      }
      case 'f': {
        event.preventDefault();
        const session = this.state.sessions[this.state.cursor];
        if (session) this.toggleFavorite(session, null), this.loadSessions();
        break;
      }
      case 't': {
        event.preventDefault();
        const id = this.currentSessionId();
        if (id) this.editAnnotations(id);
        break;
      }
      case 'b':
        event.preventDefault();
        if (window.configView) { this.setView('config'); window.configView.runBackup(); }
        break;
      case 'x': {
        event.preventDefault();
        const id = this.currentSessionId();
        if (id) this.toggleSelection(id);
        break;
      }
      case 'e':
        if (this.viewer) { event.preventDefault(); this.viewer.expandAll(); }
        break;
      case 'c':
        if (this.viewer) { event.preventDefault(); this.viewer.collapseAll(); }
        break;
      case 'p':
        event.preventDefault();
        this.cycleProvider(1);
        break;
      case 'P':
        event.preventDefault();
        this.cycleProvider(-1);
        break;
      case '1': this.setView('sessions'); break;
      case '2': this.setView('usage'); break;
      case '3': this.setView('config'); break;
      case 'F5': event.preventDefault(); this.rebuild(); break;
      default:
        break;
    }
  },

  /* ----------------------------------------------------------- providers */

  /** The descriptor of one provider, from the bootstrap payload. */
  provider(id) {
    const list = (this.state.bootstrap && this.state.bootstrap.providers) || [];
    return list.find((p) => p.id === id) || null;
  },

  /** Providers offered by the switcher: every enabled one, detected first. */
  switchableProviders() {
    const list = ((this.state.bootstrap && this.state.bootstrap.providers) || [])
      .filter((p) => p.enabled);
    const rank = (p) => (p.stats.sessions ? 0 : p.detection.detected ? 1 : 2);
    return list.slice().sort((a, b) => rank(a) - rank(b));
  },

  /** The provider filter value for API calls: empty means every provider. */
  providerParam() {
    return this.state.provider === 'all' ? '' : this.state.provider;
  },

  /** The last provider chosen, if it still exists. */
  readStoredProvider() {
    const forced = new URLSearchParams(window.location.search).get('provider');
    if (forced) return forced;
    try {
      return localStorage.getItem('provider') || 'all';
    } catch {
      return 'all';
    }
  },

  /**
   * Draw the agent picker: one button showing the current scope
   * ("Agent: All agents" or the selected agent's badge and name) that
   * opens a menu listing every enabled agent with its session count and
   * detection state, plus a link to manage them.
   *
   * It follows the ARIA menu-button pattern: Enter, Space or the arrow
   * keys open it; arrows, Home and End move; Enter selects; Escape or Tab
   * closes and gives focus back to the button.
   */
  renderProviderSwitch() {
    const host = $('#provider-switch');
    const providers = this.switchableProviders();
    if (this.state.provider !== 'all' && !providers.some((p) => p.id === this.state.provider)) {
      this.state.provider = 'all';
    }
    host.hidden = providers.length < 1;
    const total = providers.reduce((sum, p) => sum + (p.stats.sessions || 0), 0);
    const current = this.provider(this.state.provider);

    const trigger = el('button.agent-picker-button', {
      id: 'agent-picker-button',
      type: 'button',
      'aria-haspopup': 'menu',
      'aria-expanded': 'false',
      'aria-controls': 'agent-picker-menu',
      title: 'Choose which agent every view shows (p)',
      class: current ? 'scoped' : '',
      style: current ? `--p-color:${current.color}` : null,
      onclick: () => this.toggleAgentMenu(),
      onkeydown: (event) => {
        if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(event.key)) {
          event.preventDefault();
          event.stopPropagation();
          this.toggleAgentMenu(true, event.key === 'ArrowUp' ? 'last' : 'current');
        }
      },
    }, [
      el('span.agent-picker-label', { text: 'Agent' }),
      current ? providerBadge(current) : el('span.agent-all-icon', { 'aria-hidden': 'true' }),
      el('span.agent-picker-name', { text: current ? current.name : 'All agents' }),
      el('span.agent-picker-count.num', {
        text: String(current ? current.stats.sessions || 0 : total),
        'aria-label': 'sessions',
      }),
      el('span.agent-picker-caret', { 'aria-hidden': 'true' }),
    ]);

    const item = (id, label, count, provider) => {
      const on = id === this.state.provider;
      const missing = provider && !provider.detection.detected;
      return el('button.agent-option', {
        type: 'button',
        role: 'menuitemradio',
        'aria-checked': on ? 'true' : 'false',
        tabindex: '-1',
        class: missing ? 'missing' : '',
        dataset: { provider: id },
        title: missing ? provider.detection.reason : '',
        onclick: () => { this.closeAgentMenu(true); this.setProvider(id); },
      }, [
        el('span.agent-check', { 'aria-hidden': 'true', text: on ? '✓' : '' }),
        provider ? providerBadge(provider) : el('span.agent-all-icon', { 'aria-hidden': 'true' }),
        el('span.agent-option-name', { text: label }),
        missing
          ? el('span.agent-option-state', { text: 'not detected' })
          : el('span.agent-option-count.num', { text: String(count) }),
      ]);
    };

    const menu = el('div.agent-menu', {
      id: 'agent-picker-menu',
      role: 'menu',
      'aria-labelledby': 'agent-picker-button',
      hidden: 'hidden',
      onkeydown: (event) => this.onAgentMenuKey(event),
    }, [
      item('all', 'All agents', total, null),
      el('div.agent-menu-sep', { role: 'separator' }),
      ...providers.map((p) => item(p.id, p.name, p.stats.sessions || 0, p)),
      el('div.agent-menu-sep', { role: 'separator' }),
      el('button.agent-option.agent-manage', {
        type: 'button',
        role: 'menuitem',
        tabindex: '-1',
        onclick: () => {
          this.closeAgentMenu(false);
          this.setView('config');
          if (window.configView && window.configView.openTab) window.configView.openTab('providers');
        },
      }, [
        el('span.agent-check', { 'aria-hidden': 'true' }),
        el('span.agent-manage-icon', { 'aria-hidden': 'true', text: '⚙' }),
        el('span.agent-option-name', { text: 'Manage agents…' }),
      ]),
    ]);
    host.replaceChildren(trigger, menu);
  },

  /** Open or close the agent menu; `focus` picks the item to focus. */
  toggleAgentMenu(open, focus = 'current') {
    const menu = $('#agent-picker-menu');
    if (!menu) return;
    const willOpen = open === undefined ? menu.hidden : open;
    if (!willOpen) { this.closeAgentMenu(true); return; }
    menu.hidden = false;
    $('#agent-picker-button').setAttribute('aria-expanded', 'true');
    const items = $$('[role^="menuitem"]', menu);
    const target = focus === 'last' ? items[items.length - 1]
      : items.find((node) => node.getAttribute('aria-checked') === 'true') || items[0];
    if (target) target.focus();
    // Any click outside closes it.
    this.agentMenuOutside = (event) => {
      if (!event.target.closest('#provider-switch')) this.closeAgentMenu(false);
    };
    setTimeout(() => document.addEventListener('mousedown', this.agentMenuOutside), 0);
  },

  /** Close the agent menu, optionally returning focus to its button. */
  closeAgentMenu(refocus) {
    const menu = $('#agent-picker-menu');
    if (menu) menu.hidden = true;
    const button = $('#agent-picker-button');
    if (button) {
      button.setAttribute('aria-expanded', 'false');
      if (refocus) button.focus();
    }
    if (this.agentMenuOutside) {
      document.removeEventListener('mousedown', this.agentMenuOutside);
      this.agentMenuOutside = null;
    }
  },

  /** Keyboard handling inside the open menu. */
  onAgentMenuKey(event) {
    const items = $$('#agent-picker-menu [role^="menuitem"]');
    const index = items.indexOf(document.activeElement);
    const move = (to) => { event.preventDefault(); items[(to + items.length) % items.length].focus(); };
    event.stopPropagation();
    switch (event.key) {
      case 'ArrowDown': move(index + 1); break;
      case 'ArrowUp': move(index - 1); break;
      case 'Home': move(0); break;
      case 'End': move(items.length - 1); break;
      case 'Escape': event.preventDefault(); this.closeAgentMenu(true); break;
      case 'Tab': this.closeAgentMenu(false); break;
      default:
        // Type-ahead: jump to the first agent starting with that letter.
        if (event.key.length === 1 && /\S/.test(event.key)) {
          const letter = event.key.toLowerCase();
          const hit = items.findIndex((node, i) => i > index
            && node.textContent.trim().toLowerCase().startsWith(letter));
          const wrap = hit >= 0 ? hit
            : items.findIndex((node) => node.textContent.trim().toLowerCase().startsWith(letter));
          if (wrap >= 0) move(wrap);
        }
    }
  },

  /** Move the selection to the next or previous agent. */
  cycleProvider(step) {
    const ids = ['all'].concat(this.switchableProviders().map((p) => p.id));
    const index = ids.indexOf(this.state.provider);
    this.setProvider(ids[(index + step + ids.length) % ids.length]);
  },

  /**
   * Scope every view to one provider (or "all"): the session list, its
   * filters, search and the usage dashboard follow.
   */
  async setProvider(id) {
    if (id === this.state.provider) return;
    this.state.provider = id;
    try { localStorage.setItem('provider', id); } catch { /* per-run only */ }
    this.renderProviderSwitch();
    this.state.selection.clear();
    await this.refreshFilters();
    const term = $('#search').value;
    if (this.state.searchMode && term) await this.runSearch(term);
    else await this.loadSessions();
    this.showProviderState();
    if (this.usageReady && window.usageView) window.usageView.setProvider(this.providerParam());
  },

  /** Reload the dropdown values for the current provider. */
  async refreshFilters() {
    try {
      const values = await api.get('/api/distinct', { provider: this.providerParam() });
      this.fillFilters({ ...this.state.bootstrap, distinct: values, projects: values.projects });
    } catch {
      /* the previous values stay usable */
    }
  },

  /**
   * When one provider is selected and it has nothing to show, say why:
   * not detected (with where it looked) or simply no sessions yet.
   */
  showProviderState() {
    const provider = this.provider(this.state.provider);
    if (!provider || provider.stats.sessions || this.state.loadedId) return;
    const found = provider.detection;
    $('#session-content').replaceChildren(el('div.placeholder.provider-state', {}, [
      providerBadge(provider, 'lg'),
      el('h2', { text: found.detected ? `No ${provider.name} sessions yet` : `${provider.name} not detected` }),
      el('p', { text: found.reason }),
      el('p', {}, ['Sessions are read from ', el('code', { text: found.root }), '.']),
      el('div.provider-state-actions', {}, [
        el('button.bordered', {
          text: 'Change the path in Settings',
          onclick: () => {
            this.setView('config');
            if (window.configView && window.configView.openTab) window.configView.openTab('providers');
          },
        }),
        el('button.bordered', { text: 'Detect again', onclick: () => this.rescanProviders() }),
      ]),
    ]));
  },

  /** Ask the backend to detect tools again, then reload everything. */
  async rescanProviders() {
    this.setStatus('busy', 'Detecting tools');
    try {
      await api.send('POST', '/api/providers/rescan');
      await this.reload();
      this.showProviderState();
      toast('Providers detected again', '', 'ok');
    } catch (error) {
      toast('Could not rescan', error.message, 'error');
    } finally {
      this.setStatus('ok', 'Ready');
    }
  },

  /* --------------------------------------------------------------- view */

  /** Switch the main view. Also called from the native View menu. */
  setView(name) {
    if (!['sessions', 'usage', 'config'].includes(name)) return;
    this.state.view = name;
    $$('.view').forEach((section) => {
      section.classList.toggle('active', section.id === 'view-' + name);
    });
    $$('#nav button').forEach((button) => {
      button.classList.toggle('active', button.dataset.view === name);
    });
    if (name === 'usage') this.showUsage();
    if (name === 'config') this.showConfig();
    // Reopen on the view the window was last showing.
    if (this.state.bootstrap) {
      api.send('PATCH', '/api/config', { last_view: name }).catch(() => {});
    }
  },

  /** Render the config view, loading it the first time it is opened. */
  showConfig() {
    if (window.configView) window.configView.render($('#config-host'));
  },

  /** Render the usage dashboard, loading it the first time it is opened. */
  showUsage() {
    const host = $('#usage-host');
    if (!window.usageView) return;
    const projects = this.state.bootstrap ? this.state.bootstrap.projects : [];
    if (!this.usageReady) {
      this.usageReady = true;
      window.usageView.render(host, {
        projects,
        provider: this.providerParam(),
        providers: (this.state.bootstrap && this.state.bootstrap.providers) || [],
      });
    }
  },

  /** Move focus to the search box. Called from the native View menu. */
  focusSearch() {
    const search = $('#search');
    search.focus();
    search.select();
  },

  /* -------------------------------------------------------------- theme */

  /** Read the theme the user last chose, defaulting to "system". */
  readStoredTheme() {
    // An explicit ?theme= wins, which makes the UI reproducible in
    // screenshots and lets a user pin a theme from a shortcut.
    const forced = new URLSearchParams(window.location.search).get('theme');
    if (['system', 'light', 'dark'].includes(forced)) return forced;
    try {
      return localStorage.getItem('theme') || 'system';
    } catch {
      return 'system';
    }
  },

  /**
   * Apply a theme.
   * @param {'system'|'light'|'dark'} theme
   */
  applyTheme(theme) {
    this.state.theme = theme;
    // Keep the document attribute authoritative from the first paint.
    const dark = theme === 'dark'
      || (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    // Chart marks read CSS custom properties at build time, so a theme
    // change has to rebuild them rather than relying on cascade.
    if (this.usageReady && window.usageView && window.usageView.state.data) {
      window.usageView.paint();
    }
    try {
      localStorage.setItem('theme', theme);
    } catch {
      /* storage can be unavailable; the theme still applies for this run */
    }
  },

  /** Cycle system -> light -> dark. Called from the native View menu. */
  cycleTheme() {
    const order = ['system', 'light', 'dark'];
    const next = order[(order.indexOf(this.state.theme) + 1) % order.length];
    this.applyTheme(next);
    api.send('PATCH', '/api/config', { theme: next }).catch(() => {});
    toast('Theme: ' + next);
  },

  /* ------------------------------------------------------------ filters */

  /** Populate the filter dropdowns from the bootstrap payload. */
  fillFilters(boot) {
    const projects = $('#filter-project');
    projects.replaceChildren(el('option', { value: '', text: 'All projects' }));
    for (const project of boot.projects) {
      const label = `${project.name} (${project.session_count})`;
      projects.append(el('option', {
        value: project.key || project.dir_name,
        text: project.exists ? label : label + ' · folder gone',
      }));
    }

    const models = $('#filter-model');
    models.replaceChildren(el('option', { value: '', text: 'Model' }));
    for (const model of boot.distinct.models) {
      models.append(el('option', { value: model, text: model }));
    }

    const tools = $('#filter-tool');
    tools.replaceChildren(el('option', { value: '', text: 'Tool' }));
    for (const tool of boot.distinct.tools) {
      tools.append(el('option', { value: tool, text: tool }));
    }
  },

  /* ----------------------------------------------------------- sessions */

  /** Fetch and render the session list for the current filters. */
  async loadSessions() {
    const list = $('#session-list');
    list.replaceChildren(el('div', {
      class: 'placeholder',
      style: 'padding:24px',
      html: '<span class="spinner"></span>',
    }));
    try {
      const data = await api.get('/api/sessions', {
        provider: this.providerParam(),
        project: $('#filter-project').value,
        model: $('#filter-model').value,
        tool: $('#filter-tool').value,
        sort: $('#sort').value,
        only_favorites: this.state.onlyFavorites ? 'true' : '',
      });
      this.state.sessions = data.sessions;
      this.state.cursor = -1;
      this.renderSessions(data);
    } catch (error) {
      list.replaceChildren();
      toast('Could not list sessions', error.message, 'error');
    }
  },

  /** Render the session rows. */
  renderSessions(data) {
    const list = $('#session-list');
    $('#list-count').textContent = `${data.total} session${data.total === 1 ? '' : 's'}`;
    $('#list-hint').textContent = '';

    if (!data.sessions.length) {
      list.replaceChildren(el('div.placeholder', { style: 'padding:30px' }, [
        el('h2', { text: 'Nothing matches' }),
        el('p', { text: 'Try clearing a filter.' }),
      ]));
      return;
    }

    const rows = data.sessions.map((session, index) => this.sessionRow(session, index));
    list.replaceChildren(...rows);
    this.paintSelection();
  },

  /** Build one session row. */
  sessionRow(session, index) {
    const projectName = (session.project_path || session.project_dir).split('/').filter(Boolean).pop()
      || session.project_dir;

    const pills = [];
    if (session.git_branch && session.git_branch !== 'HEAD') {
      pills.push(el('span.pill', { text: session.git_branch }));
    }
    if (!session.project_exists) {
      pills.push(el('span.pill.warn', { text: 'folder gone', title: session.project_path }));
    }
    if (session.corrupt_lines) {
      pills.push(el('span.pill.error', {
        text: `${session.corrupt_lines} bad line${session.corrupt_lines === 1 ? '' : 's'}`,
      }));
    }

    const row = el('div.session-item', {
      dataset: { id: session.session_id, index: String(index) },
      title: session.preview || session.title,
      onclick: (event) => {
        // Ctrl/Cmd click toggles multi-selection instead of opening.
        if (event.ctrlKey || event.metaKey) {
          event.preventDefault();
          this.toggleSelection(session.session_id);
          return;
        }
        if (event.shiftKey && this.state.selection.size) {
          event.preventDefault();
          this.extendSelection(index);
          return;
        }
        this.openSession(session);
      },
      oncontextmenu: (event) => {
        event.preventDefault();
        this.toggleSelection(session.session_id);
      },
    }, [
      el('div.row-top', {}, [
        providerBadge(this.provider(session.provider)),
        el('div.title', { text: session.title || session.session_id }),
        el('button', {
          class: session.is_favorite ? 'star on' : 'star',
          text: session.is_favorite ? '\u2605' : '\u2606',
          title: session.is_favorite ? 'Remove from favourites' : 'Add to favourites',
          onclick: (event) => {
            event.stopPropagation();
            this.toggleFavorite(session, event.currentTarget);
          },
        }),
      ]),
      el('div.sub', {}, [
        el('span.project.truncate', { text: projectName, title: session.project_path }),
        el('span.faint', { text: when(session.last_timestamp) }),
      ]),
      (session.tags || []).length
        ? el('div.sub', {}, session.tags.map((tag) => el('span.tag', { text: tag })))
        : null,
      session.note ? el('span.pill', { text: 'note' }) : null,
      el('div.sub.stats', {}, [
        el('span.num', { text: `${session.message_count} msg` }),
        el('span.num', { text: compact(session.total_tokens) + ' tok' }),
        el('span.num', { text: money(session.estimated_cost) }),
        el('span.num', { text: duration(session.duration_seconds) }),
        el('span.num', { text: bytes(session.file_size) }),
      ]),
      pills.length ? el('div.sub', {}, pills) : null,
    ]);
    return row;
  },

  /** Move the keyboard cursor through the list. */
  moveCursor(delta) {
    if (!this.state.sessions.length) return;
    const next = Math.max(0, Math.min(this.state.sessions.length - 1, this.state.cursor + delta));
    this.state.cursor = next;
    const rows = $$('#session-list .session-item');
    rows.forEach((row, index) => row.classList.toggle('selected', index === next));
    const active = rows[next];
    if (active) active.scrollIntoView({ block: 'nearest' });
  },

  /**
   * Honour a deep link of the form `?open=<session-id>&line=<n>`.
   * `line` or `uuid` scrolls straight to one message, which is what the
   * search results link to.
   */
  openDeepLink() {
    const params = new URLSearchParams(window.location.search);
    const view = params.get('view')
      || (this.state.bootstrap && this.state.bootstrap.config.last_view);
    if (view && view !== 'sessions') this.setView(view);
    const wanted = params.get('open');
    if (!wanted) return;
    const jump = this.deepLinkJump(params);
    const session = this.state.sessions.find((s) => s.session_id === wanted)
      || { session_id: wanted };
    this.openSession(session, jump);
  },

  /** Extract a jump target from URL parameters, or undefined. */
  deepLinkJump(params) {
    const line = Number(params.get('line'));
    const uuid = params.get('uuid');
    if (!uuid && !Number.isFinite(line)) return undefined;
    if (!uuid && !line) return undefined;
    return { line: line || undefined, uuid: uuid || undefined };
  },

  /**
   * Install a parsed conversation into the viewer.
   * Shared by the normal fetch path and the deep-link preload.
   */
  mountConversation(parsed, jump) {
    const content = $('#session-content');
    this.state.conversation = parsed;
    this.state.loadedId = parsed.meta ? parsed.meta.session_id : null;
    if (this.viewer) this.viewer.destroy();
    content.replaceChildren();
    this.viewer = new window.ConversationViewer(content, {
      onInspect: (message) => this.showMessageInfo(message),
      onCopyMarkdown: () => this.copyConversation(),
      onExport: (event) => this.showExportMenu(event),
      actions: (sessionId) => this.sessionActions(sessionId),
      provider: (id) => this.provider(id),
      providerBadge: (id) => providerBadge(this.provider(id), 'md'),
    });
    this.viewer.load(parsed);
    if (jump) this.viewer.jumpTo(jump);
    if (this.state.loadedId) {
      const index = this.state.sessions.findIndex(
        (s) => s.session_id === this.state.loadedId,
      );
      if (index >= 0) {
        this.state.cursor = index;
        $$('#session-list .session-item').forEach((row, position) => {
          row.classList.toggle('selected', position === index);
        });
      }
    }
    if (parsed.errors && parsed.errors.length) {
      toast(
        'Transcript has unparsable lines',
        `${parsed.errors.length} line(s) could not be read and were skipped.`,
        'warn',
      );
    }
  },

  /**
   * Open a session in the conversation viewer.
   * @param {object} session  a row from the session list
   * @param {{line?:number, uuid?:string}} [jump]  scroll target after load
   */
  async openSession(session, jump) {
    if (!session) return;
    const id = session.session_id;
    this.state.selectedId = id;

    const index = this.state.sessions.findIndex((s) => s.session_id === id);
    if (index >= 0) {
      this.state.cursor = index;
      $$('#session-list .session-item').forEach((row, position) => {
        row.classList.toggle('selected', position === index);
      });
    }

    const content = $('#session-content');
    // A cached conversation makes jumping between search hits instant.
    if (this.state.loadedId === id && this.viewer) {
      if (jump) this.viewer.jumpTo(jump);
      return;
    }

    content.replaceChildren(el('div.placeholder', {}, [el('span.spinner')]));
    this.setStatus('busy', 'Opening');
    try {
      const parsed = await api.get(`/api/sessions/${encodeURIComponent(id)}/messages`);
      this.mountConversation(parsed, jump);
      this.setStatus('ok', 'Ready');
    } catch (error) {
      this.state.loadedId = null;
      this.setStatus('error', 'Failed to open');
      content.replaceChildren(el('div.placeholder', {}, [
        el('h2', { text: 'Could not open that session' }),
        el('p', { text: error.message }),
      ]));
    }
  },

  /** Side panel with one message's metadata. */
  showMessageInfo(message) {
    const usage = message.usage || {};
    const rows = [
      ['Role', message.kind],
      ['Timestamp', message.timestamp ? new Date(message.timestamp).toLocaleString() : '\u2014'],
      ['Model', message.model || '\u2014'],
      ['Message UUID', message.uuid || '\u2014'],
      ['Parent UUID', message.parent_uuid || '\u2014'],
      ['Transcript line', String(message.line)],
      ['Sub-agent branch', message.is_sidechain ? 'yes' : 'no'],
      ['Flagged as error', message.is_error ? 'yes' : 'no'],
    ];
    if (message.extra && message.extra.requestId) rows.push(['Request id', message.extra.requestId]);
    if (message.extra && message.extra.effort) rows.push(['Effort', message.extra.effort]);
    if (message.extra && Array.isArray(message.extra.merged_uuids)) {
      rows.push(['Merged lines', `${message.extra.merged_uuids.length + 1} lines of one API response`]);
    }
    if (message.usage) {
      rows.push(
        ['Input tokens', (usage.input || 0).toLocaleString()],
        ['Output tokens', (usage.output || 0).toLocaleString()],
        ['Thinking tokens', (usage.thinking || 0).toLocaleString()],
        ['Cache write (5m)', (usage.cache_write_5m || 0).toLocaleString()],
        ['Cache write (1h)', (usage.cache_write_1h || 0).toLocaleString()],
        ['Cache read', (usage.cache_read || 0).toLocaleString()],
      );
    }

    const grid = el('div.inspect-grid');
    for (const [label, value] of rows) {
      grid.append(el('div.label', { text: label }), el('div.value', { text: value }));
    }
    openModal('Message details', grid);
  },

  /* ----------------------------------------------------------- selection */

  /** Add or remove one session from the multi-selection. */
  toggleSelection(sessionId) {
    if (this.state.selection.has(sessionId)) this.state.selection.delete(sessionId);
    else this.state.selection.add(sessionId);
    this.paintSelection();
  },

  /** Select everything between the last selected row and `index`. */
  extendSelection(index) {
    const ids = this.state.sessions.map((s) => s.session_id);
    let anchor = index;
    for (let i = 0; i < ids.length; i += 1) {
      if (this.state.selection.has(ids[i])) anchor = i;
    }
    const [from, to] = anchor <= index ? [anchor, index] : [index, anchor];
    for (let i = from; i <= to; i += 1) this.state.selection.add(ids[i]);
    this.paintSelection();
  },

  /** Select or clear every session currently listed. */
  selectAll(on) {
    if (on) for (const session of this.state.sessions) this.state.selection.add(session.session_id);
    else this.state.selection.clear();
    this.paintSelection();
  },

  /** Reflect the selection in the rows and the action bar. */
  paintSelection() {
    const selected = this.state.selection;
    $$('#session-list .session-item').forEach((row) => {
      row.classList.toggle('checked', selected.has(row.dataset.id));
    });
    const bar = $('#selection-bar');
    if (!selected.size) {
      bar.classList.add('hidden');
      return;
    }
    bar.classList.remove('hidden');
    const total = this.state.sessions
      .filter((s) => selected.has(s.session_id))
      .reduce((sum, s) => sum + (s.file_size || 0), 0);
    $('#selection-count').textContent =
      `${selected.size} selected \u00b7 ${bytes(total)}`;
  },

  /* ------------------------------------------------------- annotations */

  /** Toggle the favourite flag, updating the star in place. */
  async toggleFavorite(session, button) {
    try {
      const result = await api.send(
        'POST', `/api/sessions/${encodeURIComponent(session.session_id)}/annotate`,
        { favorite: !session.is_favorite },
      );
      session.is_favorite = result.is_favorite;
      if (button) {
        button.classList.toggle('on', result.is_favorite);
        button.textContent = result.is_favorite ? '\u2605' : '\u2606';
      }
    } catch (error) {
      toast('Could not update favourites', error.message, 'error');
    }
  },

  /** Edit the tags and personal note kept alongside a session. */
  async editAnnotations(sessionId) {
    let session = this.state.sessions.find((s) => s.session_id === sessionId);
    if (!session) {
      try {
        session = await api.get(`/api/sessions/${encodeURIComponent(sessionId)}`);
      } catch (error) {
        toast('Could not load the session', error.message, 'error');
        return;
      }
    }

    const tagsInput = el('input', {
      type: 'text',
      value: (session.tags || []).join(', '),
      placeholder: 'comma, separated, tags',
    });
    const noteArea = el('textarea.note-area', { placeholder: 'Your own notes about this session' });
    noteArea.value = session.note || '';

    const body = el('div', {}, [
      el('p.faint', {
        style: 'margin:0 0 10px;font-size:11.5px',
        text: 'Tags and notes are stored in the dashboard\u2019s own config. '
          + 'Nothing is written into any tool\u2019s own files.',
      }),
      el('label', { style: 'display:block;margin-bottom:4px;font-size:12px' }, ['Tags']),
      tagsInput,
      el('label', { style: 'display:block;margin:12px 0 4px;font-size:12px' }, ['Note']),
      noteArea,
      el('div', { style: 'display:flex;gap:7px;margin-top:11px' }, [
        el('button.primary', {
          text: 'Save',
          onclick: async () => {
            try {
              const result = await api.send(
                'POST', `/api/sessions/${encodeURIComponent(sessionId)}/annotate`,
                {
                  tags: tagsInput.value.split(',').map((t) => t.trim()).filter(Boolean),
                  note: noteArea.value,
                },
              );
              session.tags = result.tags;
              session.note = result.note;
              closeModal();
              toast('Saved', '', 'ok');
              await this.loadSessions();
            } catch (error) {
              toast('Could not save', error.message, 'error');
            }
          },
        }),
        el('button.bordered', { text: 'Cancel', onclick: closeModal }),
      ]),
    ]);
    openModal('Tags and notes', body);
  },

  /** Open a session by id, switching to the sessions view first. */
  async openSessionById(sessionId) {
    closeModal();
    this.setView('sessions');
    const session = this.state.sessions.find((s) => s.session_id === sessionId)
      || { session_id: sessionId };
    await this.openSession(session);
  },

  /* ------------------------------------------------------------ deletion */

  /**
   * Move sessions to the trash, after a native confirmation.
   * @param {string[]} ids
   */
  async deleteSessions(ids) {
    const list = (ids || []).filter(Boolean);
    if (!list.length) {
      toast('Nothing selected', 'Ctrl-click sessions to select them.', 'warn');
      return;
    }

    const chosen = this.state.sessions.filter((s) => list.includes(s.session_id));
    const names = chosen.length ? chosen : list.map((id) => ({ session_id: id, title: id }));
    const preview = names.slice(0, 8)
      .map((s) => `  \u2022 ${s.title || s.session_id}`)
      .join('\n');
    const more = names.length > 8 ? `\n  \u2026 and ${names.length - 8} more` : '';
    const totalBytes = chosen.reduce((sum, s) => sum + (s.file_size || 0), 0);
    const message =
      `Move ${list.length} transcript${list.length === 1 ? '' : 's'} to the dashboard trash?\n\n`
      + preview + more
      + `\n\n${bytes(totalBytes)} will be moved out of the tool\u2019s history into\n`
      + `~/.agentboard/trash, where you can restore it.\n`
      + 'Nothing is erased.';

    const confirmed = await this.confirm('Move to trash', message);
    if (!confirmed) return;

    this.setStatus('busy', 'Deleting');
    try {
      const result = await api.send('POST', '/api/sessions/delete', { session_ids: list });
      this.state.selection.clear();
      toast(
        `Moved ${result.deleted} to trash`,
        'Use the Trash panel to restore them.',
        'ok',
      );
      if (list.includes(this.state.loadedId)) {
        this.state.loadedId = null;
        $('#session-content').replaceChildren(el('div.placeholder', {}, [
          el('h2', { text: 'Session moved to trash' }),
          el('p', { text: 'Open the Trash panel to put it back.' }),
        ]));
      }
      await this.reload();
    } catch (error) {
      toast('Could not delete', error.message, 'error');
    } finally {
      this.setStatus('ok', 'Ready');
    }
  },

  /**
   * Ask for confirmation, natively when the app window provides it.
   * Falls back to the browser's own dialog outside the app.
   */
  async confirm(title, message) {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.confirm) {
      try {
        return Boolean(await window.pywebview.api.confirm(title, message));
      } catch {
        /* fall through to the web dialog */
      }
    }
    return window.confirm(`${title}\n\n${message}`);
  },

  /* --------------------------------------------------------------- trash */

  /** Show the trash, with restore and permanent-delete actions. */
  async showTrash() {
    let data;
    try {
      data = await api.get('/api/trash');
    } catch (error) {
      toast('Could not read the trash', error.message, 'error');
      return;
    }

    const body = el('div');
    body.append(el('p.faint', {
      style: 'margin:0 0 12px;font-size:12px',
      text: `${data.total_files} file(s), ${bytes(data.total_bytes)}. `
        + `Batches are purged automatically after ${data.retention_days} days.`,
    }));

    if (!data.batches.length) {
      body.append(el('p', { text: 'The trash is empty.' }));
      openModal('Trash', body);
      return;
    }

    for (const batch of data.batches) {
      const when = batch.deleted_at ? new Date(batch.deleted_at).toLocaleString() : 'unknown date';
      const items = el('div', { style: 'margin:5px 0 0;font-size:12px;color:var(--text-dim)' });
      for (const item of batch.items.slice(0, 6)) {
        items.append(el('div.truncate', { text: '\u2022 ' + (item.title || item.session_id) }));
      }
      if (batch.items.length > 6) {
        items.append(el('div.faint', { text: `\u2026 and ${batch.items.length - 6} more` }));
      }

      body.append(el('div', {
        style: 'border:1px solid var(--border);border-radius:7px;padding:10px;margin-bottom:9px',
      }, [
        el('div', { style: 'display:flex;align-items:center;gap:9px' }, [
          el('div', { style: 'flex:1;min-width:0' }, [
            el('div', { text: `${batch.count} file(s) \u00b7 ${bytes(batch.total_bytes)}`,
                        style: 'font-weight:600' }),
            el('div.faint', { text: when, style: 'font-size:11px' }),
          ]),
          el('button.bordered', {
            text: 'Restore',
            onclick: async () => {
              try {
                const result = await api.send('POST', '/api/trash/restore',
                                              { batch_id: batch.batch_id });
                const skipped = result.skipped.length;
                toast(
                  `Restored ${result.restored.length}`,
                  skipped ? `${skipped} skipped: ${result.skipped[0].reason}` : '',
                  skipped ? 'warn' : 'ok',
                );
                closeModal();
                await this.reload();
              } catch (error) {
                toast('Restore failed', error.message, 'error');
              }
            },
          }),
          el('button.bordered', {
            text: 'Delete forever',
            style: 'color:var(--error)',
            onclick: async () => {
              const ok = await this.confirm(
                'Delete permanently',
                `Permanently erase ${batch.count} transcript(s) from the trash?\n\n`
                + 'This cannot be undone.',
              );
              if (!ok) return;
              try {
                await api.send('DELETE', `/api/trash/${encodeURIComponent(batch.batch_id)}`);
                toast('Deleted permanently', '', 'ok');
                closeModal();
                this.showTrash();
              } catch (error) {
                toast('Could not delete', error.message, 'error');
              }
            },
          }),
        ]),
        items,
      ]));
    }
    openModal('Trash', body);
  },

  /* --------------------------------------------------- bulk maintenance */

  /** Offer the bulk clean-up rules. */
  showMaintenance() {
    const body = el('div');
    body.append(el('p.faint', {
      style: 'margin:0 0 12px;font-size:12px',
      text: 'Pick a rule to see what it matches. Nothing is removed until you confirm.',
    }));

    const rules = [
      ['older_than', 'Sessions older than 30 days', { days: 30 }],
      ['older_than', 'Sessions older than 90 days', { days: 90 }],
      ['fewer_messages', 'Aborted sessions (under 2 messages)', { messages: 2 }],
      ['fewer_messages', 'Short sessions (under 10 messages)', { messages: 10 }],
      ['orphaned', 'Sessions from deleted project folders', {}],
    ];
    for (const [rule, label, params] of rules) {
      body.append(el('button.bordered', {
        text: label,
        style: 'display:block;width:100%;text-align:left;padding:8px 11px;margin-bottom:6px',
        onclick: () => this.previewMaintenance(rule, params, label),
      }));
    }
    openModal('Bulk clean-up', body);
  },

  /** Show what a maintenance rule matches, then offer to delete it. */
  async previewMaintenance(rule, params, label) {
    let data;
    try {
      data = await api.get('/api/maintenance/candidates', { rule, ...params });
    } catch (error) {
      toast('Could not evaluate that rule', error.message, 'error');
      return;
    }

    const body = el('div');
    body.append(el('p', {
      style: 'margin:0 0 10px',
      text: `${data.count} session(s) match ${data.description}, `
        + `totalling ${bytes(data.total_bytes)}.`,
    }));

    if (!data.count) {
      body.append(el('p.faint', { text: 'Nothing to clean up.' }));
      openModal(label, body);
      return;
    }

    const list = el('div', {
      style: 'max-height:260px;overflow:auto;border:1px solid var(--border);'
        + 'border-radius:7px;padding:8px;font-size:12px;margin-bottom:12px',
    });
    for (const session of data.sessions.slice(0, 200)) {
      list.append(el('div.truncate', {
        text: `${session.title} \u2014 ${bytes(session.file_size)}, ${session.message_count} msg`,
      }));
    }
    body.append(list);

    body.append(el('button.primary', {
      text: `Move ${data.count} session(s) to trash`,
      onclick: () => {
        closeModal();
        this.deleteSessions(data.sessions.map((s) => s.session_id));
      },
    }));
    openModal(label, body);
  },

  /**
   * Build the per-session action buttons shown in the viewer header.
   * Resume is disabled, with an explanation, when the folder has gone.
   */
  async sessionActions(sessionId) {
    const host = el('div', { style: 'display:flex;gap:5px;flex-wrap:wrap;align-items:center' });
    const session = (this.state.conversation && this.state.conversation.meta)
      || this.state.sessions.find((s) => s.session_id === sessionId) || {};
    const provider = this.provider(session.provider) || { capabilities: {}, name: 'This provider' };
    const caps = provider.capabilities || {};
    let info = null;
    if (provider.can_resume) {
      try {
        info = await api.get(`/api/sessions/${encodeURIComponent(sessionId)}/resume-command`);
      } catch {
        info = null;
      }
    }
    if (!info) {
      // No resume for this provider: the folder buttons still work, and
      // an explicit note says why Resume is missing.
      const exists = Boolean(session.project_exists);
      info = {
        project_exists: exists, cwd: session.project_path, command: null,
        editor_available: true,
        reason: exists ? '' : `The project folder ${session.project_path || '(unknown)'} no longer exists.`,
      };
    }

    if (info.command) host.append(...this.resumeButtons(sessionId, info));
    else {
      host.append(el('span.capability-note', {
        text: 'No resume',
        title: `${provider.name} cannot reopen a session by id. Set a resume command for it in Settings \u2192 Providers.`,
      }));
    }

    const folder = el('button.bordered', {
      text: 'Folder',
      title: info.project_exists ? `Open ${info.cwd}` : info.reason,
      onclick: () => this.openLocation(sessionId, 'folder'),
    });
    if (!info.project_exists) folder.setAttribute('disabled', 'disabled');
    host.append(folder);

    if (info.editor_available) {
      const editor = el('button.bordered', {
        text: 'Editor',
        title: info.project_exists ? 'Open the project in your editor' : info.reason,
        onclick: () => this.openLocation(sessionId, 'editor'),
      });
      if (!info.project_exists) editor.setAttribute('disabled', 'disabled');
      host.append(editor);
    }

    host.append(el('button.bordered', {
      text: 'Tags & note',
      title: 'Kept in the dashboard config, never in the tool\u2019s own files',
      onclick: () => this.editAnnotations(sessionId),
    }));

    if (caps.delete) {
      host.append(el('button.danger', {
        text: 'Delete',
        title: 'Move this transcript to the dashboard trash',
        onclick: () => this.deleteSessions([sessionId]),
      }));
    } else {
      host.append(el('span.capability-note', {
        text: 'Read-only',
        title: `Deleting ${provider.name} sessions is not supported; its files are never modified.`,
      }));
    }
    return host;
  },

  /** The Resume and Copy command buttons, for providers that can resume. */
  resumeButtons(sessionId, info) {
    const resume = el('button.bordered', {
      text: 'Resume',
      title: info.project_exists
        ? `Open a terminal in ${info.cwd} and run ${info.command}`
        : info.reason,
      onclick: () => this.resumeSession(sessionId),
    });
    if (!info.project_exists) {
      resume.setAttribute('disabled', 'disabled');
      resume.title = info.reason;
    }
    const copy = el('button.bordered', {
      text: 'Copy command',
      title: 'Copy the resume command to the clipboard',
      onclick: () => this.copyResumeCommand(sessionId),
    });
    return [resume, copy];
  },

  /* -------------------------------------------------------------- resume */

  /** Open a terminal in the session's directory, running the resume command. */
  async resumeSession(sessionId) {
    const id = sessionId || this.state.loadedId || this.currentSessionId();
    if (!id) {
      toast('No session selected', '', 'warn');
      return;
    }
    try {
      const info = await api.get(`/api/sessions/${encodeURIComponent(id)}/resume-command`);
      if (!info.project_exists) {
        toast('Cannot resume', info.reason, 'warn');
        return;
      }
      const result = await api.send('POST', `/api/sessions/${encodeURIComponent(id)}/resume`);
      toast('Terminal opened', `${result.program}: ${result.command}`, 'ok');
    } catch (error) {
      toast('Could not resume', error.message, 'error');
    }
  },

  /** Copy the resume command, so it can be pasted into any shell. */
  async copyResumeCommand(sessionId) {
    const id = sessionId || this.state.loadedId || this.currentSessionId();
    if (!id) return;
    try {
      const info = await api.get(`/api/sessions/${encodeURIComponent(id)}/resume-command`);
      await this.copyText(info.shell_line);
      toast('Command copied', info.shell_line, 'ok');
    } catch (error) {
      toast('Could not copy', error.message, 'error');
    }
  },

  /** Reveal the project directory, or open it in the editor. */
  async openLocation(sessionId, where) {
    const id = sessionId || this.state.loadedId || this.currentSessionId();
    if (!id) return;
    try {
      const result = await api.send(
        'POST', `/api/sessions/${encodeURIComponent(id)}/open?where=${where}`,
      );
      toast(where === 'folder' ? 'Opened in the file manager' : 'Opened in the editor',
            result.path, 'ok');
    } catch (error) {
      toast('Could not open', error.message, 'error');
    }
  },

  /** The session under the keyboard cursor, if any. */
  currentSessionId() {
    const session = this.state.sessions[this.state.cursor];
    return session ? session.session_id : null;
  },

  /** Re-read the index summary and the session list after a change. */
  async reload() {
    const boot = await api.get('/api/bootstrap').catch(() => null);
    if (boot) {
      this.state.bootstrap = boot;
      this.renderProviderSwitch();
      if (this.state.provider === 'all') this.fillFilters(boot);
      else await this.refreshFilters();
      this.renderCorpus(boot.stats);
    }
    await this.loadSessions();
  },

  /* -------------------------------------------------------------- export */

  /** Copy the whole conversation to the clipboard as Markdown. */
  async copyConversation() {
    const id = this.state.loadedId;
    if (!id) {
      toast('Open a conversation first', '', 'warn');
      return;
    }
    try {
      const result = await api.get(`/api/sessions/${encodeURIComponent(id)}/export`, {
        format: 'markdown',
      });
      await this.copyText(result.content);
      toast('Copied as Markdown', `${result.bytes.toLocaleString()} bytes on the clipboard`, 'ok');
    } catch (error) {
      toast('Copy failed', error.message, 'error');
    }
  },

  /**
   * Put text on the clipboard.
   * The async Clipboard API is unavailable in some webviews, so fall back
   * to a hidden textarea and `execCommand`.
   */
  async copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch {
      /* fall through to the legacy path */
    }
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', '');
    area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
    document.body.append(area);
    area.select();
    let ok = false;
    try {
      ok = document.execCommand('copy');
    } catch {
      ok = false;
    }
    area.remove();
    if (!ok) throw new Error('the clipboard is not available in this window');
    return true;
  },

  /** Small menu listing the export formats. */
  showExportMenu(event) {
    const formats = [
      ['markdown', 'Markdown (.md)'],
      ['html', 'HTML (.html)'],
      ['json', 'JSON (.json)'],
    ];
    const list = el('div', { style: 'display:flex;flex-direction:column;gap:6px' });
    for (const [format, label] of formats) {
      list.append(el('button.bordered', {
        text: label,
        style: 'text-align:left;padding:8px 11px',
        onclick: () => { closeModal(); this.exportCurrent(format); },
      }));
    }
    list.append(el('p.faint', {
      style: 'margin:8px 0 0;font-size:11.5px',
      text: this.state.native
        ? 'The file is written through your system save dialog.'
        : 'Running in a browser: the file downloads instead.',
    }));
    openModal('Export conversation', list);
    if (event) event.stopPropagation();
  },

  /**
   * Export the open conversation, or the usage statistics.
   * Called from the native File menu as well as the Export button.
   * @param {'markdown'|'html'|'json'|'csv'} kind
   */
  async exportCurrent(kind) {
    if (kind === 'csv') {
      await this.exportUsageCsv();
      return;
    }
    const id = this.state.loadedId;
    if (!id) {
      toast('Open a conversation first', 'Select a session, then export it.', 'warn');
      return;
    }
    this.setStatus('busy', 'Rendering export');
    try {
      const result = await api.get(`/api/sessions/${encodeURIComponent(id)}/export`, {
        format: kind,
      });
      await this.saveFile(result.filename, result.content, kind);
    } catch (error) {
      toast('Export failed', error.message, 'error');
    } finally {
      this.setStatus('ok', 'Ready');
    }
  },

  /** Export the aggregated statistics as CSV. */
  async exportUsageCsv() {
    try {
      const text = await api.get('/api/usage/csv');
      await this.saveFile('agentboard-usage.csv', text, 'csv');
    } catch (error) {
      toast('Export failed', error.message, 'error');
    }
  },

  /**
   * Write a file, preferring the native save dialog.
   * The webview sandbox blocks downloads a page starts itself, so the
   * browser fallback only applies when running outside the app window.
   */
  async saveFile(filename, content, kind) {
    const types = {
      markdown: ['Markdown (*.md)'],
      html: ['HTML (*.html)'],
      json: ['JSON (*.json)'],
      csv: ['CSV (*.csv)'],
    }[kind] || [];

    if (window.pywebview && window.pywebview.api && window.pywebview.api.save_file) {
      const result = await window.pywebview.api.save_file(filename, content, types);
      if (result.cancelled) return;
      if (result.saved) toast('Saved', result.path, 'ok');
      else toast('Could not save', result.error || 'unknown error', 'error');
      return;
    }

    const blob = new Blob([content], { type: 'application/octet-stream' });
    const url = URL.createObjectURL(blob);
    const link = el('a', { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
    toast('Downloaded', filename, 'ok');
  },

  /* ------------------------------------------------------------- search */

  /** Run a full-text search, or restore the plain list when cleared. */
  async runSearch(term) {
    if (!term || term.trim().length < 2) {
      if (this.state.searchMode) {
        this.state.searchMode = false;
        await this.loadSessions();
      }
      return;
    }
    this.state.searchMode = true;
    this.setStatus('busy', 'Searching');
    const list = $('#session-list');
    try {
      const result = await api.get('/api/search', {
        q: term,
        project: $('#filter-project').value,
        provider: this.providerParam(),
      });
      if (result.error) {
        toast('Search problem', result.error, 'warn');
      }
      $('#list-count').textContent =
        `${result.hits.length} hit${result.hits.length === 1 ? '' : 's'} in ${result.session_count} session${result.session_count === 1 ? '' : 's'}`;
      $('#list-hint').textContent =
        `${result.elapsed_seconds.toFixed(2)}s${result.truncated ? ' · capped' : ''}`;

      if (!result.hits.length) {
        list.replaceChildren(el('div.placeholder', { style: 'padding:30px' }, [
          el('h2', { text: 'No matches' }),
          el('p', { text: `Nothing in ${result.scanned_files} transcripts contains that.` }),
        ]));
      } else {
        list.replaceChildren(...result.hits.map((hit) => this.searchRow(hit, term)));
      }
      this.setStatus('ok', 'Ready');
    } catch (error) {
      this.setStatus('error', 'Search failed');
      toast('Search failed', error.message, 'error');
    }
  },

  /** One search-result row, with the matched span highlighted. */
  searchRow(hit, term) {
    const before = escapeHtml(hit.snippet.slice(0, hit.match_start));
    const match = escapeHtml(hit.snippet.substr(hit.match_start, hit.match_length));
    const after = escapeHtml(hit.snippet.slice(hit.match_start + hit.match_length));
    const highlighted = match
      ? `${before}<mark style="background:var(--accent-soft);color:var(--accent);border-radius:2px">${match}</mark>${after}`
      : escapeHtml(hit.snippet);

    return el('div.session-item', {
      title: 'Open this session and scroll to the match',
      onclick: () => {
        const session = this.state.sessions.find((s) => s.session_id === hit.session_id)
          || { session_id: hit.session_id };
        this.openSession(session, { line: hit.line, uuid: hit.uuid });
      },
    }, [
      el('div.title', { text: hit.title, style: 'font-size:12px' }),
      el('div', { html: highlighted, style: 'font-size:11.5px;color:var(--text-dim);line-height:1.5' }),
      el('div.sub', {}, [
        providerBadge(this.provider(hit.provider)),
        el('span', {
          text: hit.role === 'user' ? 'you'
            : ((this.provider(hit.provider) || {}).assistant_label || hit.role || '').toLowerCase(),
        }),
        el('span', { text: when(hit.timestamp) }),
        el('span.faint', { text: 'line ' + hit.line }),
      ]),
    ]);
  },

  /* -------------------------------------------------------------- index */

  /** Trigger a full re-index. Called from the native File menu and F5. */
  async rebuild() {
    try {
      const result = await api.send('POST', '/api/index/rebuild?force=true');
      if (!result.started) {
        toast('Already indexing', result.reason || '', 'warn');
        return;
      }
      this.setStatus('busy', 'Indexing');
      this.pollIndexProgress(true);
    } catch (error) {
      toast('Could not start indexing', error.message, 'error');
    }
  },

  /** Alias used by the tray "Refresh" item. */
  refresh() {
    this.rebuild();
  },

  /** Poll indexing progress and drive the progress bar. */
  async pollIndexProgress(force = false) {
    const bar = $('#progress');
    const fill = bar.firstElementChild;
    let sawRunning = force;

    const tick = async () => {
      let status;
      try {
        status = await api.get('/api/index/status');
      } catch {
        bar.classList.remove('visible');
        return;
      }
      if (status.running) {
        sawRunning = true;
        bar.classList.add('visible');
        fill.style.width = status.percent + '%';
        this.setStatus('busy', `Indexing ${status.done}/${status.total}`);
        setTimeout(tick, 120);
        return;
      }
      bar.classList.remove('visible');
      fill.style.width = '0';
      if (sawRunning) {
        this.setStatus('ok', 'Ready');
        toast(
          'Index rebuilt',
          `${status.scanned} scanned, ${status.from_cache} from cache, ${status.elapsed_seconds}s`,
          'ok',
        );
        const boot = await api.get('/api/bootstrap').catch(() => null);
        if (boot) {
          this.state.bootstrap = boot;
          this.renderProviderSwitch();
          this.fillFilters(boot);
          this.renderCorpus(boot.stats);
        }
        await this.loadSessions();
        if (this.usageReady && window.usageView) window.usageView.refresh();
      }
    };
    tick();
  },

  /* ------------------------------------------------------------- status */

  /**
   * Update the status bar.
   * @param {'ok'|'busy'|'error'} kind
   */
  setStatus(kind, text) {
    const dot = $('#status-dot');
    dot.className = kind === 'ok' ? 'dot' : `dot ${kind}`;
    $('#status-text').textContent = text;
  },

  /** Summarise the corpus in the status bar. */
  renderCorpus(stats) {
    $('#status-corpus').textContent =
      `${stats.session_count} sessions · ${stats.project_count} projects · ` +
      `${compact(stats.message_count)} messages · ${compact(stats.total_tokens)} tokens · ` +
      bytes(stats.file_size);
  },

  /**
   * Friendly screen for a machine where no supported tool has history yet:
   * every provider is listed with where it was looked for.
   */
  renderEmptyState(boot) {
    const providers = (boot.providers || []).filter((p) => p.enabled);
    $('#session-content').replaceChildren(el('div.placeholder.onboarding', {}, [
      el('h2', { text: 'No AI assistant history found yet' }),
      el('p', { text: 'Agentboard reads the local history of these tools. Use any of them once and its sessions appear here.' }),
      el('ul.onboarding-list', {}, providers.map((p) => el('li', {}, [
        providerBadge(p, 'md'),
        el('div', {}, [
          el('strong', { text: p.name }),
          el('div.faint', {}, [
            p.detection.installed ? 'installed, no history in ' : 'not installed \u00b7 looks in ',
            el('code', { text: p.detection.root }),
          ]),
        ]),
      ]))),
      el('button.primary', {
        text: 'Open settings',
        onclick: () => this.setView('config'),
      }),
    ]));
    $('#list-count').textContent = '0 sessions';
  },

  /* ---------------------------------------------------------- shortcuts */

  /** Show the shortcut reference. Called from the native Help menu. */
  showShortcuts() {
    const rows = [
      ['/', 'Focus the search box'],
      ['j / k', 'Next / previous session'],
      ['Enter', 'Open the highlighted session'],
      ['1 2 3', 'Sessions / Usage / Settings'],
      ['p', 'Next provider (Shift+p: previous)'],
      ['e', 'Expand every block in the conversation'],
      ['c', 'Collapse them again'],
      ['r', 'Resume the session in a terminal'],
      ['x', 'Add or remove the session from the selection'],
      ['Del', 'Move the selection to the trash'],
      ['f', 'Star or unstar the highlighted session'],
      ['t', 'Edit its tags and note'],
      ['b', 'Back up every transcript to a ZIP'],
      ['F5', 'Rebuild the index'],
      ['Esc', 'Clear search or close a dialog'],
      ['?', 'This list'],
    ];
    const grid = el('div.shortcut-grid');
    for (const [keys, description] of rows) {
      grid.append(
        el('div.keys', {}, keys.split(' ').map((key) => el('kbd', { text: key }))),
        el('div.desc', { text: description }),
      );
    }
    openModal('Keyboard shortcuts', grid);
  },

  /** Open a link in the real browser rather than inside the app window. */
  openExternal(url) {
    if (window.pywebview && window.pywebview.api && window.pywebview.api.open_external) {
      window.pywebview.api.open_external(url);
    } else {
      window.open(url, '_blank', 'noopener');
    }
  },
};

// The usage and config views call back into these.
dashboard.toast = toast;
dashboard.openModal = openModal;
dashboard.closeModal = closeModal;
dashboard.el = el;

window.dashboard = dashboard;
document.addEventListener('DOMContentLoaded', () => dashboard.init());
