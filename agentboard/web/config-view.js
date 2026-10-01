/* The Config view: settings, pricing, instruction files, assets and todos.
 *
 * Everything Claude Code owns is shown read-only. The only writes this
 * view can perform are the two the brief allows: editing a CLAUDE.md
 * (with an automatic timestamped backup) and changing the dashboard's own
 * configuration, which lives in ~/.agentboard and never touches
 * ~/.claude.
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

  const configView = {
    state: { tab: 'settings', data: {}, assetFilter: '', assetKind: '' },

    /** Render the view, fetching everything it needs once. */
    async render(host) {
      this.host = host;
      host.replaceChildren(el('div.placeholder', {}, [el('span.spinner')]));
      try {
        const [claudeConfig, appConfig, claudeMd, assets, todos, trash] = await Promise.all([
          this.get('/api/claude-config'),
          this.get('/api/config'),
          this.get('/api/claude-md'),
          this.get('/api/assets'),
          this.get('/api/todos'),
          this.get('/api/trash'),
        ]);
        this.state.data = { claudeConfig, appConfig, claudeMd, assets, todos, trash };
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
      const tabs = el('div.cfg-tabs');
      const counts = this.state.data.assets.counts || {};
      const entries = [
        ['settings', 'Dashboard settings'],
        ['pricing', 'Pricing table'],
        ['claude', 'Claude Code config'],
        ['instructions', `CLAUDE.md (${this.state.data.claudeMd.files.length})`],
        ['assets', `Skills & commands (${Object.values(counts).reduce((a, b) => a + b, 0)})`],
        ['todos', `Todos (${this.state.data.todos.total})`],
      ];
      for (const [key, label] of entries) {
        tabs.append(el('button', {
          text: label,
          class: this.state.tab === key ? 'on' : '',
          onclick: () => { this.state.tab = key; this.paint(); },
        }));
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
        default: return this.settingsPanel();
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

      const resume = el('div.cfg-row', {}, [
        el('input.cfg-text', {
          type: 'text', value: (config.terminal || {}).resume_command || '',
          onchange: (event) => this.patch({ terminal: { resume_command: event.target.value } }),
        }),
        el('div', {}, [
          el('div', { text: 'Resume command' }),
          el('div.cfg-help', { text: '{session_id} is substituted. Runs in a new terminal.' }),
        ]),
      ]);

      wrap.append(section('Behaviour', '', el('div', {}, [list, retention, editor, resume]), true));

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
            text: 'Copies the whole ~/.claude/projects tree into a single archive. '
              + 'Nothing in ~/.claude is modified.',
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
          ['Claude Code data', this.state.data.claudeConfig.settings_path.replace(/settings\.json$/, '')],
          ['Global config', this.state.data.claudeConfig.global_path],
          ['Dashboard config', 'see the About dialog'],
        ].map(([label, value]) => el('div.cfg-pathrow', {}, [
          el('span.cfg-help', { text: label }),
          el('code', { text: value }),
        ]))),
      ));
      return wrap;
    },

    /* ----------------------------------------------------------- pricing */

    /** The editable price table, with a clear caveat. */
    pricingPanel() {
      const pricing = this.state.data.appConfig.pricing || {};
      const wrap = el('div');
      wrap.append(el('p.cfg-help', {
        style: 'margin:0 0 12px',
        text: 'Prices are US dollars per million tokens and are used only for the local '
          + 'estimates in this app. They are not billing data. Cache writes are charged by '
          + 'time-to-live: the one-hour tier costs about twice the input rate.',
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

      wrap.append(el('div.chart-table-wrap', {}, [
        el('table.chart-table.pricing-table', {},
          [el('thead', {}, [head]), el('tbody', {}, rows)]),
      ]));
      wrap.append(el('p.cfg-help', {
        style: 'margin-top:10px',
        text: 'A model with no row here falls back to the "default" row. Editing a price '
          + 'recomputes every estimate in the app immediately.',
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
