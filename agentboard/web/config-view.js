/* The Settings view: providers, general preferences and pricing, then the
 * Claude Code panels (its config, CLAUDE.md, skills, todos) when that
 * provider is enabled.
 *
 * Every tool's own files are shown read-only. The only writes this view
 * performs are editing a CLAUDE.md (with an automatic timestamped backup)
 * and changing the dashboard's own configuration in ~/.agentboard.
 *
 * Exposes `window.configView`.
 */
'use strict';

(function () {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  /** Build an HTML element. */
  function el(tag, attrs = {}, children = []) {
    const [name, ...classes] = tag.split('.');
    const node = document.createElement(name);
    if (classes.length) node.className = classes.join(' ');
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key === 'html') node.innerHTML = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, String(value));
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Bytes as a short human string. */
  function bytes(value) {
    const n = Number(value) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 ** 2) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
    return (n / 1024 ** 3).toFixed(2) + ' GB';
  }

  /** Pretty-print a JSON value into a scrollable block. */
  function jsonBlock(value) {
    let text;
    try {
      text = JSON.stringify(value, null, 2);
    } catch {
      text = String(value);
    }
    return el('pre.json-block', { text });
  }

  /** A collapsible section, closed unless `open`. */
  function section(title, subtitle, body, open = false) {
    const wrapper = el('details.cfg-section', open ? { open: 'open' } : {});
    wrapper.append(el('summary', {}, [
      el('span.cfg-title', { text: title }),
      subtitle ? el('span.cfg-sub', { text: subtitle }) : null,
    ]));
    wrapper.append(el('div.cfg-body', {}, [body]));
    return wrapper;
  }

  /** A short empty state that says where the app looked. */
  function emptyState(message, path) {
    return el('div.cfg-empty', {}, [
      el('p', { text: message }),
      path ? el('code', { text: path }) : null,
    ]);
  }

  /** A new custom provider spec, offered as a starting point. */
  const SPEC_TEMPLATE = `{
  "id": "mytool",
  "name": "My Tool",
  "color": "#e11d48",
  "home": "~/.mytool",
  "glob": "sessions/**/*.jsonl",
  "format": "jsonl",
  "session": { "id": "session_id", "cwd": "cwd" },
  "fields": {
    "role": "role",
    "text": "content",
    "timestamp": "timestamp",
    "model": "model",
    "input_tokens": "usage.input_tokens",
    "output_tokens": "usage.output_tokens"
  }
}`;

  /** Capability flags in display order, with readable names. */
  const CAPABILITIES = [
    ['usage', 'Token usage'], ['cost', 'Cost estimate'], ['cache_tokens', 'Cache tokens'],
    ['tool_calls', 'Tool calls'], ['thinking', 'Reasoning'], ['search', 'Search'],
    ['live', 'Live updates'], ['resume', 'Resume'], ['delete', 'Delete'],
    ['reported_cost', 'Reported cost'], ['instructions', 'Instruction files'],
  ];

  /** A provider badge, drawn as in the shell. */
  function badge(provider, size = 'md') {
    return el(`span.provider-badge.${size}`, {
      style: `--p-color:${provider.color}`, title: provider.name,
      role: 'img', 'aria-label': provider.name, text: provider.monogram || '?',
    });
  }

  const configView = {
    state: {
      // ?tab= opens a section directly, e.g. ?view=config&tab=pricing.
      tab: new URLSearchParams(window.location.search).get('tab') || 'providers',
      data: {}, assetFilter: '', assetKind: '',
      pricingProvider: new URLSearchParams(window.location.search).get('pricing') || '',
      showAdd: false, draft: SPEC_TEMPLATE,
    },

    /** Show one tab, e.g. from the "change the path" link. */
    openTab(tab) {
      this.state.tab = tab;
      if (this.state.data.providers) this.paint();
    },

    /** Render the view, fetching everything it needs once. */
    async render(host) {
      this.host = host;
      host.replaceChildren(el('div.placeholder', {}, [el('span.spinner')]));
      try {
        const [providers, claudeConfig, appConfig, claudeMd, assets, todos, trash] = await Promise.all([
          this.get('/api/providers'),
          this.get('/api/claude-config'),
          this.get('/api/config'),
          this.get('/api/claude-md'),
          this.get('/api/assets'),
          this.get('/api/todos'),
          this.get('/api/trash'),
        ]);
        this.state.data = { providers, claudeConfig, appConfig, claudeMd, assets, todos, trash };
        this.paint();
      } catch (error) {
        host.replaceChildren(el('div.placeholder', {}, [
          el('h2', { text: 'Could not read the configuration' }),
          el('p', { text: error.message }),
        ]));
      }
    },

    /** GET a JSON endpoint, surfacing the server's own error message. */
    async get(path) {
      const response = await fetch(path, { headers: { Accept: 'application/json' } });
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || response.statusText);
      return body;
    },

    /** Build the tab chrome and the active panel. */
    paint() {
      const page = el('div.config-page');
      const tabs = el('div.cfg-tabs', { role: 'tablist', 'aria-label': 'Settings sections' });
      const counts = this.state.data.assets.counts || {};
      const providers = this.state.data.providers.providers;
      const claude = providers.find((p) => p.id === 'claude');
      const entries = [
        ['providers', `Providers (${providers.length})`],
        ['settings', 'General'],
        ['pricing', 'Pricing'],
      ];
      // Claude Code's own configuration panels, only while it is enabled.
      const claudeEntries = claude && claude.enabled ? [
        ['claude', 'Config'],
        ['instructions', `CLAUDE.md (${this.state.data.claudeMd.files.length})`],
        ['assets', `Skills & commands (${Object.values(counts).reduce((a, b) => a + b, 0)})`],
        ['todos', `Todos (${this.state.data.todos.total})`],
      ] : [];
      if (!entries.concat(claudeEntries).some(([key]) => key === this.state.tab)) {
        this.state.tab = 'providers';
      }
      const tab = ([key, label]) => el('button', {
        text: label,
        role: 'tab',
        'aria-selected': this.state.tab === key ? 'true' : 'false',
        class: this.state.tab === key ? 'on' : '',
        onclick: () => { this.state.tab = key; this.paint(); },
      });
      tabs.append(...entries.map(tab));
      if (claudeEntries.length) {
        tabs.append(el('span.cfg-tab-group', {}, [badge(claude, 'sm'), el('span', { text: 'Claude Code' })]));
        tabs.append(...claudeEntries.map(tab));
      }
      page.append(tabs);
      page.append(el('div.cfg-panel', {}, [this.panel()]));
      this.host.replaceChildren(page);
    },

    /** The panel for the active tab. */
    panel() {
      switch (this.state.tab) {
        case 'pricing': return this.pricingPanel();
        case 'claude': return this.claudePanel();
        case 'instructions': return this.instructionsPanel();
        case 'assets': return this.assetsPanel();
        case 'todos': return this.todosPanel();
        case 'settings': return this.settingsPanel();
        default: return this.providersPanel();
      }
    },

    /* --------------------------------------------------------- providers */

    /** Every provider: detection, switch, paths, capabilities; and custom ones. */
    providersPanel() {
      const { providers, errors } = this.state.data.providers;
      const wrap = el('div');
      wrap.append(el('div.cfg-toolbar', {}, [
        el('p.cfg-help', {
          text: 'Agentboard reads each tool\u2019s local history, read-only. Switch a provider off to '
            + 'hide it everywhere; point it at another folder if the tool keeps its data elsewhere.',
        }),
        el('button.bordered', { text: 'Detect again', onclick: () => this.providerAction('POST', '/api/providers/rescan', null, 'Providers detected again') }),
        el('button.primary', {
          text: this.state.showAdd ? 'Cancel' : 'Add a provider',
          'aria-expanded': this.state.showAdd ? 'true' : 'false',
          onclick: () => { this.state.showAdd = !this.state.showAdd; this.paint(); },
        }),
      ]));
      if (this.state.showAdd) wrap.append(this.addProviderForm());
      if ((errors || []).length) {
        wrap.append(el('div.cfg-errors', { role: 'alert' }, [
          el('strong', { text: 'Some provider specs could not be loaded' }),
          ...errors.map((e) => el('div', {}, [el('code', { text: e.source }), ` \u2014 ${e.error}`])),
        ]));
      }
      const list = el('div.provider-cards');
      for (const provider of providers) list.append(this.providerCard(provider));
      wrap.append(list);
      return wrap;
    },

    /** One provider's card. */
    providerCard(provider) {
      const found = provider.detection;
      const status = !provider.enabled ? ['off', 'Disabled']
        : provider.stats.sessions ? ['ok', `${provider.stats.sessions} session${provider.stats.sessions === 1 ? '' : 's'}`]
          : found.detected ? ['idle', 'Detected, no sessions yet'] : ['missing', 'Not detected'];
      const id = `provider-${provider.id}`;
      const field = (key, label, placeholder, help) => el('label.provider-field', {}, [
        el('span', { text: label }),
        el('input', {
          type: 'text',
          value: provider.settings[key] || '',
          placeholder,
          spellcheck: 'false',
          'aria-describedby': `${id}-${key}-help`,
          onchange: (event) => this.updateProvider(provider, { [key]: event.target.value.trim() }),
        }),
        el('span.cfg-help', { id: `${id}-${key}-help`, text: help }),
      ]);

      return el(provider.enabled ? 'article.provider-card' : 'article.provider-card.disabled', {
        style: `--p-color:${provider.color}`,
        'aria-labelledby': `${id}-name`,
      }, [
        el('div.provider-card-head', {}, [
          badge(provider, 'lg'),
          el('div.provider-card-title', {}, [
            el('h3', { id: `${id}-name`, text: provider.name }),
            el('div.cfg-help', { text: provider.description }),
          ]),
          el('span.status-pill', { 'data-status': status[0], text: status[1] }),
          el('label.switch', { title: provider.enabled ? 'Switch off' : 'Switch on' }, [
            el('input', {
              type: 'checkbox',
              role: 'switch',
              'aria-label': `Enable ${provider.name}`,
              ...(provider.enabled ? { checked: 'checked' } : {}),
              onchange: (event) => this.updateProvider(provider, { enabled: event.target.checked }),
            }),
            el('span.switch-track', { 'aria-hidden': 'true' }),
          ]),
        ]),
        el('p.provider-reason', {}, [
          el('span', { text: found.reason }),
        ]),
        el('div.provider-fields', {}, [
          field('path', 'Data folder', provider.default_home,
            'Empty means the default shown in the box.'),
          field('resume_command', 'Resume command',
            provider.default_resume_command || 'not supported by default',
            provider.default_resume_command
              ? '{session_id} is substituted. Empty means the default.'
              : 'Set one (with {session_id}) if the tool can reopen a session by id.'),
        ]),
        el('ul.capabilities', { 'aria-label': `${provider.name} capabilities` },
          CAPABILITIES.map(([key, label]) => {
            const on = key === 'resume' ? provider.can_resume : provider.capabilities[key];
            return el('li', {
              class: on ? 'on' : 'off',
              title: on ? `${label}: supported` : `${label}: not supported by ${provider.name}`,
            }, [el('span', { 'aria-hidden': 'true', text: on ? '\u2713' : '\u2013' }), ` ${label}`]);
          })),
        provider.custom ? el('div.provider-custom', {}, [
          el('span.cfg-help', {}, ['Custom provider from ', el('code', { text: provider.source || 'config' })]),
          (provider.source || '').startsWith('custom_providers')
            ? el('button.bordered', {
                text: 'Edit spec',
                onclick: () => {
                  this.state.showAdd = true;
                  this.state.draft = JSON.stringify(provider.spec, null, 2);
                  this.paint();
                },
              })
            : null,
          (provider.source || '').startsWith('custom_providers')
            ? el('button.danger', {
                text: 'Remove',
                onclick: async () => {
                  const ok = await window.dashboard.confirm('Remove provider',
                    `Remove ${provider.name}? Its sessions leave the dashboard; the tool's own files are not touched.`);
                  if (ok) this.providerAction('DELETE', `/api/providers/custom/${encodeURIComponent(provider.id)}`, null, `${provider.name} removed`);
                },
              })
            : el('span.cfg-help', { text: 'Delete that file to remove it.' }),
        ]) : null,
      ]);
    },

    /** The form for a spec-defined provider. */
    addProviderForm() {
      const area = el('textarea.cfg-editor.spec-editor', {
        spellcheck: 'false', 'aria-label': 'Provider spec (JSON)', rows: '18',
        oninput: (event) => { this.state.draft = event.target.value; },
      });
      area.value = this.state.draft;
      return el('div.cfg-section.add-provider', {}, [
        el('div.cfg-body', {}, [
          el('p.cfg-help', {
            text: 'Describe where the tool keeps its sessions and which JSON fields hold each value. '
              + 'Paths in "fields" are dotted (message.usage.input_tokens). Formats: "jsonl" (one record '
              + 'per line) or "json" (a document; set "messages_path"). See README \u2192 How to add a new AI provider.',
          }),
          area,
          el('div', { style: 'display:flex;gap:7px;margin-top:8px' }, [
            el('button.primary', {
              text: 'Save provider',
              onclick: async () => {
                let spec;
                try {
                  spec = JSON.parse(this.state.draft);
                } catch (error) {
                  window.dashboard.toast('That is not valid JSON', error.message, 'error');
                  return;
                }
                const ok = await this.providerAction('POST', '/api/providers/custom', spec, `${spec.name || spec.id} saved`);
                if (ok) { this.state.showAdd = false; this.state.draft = SPEC_TEMPLATE; this.paint(); }
              },
            }),
            el('button.bordered', {
              text: 'Reset to the template',
              onclick: () => { this.state.draft = SPEC_TEMPLATE; this.paint(); },
            }),
          ]),
        ]),
      ]);
    },

    /** Save one provider's settings. */
    updateProvider(provider, patch) {
      return this.providerAction('PATCH', `/api/providers/${encodeURIComponent(provider.id)}`, patch,
        `${provider.name} updated`);
    },

    /** Send a provider change, then refresh this view and the shell. */
    async providerAction(method, path, body, message) {
      try {
        const result = await this.post(path, body === null ? {} : body, method);
        this.state.data.providers = result;
        this.state.data.appConfig = await this.get('/api/config');
        this.paint();
        if (window.dashboard) {
          await window.dashboard.reload();
          if (window.dashboard.usageReady && window.usageView) window.usageView.refresh();
          window.dashboard.toast(message, '', 'ok');
        }
        return true;
      } catch (error) {
        window.dashboard.toast('Could not save', error.message, 'error');
        return false;
      }
    },

    /* ---------------------------------------------------------- settings */

    /** The dashboard's own preferences. */
    settingsPanel() {
      const config = this.state.data.appConfig;
      const wrap = el('div');

      const toggles = [
        ['minimize_to_tray', 'Minimise to the system tray',
         'Closing the window hides it instead of quitting. Needs a tray to be available.'],
        ['auto_refresh', 'Watch the filesystem for changes',
         'Pick up new and growing sessions without pressing Refresh. Takes effect on restart.'],
        ['confirm_deletions', 'Confirm before moving anything to the trash',
         'Strongly recommended. The confirmation names every file.'],
      ];
      const list = el('div.cfg-list');
      for (const [key, label, help] of toggles) {
        list.append(el('label.cfg-row', {}, [
          el('input', {
            type: 'checkbox',
            ...(config[key] ? { checked: 'checked' } : {}),
            onchange: (event) => this.patch({ [key]: event.target.checked }),
          }),
          el('div', {}, [
            el('div', { text: label }),
            el('div.cfg-help', { text: help }),
          ]),
        ]));
      }

      const retention = el('div.cfg-row', {}, [
        el('input.cfg-number', {
          type: 'text', inputmode: 'numeric', value: String(config.trash_retention_days),
          onchange: (event) => {
            const days = Number(event.target.value);
            if (Number.isFinite(days) && days >= 0) this.patch({ trash_retention_days: days });
          },
        }),
        el('div', {}, [
          el('div', { text: 'Days to keep deleted sessions' }),
          el('div.cfg-help', { text: 'Zero disables purging entirely. Applied at launch.' }),
        ]),
      ]);

      const editor = el('div.cfg-row', {}, [
        el('input.cfg-text', {
          type: 'text', value: config.editor_command || '',
          onchange: (event) => this.patch({ editor_command: event.target.value }),
        }),
        el('div', {}, [
          el('div', { text: 'Editor command' }),
          el('div.cfg-help', { text: 'Used by the "Editor" button on a session. Usually "code".' }),
        ]),
      ]);

      const interval = el('div.cfg-row', {}, [
        el('input.cfg-number', {
          type: 'text', inputmode: 'numeric', 'aria-label': 'Refresh interval in seconds',
          value: String(config.refresh_interval_seconds || 5),
          onchange: (event) => {
            const seconds = Number(event.target.value);
            if (Number.isFinite(seconds) && seconds >= 1 && seconds <= 3600) {
              this.patch({ refresh_interval_seconds: seconds });
            } else {
              event.target.value = String(config.refresh_interval_seconds || 5);
              window.dashboard.toast('Use a number of seconds between 1 and 3600', '', 'warn');
            }
          },
        }),
        el('div', {}, [
          el('div', { text: 'Refresh interval (seconds)' }),
          el('div.cfg-help', { text: 'How often the window checks for new activity from any provider.' }),
        ]),
      ]);

      wrap.append(section('Behaviour', '', el('div', {}, [list, interval, retention, editor]), true));

      const trash = this.state.data.trash;
      wrap.append(section(
        'Trash',
        `${trash.total_files} file(s), ${bytes(trash.total_bytes)}`,
        el('div', {}, [
          el('p.cfg-help', { text: `Deleted sessions are moved to ${trash.trash_path}.` }),
          el('button.bordered', {
            text: 'Open the trash',
            onclick: () => window.dashboard && window.dashboard.showTrash(),
          }),
        ]),
      ));

      wrap.append(section(
        'Backup',
        'a ZIP of every transcript',
        el('div', {}, [
          el('p.cfg-help', {
            text: 'Copies every Claude Code transcript (its projects folder) into a single '
              + 'archive. Nothing in the tool\u2019s own data is modified.',
          }),
          el('button.primary', {
            text: 'Back up all transcripts…',
            onclick: () => this.runBackup(),
          }),
        ]),
      ));

      wrap.append(section(
        'Where things live',
        '',
        el('div.cfg-paths', {}, [
          ...this.state.data.providers.providers.map((p) => [p.name, p.detection.root]),
          ['Dashboard data', ((window.dashboard && window.dashboard.state.bootstrap) || { paths: {} }).paths.app_home || '~/.agentboard'],
        ].map(([label, value]) => el('div.cfg-pathrow', {}, [
          el('span.cfg-help', { text: label }),
          el('code', { text: value }),
        ]))),
      ));
      return wrap;
    },

    /* ----------------------------------------------------------- pricing */

    /**
     * Pricing: the global table, then each provider's own. A provider's
     * effective price for a model is its own override, else the global
     * table, else its built-in row; unknown models use its "default" row.
     */
    pricingPanel() {
      // Claude Code's prices are the global table itself, so it has no tab.
      const priced = this.state.data.providers.providers.filter(
        (p) => p.capabilities.cost && p.id !== 'claude');
      const wrap = el('div');
      const picker = el('div.segmented', { role: 'tablist', 'aria-label': 'Price table' });
      const options = [['', 'Global \u00b7 Claude Code']].concat(priced.map((p) => [p.id, p.name]));
      for (const [id, label] of options) {
        const provider = priced.find((p) => p.id === id);
        picker.append(el('button', {
          role: 'tab',
          'aria-selected': this.state.pricingProvider === id ? 'true' : 'false',
          class: this.state.pricingProvider === id ? 'on' : '',
          onclick: () => { this.state.pricingProvider = id; this.paint(); },
        }, [provider ? badge(provider, 'sm') : null, el('span', { text: ' ' + label })]));
      }
      wrap.append(el('div', { style: 'margin-bottom:12px' }, [picker]));
      const provider = priced.find((p) => p.id === this.state.pricingProvider);
      wrap.append(provider ? this.providerPricing(provider) : this.globalPricing());
      return wrap;
    },

    /** A provider's own price rows: built-in defaults plus its overrides. */
    providerPricing(provider) {
      const overrides = provider.settings.pricing || {};
      const defaults = provider.default_pricing || {};
      const models = Array.from(new Set([...Object.keys(defaults), ...Object.keys(overrides)]));
      // Only the counters this provider records: OpenAI and Google bill
      // cached reads but no cache writes.
      const spec = provider.spec || {};
      const writes = Boolean(spec.fields && spec.fields.cache_write_tokens);
      const columns = ['input', 'output']
        .concat(provider.capabilities.cache_tokens ? ['cache_read'] : [])
        .concat(writes ? ['cache_write_5m'] : []);
      const wrap = el('div');
      wrap.append(el('p.cfg-help', {
        style: 'margin:0 0 12px',
        text: `US dollars per million tokens, used only for local estimates of ${provider.name} sessions. `
          + 'Edited cells become overrides of the built-in price; "Reset" drops the override. '
          + (models.length ? '' : `${provider.name} uses the global table; add a model to price it separately.`),
      }));
      const save = (next) => this.updateProvider(provider, { pricing: next });
      const head = el('tr', {}, [el('th', { text: 'Model' }),
        ...columns.map((c) => el('th', { text: c.replace(/_/g, ' ') })), el('th')]);
      const rows = models.map((model) => {
        const effective = { ...(defaults[model] || {}), ...(overrides[model] || {}) };
        return el('tr', { class: overrides[model] ? 'overridden' : '' }, [
          el('td', { text: model }),
          ...columns.map((column) => el('td.num', {}, [el('input.cfg-price', {
            type: 'text', inputmode: 'decimal', 'aria-label': `${model} ${column.replace(/_/g, ' ')}`,
            value: effective[column] === undefined ? '' : String(effective[column]),
            onchange: (event) => {
              const value = Number(event.target.value);
              if (!Number.isFinite(value) || value < 0) {
                event.target.value = effective[column] === undefined ? '' : String(effective[column]);
                return;
              }
              save({ ...overrides, [model]: { ...effective, [column]: value } });
            },
          })])),
          el('td', {}, [overrides[model] ? el('button.bordered', {
            text: 'Reset',
            title: defaults[model] ? 'Go back to the built-in price' : 'Remove this row',
            onclick: () => {
              const next = { ...overrides };
              delete next[model];
              save(next);
            },
          }) : null]),
        ]);
      });
      wrap.append(el('div.chart-table-wrap.pricing-wrap', {}, [
        el('table.chart-table.pricing-table', {}, [el('thead', {}, [head]), el('tbody', {}, rows)]),
      ]));
      const name = el('input', { type: 'text', placeholder: 'model id, e.g. gpt-5.1', 'aria-label': 'New model id' });
      wrap.append(el('div.add-model', {}, [
        name,
        el('button.bordered', {
          text: 'Add model',
          onclick: () => {
            const model = name.value.trim();
            if (!model) return;
            const base = defaults.default || { input: 0, output: 0 };
            save({ ...overrides, [model]: { ...base } });
          },
        }),
      ]));
      return wrap;
    },

    /** The global price table, shared by every provider. */
    globalPricing() {
      const pricing = this.state.data.appConfig.pricing || {};
      const wrap = el('div');
      wrap.append(el('p.cfg-help', {
        style: 'margin:0 0 12px',
        text: 'US dollars per million tokens, used only for local estimates, never billing data. '
          + 'This table holds Claude Code\u2019s models and applies to every provider, under each '
          + 'provider\u2019s own overrides. Cache writes are charged by time-to-live: the one-hour tier '
          + 'costs about twice the input rate.',
      }));

      const columns = ['input', 'output', 'cache_write_5m', 'cache_write_1h', 'cache_read'];
      const head = el('tr', {}, [
        el('th', { text: 'Model' }),
        ...columns.map((c) => el('th', { text: c.replace(/_/g, ' ') })),
      ]);
      const rows = Object.entries(pricing).map(([model, row]) => el('tr', {}, [
        el('td', { text: model }),
        ...columns.map((column) => el('td.num', {}, [
          el('input.cfg-price', {
            type: 'text', inputmode: 'decimal',
            value: row[column] === undefined ? '' : String(row[column]),
            onchange: (event) => {
              const value = Number(event.target.value);
              if (!Number.isFinite(value) || value < 0) {
                event.target.value = row[column] === undefined ? '' : String(row[column]);
                return;
              }
              this.patch({ pricing: { [model]: { [column]: value } } }, 'Pricing updated');
            },
          }),
        ])),
      ]));

      wrap.append(el('div.chart-table-wrap.pricing-wrap', {}, [
        el('table.chart-table.pricing-table', {},
          [el('thead', {}, [head]), el('tbody', {}, rows)]),
      ]));
      wrap.append(el('p.cfg-help', {
        style: 'margin-top:10px',
        text: 'Claude Code models with no row here fall back to the "default" row; other providers '
          + 'fall back to their own default. Editing a price recomputes every estimate immediately.',
      }));
      return wrap;
    },

    /* ------------------------------------------------------ Claude config */

    /** Read-only view of Claude Code's own configuration. */
    claudePanel() {
      const data = this.state.data.claudeConfig;
      const wrap = el('div');

      wrap.append(section(
        'settings.json',
        data.settings_path,
        data.settings ? jsonBlock(data.settings)
          : emptyState('No settings file was found at', data.settings_path),
        true,
      ));

      const servers = data.mcp_servers || [];
      wrap.append(section(
        'MCP servers',
        `${servers.length} configured`,
        servers.length
          ? el('div', {}, servers.map((entry) => el('div.cfg-mcp', {}, [
              el('div', {}, [
                el('strong', { text: entry.name }),
                el('span.cfg-help', { text: ' in ' + entry.project }),
              ]),
              jsonBlock(entry.definition),
            ])))
          : emptyState('No MCP servers are configured for any project.'),
      ));

      const permissions = data.settings && data.settings.permissions;
      wrap.append(section(
        'Permissions',
        permissions ? '' : 'none set',
        permissions ? jsonBlock(permissions)
          : emptyState('No permission rules are set in settings.json.'),
      ));

      const hooks = data.settings && data.settings.hooks;
      wrap.append(section(
        'Hooks',
        hooks ? '' : 'none set',
        hooks ? jsonBlock(hooks) : emptyState('No hooks are configured in settings.json.'),
      ));

      const projects = data.projects || {};
      wrap.append(section(
        'Per-project state',
        `${Object.keys(projects).length} project(s) in ~/.claude.json`,
        Object.keys(projects).length
          ? el('div', {}, Object.entries(projects).map(([path, entry]) =>
              section(path, '', jsonBlock(entry))))
          : emptyState('No per-project state recorded.'),
      ));

      wrap.append(section(
        'Global state',
        data.global_path,
        el('div', {}, [
          el('p.cfg-help', {
            text: 'Identity fields and the large feature-flag caches are hidden: '
              + (data.redacted_keys || []).join(', '),
          }),
          data.global ? jsonBlock(data.global) : emptyState('Not readable.'),
        ]),
      ));

      if ((data.errors || []).length) {
        wrap.append(section('Read errors', '', jsonBlock(data.errors), true));
      }
      return wrap;
    },

    /* ------------------------------------------------------ instructions */

    /** The CLAUDE.md manager, with editing and backups. */
    instructionsPanel() {
      const data = this.state.data.claudeMd;
      const wrap = el('div');

      if (!data.files.length) {
        wrap.append(emptyState(
          'No CLAUDE.md files were found, either globally or in any project you have '
          + 'worked in. Create one at the path below and it will appear here.',
          data.global_path,
        ));
      } else {
        const list = el('div.cfg-list');
        for (const file of data.files) {
          list.append(el('div.cfg-file', {}, [
            el('div', { style: 'flex:1;min-width:0' }, [
              el('div', {}, [
                el('strong', { text: file.scope === 'global' ? 'Global' : file.project }),
                el('span.cfg-help', { text: `  ${file.name} · ${bytes(file.size)}` }),
              ]),
              el('code.truncate', { text: file.path, style: 'display:block' }),
            ]),
            el('button.bordered', {
              text: 'Edit',
              onclick: () => this.editInstructions(file.path),
            }),
          ]));
        }
        wrap.append(section('Instruction files', `${data.files.length} found`, list, true));
      }

      const backups = data.backups || [];
      wrap.append(section(
        'Backups',
        `${backups.length} taken before saves`,
        backups.length
          ? el('div.cfg-list', {}, backups.map((entry) => el('div.cfg-file', {}, [
              el('div', { style: 'flex:1;min-width:0' }, [
                el('div', { text: entry.original_name }),
                el('code.truncate', { text: entry.path, style: 'display:block' }),
              ]),
              el('span.cfg-help', { text: entry.taken_at }),
            ])))
          : emptyState('No edits have been made yet, so there are no backups.'),
      ));
      return wrap;
    },

    /** Open an instruction file in a modal editor. */
    async editInstructions(path) {
      let file;
      try {
        file = await this.get(`/api/claude-md/read?path=${encodeURIComponent(path)}`);
      } catch (error) {
        window.dashboard.toast('Could not read the file', error.message, 'error');
        return;
      }

      const area = el('textarea.cfg-editor', { spellcheck: 'false' });
      area.value = file.content;
      const status = el('div.cfg-help', {
        text: `${file.size} bytes. A timestamped backup is taken before every save.`,
      });

      const body = el('div', {}, [
        el('code.truncate', { text: path, style: 'display:block;margin-bottom:8px' }),
        area,
        el('div', { style: 'display:flex;gap:7px;align-items:center;margin-top:9px' }, [
          el('button.primary', {
            text: 'Save',
            onclick: async () => {
              try {
                const result = await this.post('/api/claude-md/save',
                                               { path, content: area.value });
                window.dashboard.toast(
                  'Saved',
                  result.backup ? `Backed up to ${result.backup}` : 'New file created',
                  'ok',
                );
                window.dashboard.closeModal();
                this.render(this.host);
              } catch (error) {
                window.dashboard.toast('Could not save', error.message, 'error');
              }
            },
          }),
          el('button.bordered', { text: 'Cancel', onclick: () => window.dashboard.closeModal() }),
          status,
        ]),
      ]);
      window.dashboard.openModal('Edit ' + path.split('/').pop(), body);
    },

    /* ----------------------------------------------------------- assets */

    /** The skills, commands and agents browser with a preview. */
    assetsPanel() {
      const data = this.state.data.assets;
      const wrap = el('div');

      if (!data.assets.length) {
        wrap.append(emptyState(
          'No skills, commands or agents were found. The dashboard looked in:',
          data.searched.join('\n'),
        ));
        return wrap;
      }

      const kinds = Object.entries(data.counts);
      const filters = el('div.cfg-filters', {}, [
        el('input.cfg-text', {
          type: 'search', placeholder: 'Filter by name or description',
          value: this.state.assetFilter,
          oninput: (event) => {
            this.state.assetFilter = event.target.value.toLowerCase();
            this.renderAssetList();
          },
        }),
        ...kinds.map(([kind, count]) => el('button', {
          text: `${kind} (${count})`,
          class: this.state.assetKind === kind ? 'bordered active' : 'bordered',
          onclick: () => {
            this.state.assetKind = this.state.assetKind === kind ? '' : kind;
            this.paint();
          },
        })),
      ]);

      wrap.append(filters);
      this.assetListNode = el('div.cfg-list');
      wrap.append(this.assetListNode);
      // Defer so the node is in the tree before it is filled.
      setTimeout(() => this.renderAssetList(), 0);
      return wrap;
    },

    /** Fill the asset list against the current filter. */
    renderAssetList() {
      if (!this.assetListNode) return;
      const { assets } = this.state.data.assets;
      const needle = this.state.assetFilter;
      const kind = this.state.assetKind;
      const matches = assets.filter((asset) => {
        if (kind && asset.kind !== kind) return false;
        if (!needle) return true;
        return (asset.name + ' ' + asset.description + ' ' + asset.source)
          .toLowerCase().includes(needle);
      });

      if (!matches.length) {
        this.assetListNode.replaceChildren(emptyState('Nothing matches that filter.'));
        return;
      }
      this.assetListNode.replaceChildren(...matches.slice(0, 300).map((asset) =>
        el('div.cfg-file', {
          onclick: () => this.previewAsset(asset),
          style: 'cursor:pointer',
        }, [
          el('span.pill', { text: asset.kind }),
          el('div', { style: 'flex:1;min-width:0' }, [
            el('div', { text: asset.name, style: 'font-weight:600' }),
            el('div.cfg-help.truncate', { text: asset.description || asset.path }),
          ]),
          el('span.cfg-help', { text: asset.source }),
        ])));
    },

    /** Show one asset definition in a modal. */
    async previewAsset(asset) {
      let file;
      try {
        file = await this.get(`/api/assets/read?path=${encodeURIComponent(asset.path)}`);
      } catch (error) {
        window.dashboard.toast('Could not read it', error.message, 'error');
        return;
      }
      const body = el('div', {}, [
        el('code.truncate', { text: asset.path, style: 'display:block;margin-bottom:8px' }),
        file.truncated
          ? el('p.cfg-help', { text: `Showing the first ${bytes(file.content.length)} of ${bytes(file.size)}.` })
          : null,
        el('div.v-markdown.cfg-preview'),
      ]);
      body.querySelector('.cfg-preview').innerHTML = window.md.render(file.content);
      window.dashboard.openModal(asset.name, body);
    },

    /* ------------------------------------------------------------ todos */

    /** Task lists from ~/.claude/todos. */
    todosPanel() {
      const data = this.state.data.todos;
      const wrap = el('div');

      if (!data.exists) {
        wrap.append(emptyState(
          'Claude Code has not created a todos directory on this machine. '
          + 'It appears once a session uses the task list.',
          data.path,
        ));
        return wrap;
      }
      if (!data.lists.length) {
        wrap.append(emptyState('The todos directory is empty.', data.path));
        return wrap;
      }

      for (const list of data.lists) {
        const items = el('div.todo-items');
        for (const item of list.items) {
          items.append(el('div.todo-item', { 'data-status': item.status }, [
            el('span.todo-mark', {
              text: item.status === 'completed' ? '✓'
                : item.status === 'in_progress' ? '▸' : '○',
            }),
            el('span', { text: item.content }),
          ]));
        }
        const done = list.counts.completed || 0;
        wrap.append(section(
          list.session_title || list.session_id || list.file,
          `${done}/${list.items.length} done`,
          el('div', {}, [
            list.error ? el('p.cfg-help', { text: 'Could not parse: ' + list.error }) : null,
            items,
            list.session_id
              ? el('button.bordered', {
                  text: 'Open the session',
                  style: 'margin-top:8px',
                  onclick: () => window.dashboard.openSessionById(list.session_id),
                })
              : null,
          ]),
          data.lists.length <= 3,
        ));
      }
      return wrap;
    },

    /* ----------------------------------------------------------- actions */

    /** Patch the dashboard config and refresh what depends on it. */
    async patch(body, message) {
      try {
        const updated = await this.post('/api/config', body, 'PATCH');
        this.state.data.appConfig = updated;
        if (window.dashboard) {
          window.dashboard.state.bootstrap.config = updated;
          if (window.dashboard.usageReady && window.usageView) window.usageView.refresh();
          window.dashboard.toast(message || 'Setting saved', '', 'ok');
        }
      } catch (error) {
        window.dashboard.toast('Could not save the setting', error.message, 'error');
      }
    },

    /** Send a JSON body. */
    async post(path, body, method = 'POST') {
      const response = await fetch(path, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.detail || response.statusText);
      return payload;
    },

    /** Ask for a folder, then write the ZIP backup there. */
    async runBackup() {
      let destination = null;
      if (window.pywebview && window.pywebview.api && window.pywebview.api.choose_folder) {
        const chosen = await window.pywebview.api.choose_folder();
        if (chosen.cancelled) return;
        if (!chosen.chosen) {
          window.dashboard.toast('Could not open the folder picker', chosen.error || '', 'error');
          return;
        }
        destination = chosen.path;
      } else {
        destination = window.prompt('Directory to write the backup into:');
        if (!destination) return;
      }

      window.dashboard.setStatus('busy', 'Backing up');
      try {
        const result = await this.post('/api/backup', { destination });
        window.dashboard.toast(
          'Backup written',
          `${result.files} files, ${bytes(result.archive_bytes)} — ${result.path}`,
          'ok',
        );
      } catch (error) {
        window.dashboard.toast('Backup failed', error.message, 'error');
      } finally {
        window.dashboard.setStatus('ok', 'Ready');
      }
    },
  };

  window.configView = configView;
}());
