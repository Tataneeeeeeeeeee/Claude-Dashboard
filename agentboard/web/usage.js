/* The usage dashboard view.
 *
 * Composition follows the data-viz rules: one filter row above everything
 * it scopes, exactly one hero figure, a KPI row of stat tiles, then the
 * charts. Two measures of different scale never share an axis, so tokens
 * and cost are separate cards rather than a dual-axis chart.
 *
 * Every card carries a table twin, toggled globally, because two of the
 * light-mode categorical slots sit below 3:1 against the surface and the
 * relief rule requires values to be reachable without colour.
 *
 * Exposes `window.usageView`.
 */
'use strict';

(function () {
  const $ = (selector, root = document) => root.querySelector(selector);

  /** Build an HTML element. */
  function el(tag, attrs = {}, children = []) {
    const [name, ...classes] = tag.split('.');
    const node = document.createElement(name);
    if (classes.length) node.className = classes.join(' ');
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === 'text') node.textContent = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(), value);
      else node.setAttribute(key, String(value));
    }
    for (const child of [].concat(children)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /** Read a themed colour token, so marks follow the validated palette. */
  function token(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  /**
   * The token classes, in fixed slot order. Never cycled: a provider that
   * records no cache writes drops that band, but the others keep their
   * colours.
   */
  function tokenSeries(caps) {
    const all = [
      { key: 'input', label: 'Input', color: token('--series-1') },
      { key: 'output', label: 'Output', color: token('--series-2') },
      { key: 'cache_write', label: 'Cache write', color: token('--series-3'), cache: true },
      { key: 'cache_read', label: 'Cache read', color: token('--series-4'), cache: true },
    ];
    return caps && caps.cache_tokens === false ? all.filter((s) => !s.cache) : all;
  }

  /** Provider descriptors, kept current by the shell. */
  function providerList(fallback) {
    const boot = window.dashboard && window.dashboard.state && window.dashboard.state.bootstrap;
    return (boot && boot.providers) || fallback || [];
  }

  /** A provider badge, drawn the same way as in the shell. */
  function badge(provider, size = 'sm') {
    return el(`span.provider-badge.${size}`, {
      style: `--p-color:${provider.color}`, title: provider.name,
      role: 'img', 'aria-label': provider.name, text: provider.monogram || '?',
    });
  }

  /** A card body for a feature this provider's data does not record. */
  function unsupported(provider, feature) {
    return el('div.chart-empty.unsupported', {}, [
      el('strong', { text: 'Not supported' }),
      el('span', { text: ` \u2014 ${provider.name} does not record ${feature}.` }),
    ]);
  }

  /** Shorten a project path to its last two segments. */
  function shortProject(path) {
    const parts = String(path || '').split('/').filter(Boolean);
    return parts.slice(-2).join('/') || path || 'unknown';
  }

  /** Percentage change rendered as a signed delta against a named period. */
  function deltaNode(percent, period) {
    if (percent === null || percent === undefined) {
      return el('span.kpi-delta.flat', { text: `no ${period} data` });
    }
    const rounded = Math.round(percent * 10) / 10;
    if (Math.abs(rounded) < 0.05) {
      return el('span.kpi-delta.flat', { text: `level vs ${period}` });
    }
    // These tiles report spend, where "up" is the unfavourable direction.
    // Colour follows direction x whether up is good, so a rise reads as
    // the warning colour; the arrow states the direction either way.
    const up = rounded > 0;
    return el('span.kpi-delta', {
      class: `kpi-delta ${up ? 'down' : 'up'}`,
      text: `${up ? '↑' : '↓'} ${Math.abs(rounded)}% vs ${period}`,
    });
  }

  const usageView = {
    state: {
      granularity: 'day',
      project: '',
      provider: '',
      metric: 'tokens',
      tables: false,
      data: null,
      loading: false,
    },

    /** Render the whole view into its host element. */
    async render(host, options = {}) {
      this.host = host;
      if (options.projects) this.projects = options.projects;
      if (options.providers) this.providers = options.providers;
      if (options.provider !== undefined) this.state.provider = options.provider;
      // The server inlines the aggregate for a `?view=usage` deep link.
      const preloaded = window.__PRELOAD__ && window.__PRELOAD__.usage;
      const preloadedCompare = window.__PRELOAD__ && window.__PRELOAD__.compare;
      if (preloaded && !this.state.data && !this.state.provider
          && (preloadedCompare || !this.wantsComparison())) {
        this.state.data = preloaded;
        this.state.compare = preloadedCompare || null;
        this.paint();
        return;
      }
      await this.refresh();
    },

    /** The comparison applies when every provider is in view and two or
     *  more of them have sessions. */
    wantsComparison() {
      if (this.state.provider) return false;
      return providerList(this.providers).filter((p) => p.enabled && p.stats.sessions).length >= 2;
    },

    /** Scope the dashboard to one provider ('' for all) and refetch. */
    setProvider(provider) {
      this.state.provider = provider || '';
      this.state.project = '';
      this.refresh();
    },

    /** Fetch a fresh aggregate and repaint. */
    async refresh() {
      if (!this.host) return;
      const page = $('.usage-page', this.host);
      // Hold the previous render at reduced opacity instead of flashing.
      if (page) page.classList.add('stale');
      else this.host.replaceChildren(el('div.placeholder', {}, [el('span.spinner')]));

      try {
        // The comparison is only meaningful with every provider in view.
        this.state.compare = null;
        if (this.wantsComparison()) {
          const compareParams = new URLSearchParams({ granularity: this.state.granularity });
          if (this.state.project) compareParams.set('project', this.state.project);
          const compare = await fetch(`/api/usage/compare?${compareParams}`, {
            headers: { Accept: 'application/json' },
          });
          if (compare.ok) this.state.compare = await compare.json();
        }
        const params = new URLSearchParams({ granularity: this.state.granularity });
        if (this.state.project) params.set('project', this.state.project);
        if (this.state.provider) params.set('provider', this.state.provider);
        const response = await fetch(`/api/usage?${params}`, {
          headers: { Accept: 'application/json' },
        });
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `${response.status} ${response.statusText}`);
        }
        this.state.data = await response.json();
        this.paint();
      } catch (error) {
        this.host.replaceChildren(el('div.placeholder', {}, [
          el('h2', { text: 'Could not load usage data' }),
          el('p', { text: error.message }),
        ]));
      }
    },

    /**
     * Width a full-width card's plot area occupies, measured from the
     * host rather than guessed, so marks render at their literal sizes.
     */
    plotWidth(columns = 1) {
      const available = (this.host ? this.host.clientWidth : 0) || 1200;
      // Below 900px the grid collapses to one column (see app.css).
      if (available <= 900) columns = 1;
      const page = Math.min(1180, available) - 56;          // page padding
      const column = columns === 1 ? page : (page - 14) / 2; // grid gap
      return Math.max(320, Math.round(column - 40));         // card padding
    },

    /** Build the DOM for the current data. */
    paint() {
      const data = this.state.data;
      const page = el('div.usage-page');
      page.append(this.filters());

      if (!data.totals.sessions) {
        page.append(el('div.placeholder', {}, [
          el('h2', { text: 'Nothing to chart yet' }),
          el('p', { text: 'Once an AI assistant has recorded a session, its usage appears here.' }),
        ]));
        this.host.replaceChildren(page);
        return;
      }

      if (this.state.compare && this.state.compare.providers.length >= 2) {
        page.append(this.comparison());
      }
      const provider = this.currentProvider();
      if (provider && provider.capabilities.usage === false) {
        page.append(el('div.unsupported-banner', {}, [
          badge(provider, 'md'),
          el('span', {
            text: `${provider.name} does not record token usage, so cost and token charts are `
              + 'not available. Sessions, messages and activity are shown below.',
          }),
        ]));
      } else {
        page.append(...this.hero());
      }
      page.append(this.kpis());
      page.append(...this.splitCards(this.cards()));
      this.host.replaceChildren(page);
    },

    /**
     * Keep the two headline charts in view and fold the rest under a
     * "More charts" disclosure, so the page tells a story before it shows
     * every number. The disclosure remembers being opened.
     */
    splitCards(grid) {
      const extra = Array.from(grid.children).slice(2);
      if (!extra.length) return [grid];
      const more = el('div.chart-grid');
      more.append(...extra);
      const details = el('details.usage-more', {}, [
        el('summary', {}, [
          el('span', { text: 'More charts' }),
          el('span.usage-more-hint', { text: `${extra.length} more · tools, projects, models, activity` }),
        ]),
        more,
      ]);
      if (this.state.moreOpen) details.setAttribute('open', 'open');
      details.addEventListener('toggle', () => { this.state.moreOpen = details.open; });
      return [grid, details];
    },

    /* ------------------------------------------------------------ filters */

    /** The single filter row that scopes every card below it. */
    filters() {
      const granularity = el('div.segmented');
      for (const [value, label] of [['day', 'Day'], ['week', 'Week'], ['month', 'Month']]) {
        granularity.append(el('button', {
          text: label,
          class: this.state.granularity === value ? 'on' : '',
          onclick: () => { this.state.granularity = value; this.refresh(); },
        }));
      }

      const project = el('select', {
        'aria-label': 'Limit to one project',
        onchange: (event) => { this.state.project = event.target.value; this.refresh(); },
      }, [el('option', { value: '', text: 'All projects' })]);
      for (const entry of this.projects || []) {
        const value = entry.key || entry.dir_name;
        const option = el('option', { value, text: entry.name });
        if (value === this.state.project) option.setAttribute('selected', 'selected');
        project.append(option);
      }

      return el('div.usage-filters', {}, [
        granularity,
        project,
        el('span.spacer'),
        el('button', {
          text: this.state.tables ? 'Hide tables' : 'Show tables',
          class: this.state.tables ? 'bordered active' : 'bordered',
          title: 'Show the numbers behind every chart',
          onclick: () => { this.state.tables = !this.state.tables; this.paint(); },
        }),
        el('button.bordered', {
          text: 'Export CSV',
          title: 'Save the aggregated statistics',
          onclick: () => window.dashboard && window.dashboard.exportCurrent('csv'),
        }),
      ]);
    },

    /** The selected provider's descriptor, or null for "all". */
    currentProvider() {
      if (!this.state.provider) return null;
      return providerList(this.providers).find((p) => p.id === this.state.provider) || null;
    },

    /* --------------------------------------------------------- comparison */

    /**
     * The all-providers overview: one tile per provider, then the same
     * measures side by side over time. Each provider keeps its own accent
     * as its series colour, and every chart has a legend, labels and a
     * table, so identity never rests on colour alone.
     */
    comparison() {
      const compare = this.state.compare;
      const known = providerList(this.providers);
      const describe = (id) => known.find((p) => p.id === id)
        || { id, name: id, monogram: '?', color: '#8a8f98', capabilities: {} };
      // A fixed order (the registry's), so tiles and legends never reshuffle
      // when the numbers change.
      const order = (id) => {
        const index = known.findIndex((p) => p.id === id);
        return index < 0 ? known.length : index;
      };
      const rows = compare.providers.slice().sort((a, b) => order(a.provider) - order(b.provider));
      const ids = rows.map((r) => r.provider);
      const series = ids.map((id) => ({ key: id, label: describe(id).name, color: describe(id).color }));
      const unit = { day: 'day', week: 'ISO week', month: 'month' }[this.state.granularity];
      const showTables = this.state.tables;
      const money = window.charts.money;
      const compact = window.charts.compact;

      const section = el('section.compare', { 'aria-labelledby': 'compare-title' });
      section.append(el('div.section-head', {}, [
        el('h2', { id: 'compare-title', text: 'Providers compared' }),
        el('span.card-sub', {
          text: `${rows.length} providers \u00b7 click one to focus on it`,
        }),
      ]));

      // 1. One tile per provider.
      const tiles = el('div.provider-tiles');
      for (const row of rows) {
        const provider = describe(row.provider);
        const caps = provider.capabilities || {};
        const costKnown = caps.cost !== false && caps.usage !== false;
        tiles.append(el('button.provider-tile', {
          style: `--p-color:${provider.color}`,
          title: `Show only ${provider.name}`,
          onclick: () => window.dashboard && window.dashboard.setProvider(row.provider),
        }, [
          el('div.provider-tile-head', {}, [badge(provider, 'md'), el('span', { text: provider.name })]),
          el('div.provider-tile-value', { text: costKnown ? money(row.cost) : '\u2014' }),
          el('div.provider-tile-label', { text: costKnown ? 'estimated cost' : 'cost not tracked' }),
          el('dl.provider-tile-stats', {}, [
            ['Sessions', String(row.sessions)],
            ['Messages', compact(row.messages)],
            ['Tokens', caps.usage === false ? '\u2014' : compact(row.total_tokens)],
            ['Tool calls', caps.tool_calls === false ? '\u2014' : compact(row.tool_calls)],
            ['Active days', String(row.active_days)],
            ['Per session', costKnown ? money(row.avg_cost) : `${row.avg_messages} msg`],
          ].flatMap(([label, value]) => [el('dt', { text: label }), el('dd.num', { text: value })])),
        ]));
      }
      section.append(tiles);

      const card = (title, subtitle, body, tableNode, wide) => {
        const node = el('div.card' + (wide ? '.wide' : ''), {}, [
          el('div.card-head', {}, [
            el('span.card-title', { text: title }),
            el('span.card-sub', { text: subtitle }),
          ]),
          body,
        ]);
        if (showTables && tableNode) node.append(tableNode);
        return node;
      };

      // Re-shape the aligned per-provider series into one row per period.
      const periodRows = (field) => compare.periods.map((period, index) => {
        const row = { period };
        for (const id of ids) row[id] = (compare.timeseries[id][index] || {})[field] || 0;
        return row;
      });
      const periodTable = (field, format) => window.charts.table(
        ['Period', ...series.map((s) => s.label)],
        periodRows(field).map((row) => [row.period, ...ids.map((id) => format(row[id]))]),
      );

      const grid = el('div.chart-grid');
      grid.append(card(
        'Sessions by provider', `by ${unit}, stacked`,
        window.charts.columnChart({
          rows: periodRows('sessions'), xKey: 'period', series,
          format: compact, height: 220, width: this.plotWidth(2),
        }),
        periodTable('sessions', String),
      ));
      grid.append(card(
        'Estimated cost by provider', `by ${unit}, stacked, from each provider's pricing`,
        window.charts.columnChart({
          rows: periodRows('cost'), xKey: 'period', series,
          format: money, height: 220, width: this.plotWidth(2),
        }),
        periodTable('cost', money),
      ));
      grid.append(card(
        'Share of tokens', 'every token billed, per provider',
        window.charts.shareBar(
          rows.map((r) => ({ label: describe(r.provider).name, value: r.total_tokens, color: describe(r.provider).color })),
          window.charts.exact,
        ),
        window.charts.table(
          ['Provider', 'Input', 'Output', 'Cache read', 'Cache write', 'Total'],
          rows.map((r) => [describe(r.provider).name, window.charts.exact(r.input),
            window.charts.exact(r.output), window.charts.exact(r.cache_read),
            window.charts.exact(r.cache_write), window.charts.exact(r.total_tokens)]),
        ),
      ));
      grid.append(card(
        'Average cost per session', 'estimated, per provider',
        window.charts.barChart({
          rows: rows.map((r) => ({
            label: describe(r.provider).name, value: r.avg_cost, color: describe(r.provider).color,
            extra: [
              { label: 'Messages / session', value: String(r.avg_messages) },
              { label: 'Tokens / session', value: compact(r.avg_tokens) },
            ],
          })),
          format: money, label: 'Per session',
        }),
        window.charts.table(
          ['Provider', 'Sessions', 'Avg cost', 'Avg messages', 'Avg tokens', 'Avg duration'],
          rows.map((r) => [describe(r.provider).name, r.sessions, money(r.avg_cost),
            r.avg_messages, compact(r.avg_tokens), `${Math.round(r.avg_duration_seconds / 60)} min`]),
        ),
      ));
      section.append(grid);
      section.append(el('div.section-head', {}, [
        el('h2', { text: 'All providers combined' }),
        el('span.card-sub', { text: 'every chart below sums the providers above' }),
      ]));
      return section;
    },

    /* --------------------------------------------------------------- hero */

    /** The one number the dashboard leads with, plus its caveat. */
    hero() {
      const totals = this.state.data.totals;
      const reported = totals.reported_cost;
      return [
        el('div.hero', {}, [
          el('span.hero-value', { text: window.charts.money(totals.estimated_cost) }),
          el('span.hero-label', {
            text: this.state.project ? 'estimated for this project'
              : this.currentProvider() ? `estimated across ${this.currentProvider().name} sessions`
                : 'estimated across all sessions',
          }),
        ]),
        el('div.hero-note', {
          text: 'A local estimate from your own pricing tables, not a bill. '
            + (reported
              ? `The tools themselves recorded ${window.charts.money(reported)} for the `
                + `${totals.sessions_with_reported_cost} sessions that stored a cost. `
              : '')
            + 'Transcripts omit background calls, so the estimate is a lower bound.',
        }),
      ];
    },

    /* ---------------------------------------------------------------- KPIs */

    /** Four stat tiles, each with a delta against the preceding period. */
    kpis() {
      const { kpis, timeseries } = this.state.data;
      const row = el('div.kpi-row');
      const periodName = { 1: 'yesterday', 7: 'prior 7 days', 30: 'prior 30 days' };
      const costTrend = timeseries.map((point) => point.cost);

      for (const card of kpis) {
        const change = card.change_percent ? card.change_percent.cost : undefined;
        row.append(el('div.kpi', {}, [
          el('div.kpi-head', {}, [
            el('span.kpi-label', { text: card.label }),
            card.days ? null : window.charts.sparkline(costTrend, token('--accent')),
          ]),
          el('div.kpi-value', { text: window.charts.money(card.cost) }),
          el('div.kpi-sub', {
            text: `${window.charts.compact(card.total_tokens)} tokens · `
              + `${card.messages} messages · ${card.sessions} sessions`,
          }),
          card.days
            ? el('div', { style: 'margin-top:4px' },
                [deltaNode(change, periodName[card.days] || 'the prior period')])
            : null,
        ]));
      }
      return row;
    },

    /* --------------------------------------------------------------- cards */

    /** Every chart card, each with its table twin. */
    cards() {
      const data = this.state.data;
      const grid = el('div.chart-grid');
      const provider = this.currentProvider();
      const caps = provider ? provider.capabilities : {};
      const series = tokenSeries(caps);
      const showTables = this.state.tables;
      // A feature the selected provider does not record gets an explicit
      // "not supported" body instead of an empty or zero chart.
      const gate = (supported, feature, build) => (supported === false && provider
        ? unsupported(provider, feature) : build());
      const unit = { day: 'day', week: 'ISO week', month: 'month' }[this.state.granularity];

      /** Wrap a chart in a titled card, adding its table when enabled. */
      const card = (title, subtitle, body, tableNode, wide) => {
        const node = el('div.card' + (wide ? '.wide' : ''), {}, [
          el('div.card-head', {}, [
            el('span.card-title', { text: title }),
            el('span.card-sub', { text: subtitle }),
          ]),
          body,
        ]);
        if (showTables && tableNode) node.append(tableNode);
        return node;
      };

      // 1. Tokens over time, stacked by class.
      grid.append(card(
        'Tokens over time',
        `by ${unit}, stacked by token class`,
        gate(caps.usage, 'token usage', () => window.charts.columnChart({
          rows: data.timeseries, xKey: 'period', series,
          format: window.charts.compact, height: 250, width: this.plotWidth(1),
        })),
        window.charts.table(
          ['Period', 'Input', 'Output', 'Cache write', 'Cache read', 'Total'],
          data.timeseries.map((point) => [
            point.period,
            window.charts.exact(point.input), window.charts.exact(point.output),
            window.charts.exact(point.cache_write), window.charts.exact(point.cache_read),
            window.charts.exact(point.total_tokens),
          ]),
        ),
        true,
      ));

      // 2. Cost over time. A separate card, never a second axis.
      grid.append(card(
        'Estimated cost over time',
        `by ${unit}, from your pricing table`,
        gate(caps.usage === false ? false : caps.cost, 'priced usage', () => window.charts.areaChart({
          rows: data.timeseries, xKey: 'period', yKey: 'cost',
          color: token('--series-cost'), format: window.charts.money,
          label: 'Estimated cost', height: 210, width: this.plotWidth(1),
        })),
        window.charts.table(
          ['Period', 'Estimated cost', 'Sessions', 'Messages', 'Tool calls'],
          data.timeseries.map((point) => [
            point.period, window.charts.money(point.cost),
            point.sessions, point.messages, point.tool_calls,
          ]),
        ),
        true,
      ));

      // 3. Composition. Cache reads dominate the absolute scale, so the
      //    stacked columns above cannot show the mix; this card can.
      const totals = data.models.reduce((acc, model) => {
        for (const key of ['input', 'output', 'cache_write', 'cache_read']) {
          acc[key] = (acc[key] || 0) + (model[key] || 0);
        }
        return acc;
      }, {});
      grid.append(card(
        'Token mix',
        'share of every token billed, all sessions in this slice',
        gate(caps.usage, 'token usage', () => window.charts.shareBar(
          series.map((s) => ({ label: s.label, value: totals[s.key] || 0, color: s.color })),
          window.charts.exact,
        )),
        window.charts.table(
          ['Token class', 'Tokens', 'Share'],
          series.map((s) => {
            const value = totals[s.key] || 0;
            const sum = Object.values(totals).reduce((a, b) => a + b, 0) || 1;
            return [s.label, window.charts.exact(value), `${((value / sum) * 100).toFixed(1)}%`];
          }),
        ),
        true,
      ));

      // 4. Activity: sessions and messages over time.
      grid.append(card(
        'Messages over time',
        `by ${unit}`,
        window.charts.columnChart({
          rows: data.timeseries, xKey: 'period',
          series: [{ key: 'messages', label: 'Messages', color: token('--series-1') }],
          format: window.charts.compact, height: 180, width: this.plotWidth(2),
        }),
        window.charts.table(
          ['Period', 'Messages', 'Tool calls'],
          data.timeseries.map((point) => [point.period, point.messages, point.tool_calls]),
        ),
      ));

      grid.append(card(
        'Sessions over time',
        `by ${unit}`,
        window.charts.columnChart({
          rows: data.timeseries, xKey: 'period',
          series: [{ key: 'sessions', label: 'Sessions', color: token('--series-3') }],
          format: window.charts.compact, height: 180, width: this.plotWidth(2),
        }),
        window.charts.table(
          ['Period', 'Sessions'],
          data.timeseries.map((point) => [point.period, point.sessions]),
        ),
      ));

      // 5. Per project.
      const projectRows = data.projects.map((entry) => ({
        label: shortProject(entry.project),
        value: entry.cost,
        extra: [
          { label: 'Tokens', value: window.charts.exact(entry.total_tokens) },
          { label: 'Sessions', value: String(entry.sessions) },
          { label: 'Tool calls', value: String(entry.tool_calls) },
        ],
      }));
      grid.append(card(
        'Estimated cost by project',
        `${data.projects.length} project${data.projects.length === 1 ? '' : 's'}`,
        window.charts.barChart({
          rows: projectRows.filter((row) => row.value > 0), color: token('--series-1'),
          format: window.charts.money, label: 'Estimated cost', maxRows: 10,
        }),
        window.charts.table(
          ['Project', 'Estimated cost', 'Tokens', 'Sessions', 'Messages', 'Disk'],
          data.projects.map((entry) => [
            entry.project, window.charts.money(entry.cost),
            window.charts.exact(entry.total_tokens), entry.sessions, entry.messages,
            formatBytes(entry.file_size),
          ]),
        ),
      ));

      // 6. Per model.
      grid.append(card(
        'Tokens by model',
        `${data.models.length} model${data.models.length === 1 ? '' : 's'} seen`,
        window.charts.barChart({
          rows: data.models.filter((entry) => entry.total_tokens > 0).map((entry) => ({
            label: entry.model,
            value: entry.total_tokens,
            extra: [
              { label: 'Estimated cost', value: window.charts.money(entry.cost) },
              { label: 'Responses', value: window.charts.exact(entry.messages) },
            ],
          })),
          color: token('--series-2'), format: window.charts.compact,
          label: 'Tokens', maxRows: 10,
        }),
        window.charts.table(
          ['Model', 'Input', 'Output', 'Cache write', 'Cache read', 'Estimated cost'],
          data.models.map((entry) => [
            entry.model,
            window.charts.exact(entry.input), window.charts.exact(entry.output),
            window.charts.exact(entry.cache_write), window.charts.exact(entry.cache_read),
            window.charts.money(entry.cost),
          ]),
        ),
      ));

      // 7. Tool usage.
      grid.append(card(
        'Tool calls',
        `${window.charts.exact(data.tools.total_calls)} calls across `
          + `${data.tools.distinct_tools} tools`,
        gate(caps.tool_calls, 'tool calls', () => window.charts.barChart({
          rows: data.tools.tools.map((entry) => ({ label: entry.name, value: entry.count })),
          color: token('--series-3'), format: window.charts.exact,
          label: 'Calls', maxRows: 12,
        })),
        window.charts.table(
          ['Tool', 'Calls'],
          data.tools.tools.map((entry) => [entry.name, window.charts.exact(entry.count)]),
        ),
      ));

      // 8. Heatmap.
      grid.append(card(
        'When you work',
        'entries by weekday and hour, local to the transcript timestamps (UTC)',
        window.charts.heatmap(data.heatmap, window.charts.exact),
        window.charts.table(
          ['Weekday', ...Array.from({ length: 24 }, (_, h) => String(h))],
          data.heatmap.weekdays.map((day, index) => [day, ...data.heatmap.grid[index]]),
        ),
        true,
      ));

      // 9. Disk.
      const disk = data.disk;
      grid.append(card(
        'Disk usage',
        `${formatBytes(disk.transcript_bytes)} of session history`,
        window.charts.barChart({
          rows: disk.largest.map((entry) => ({
            label: entry.title || entry.session_id,
            value: entry.file_size,
            extra: [{ label: 'Messages', value: String(entry.message_count) }],
          })),
          color: token('--series-4'), format: formatBytes,
          label: 'Transcript size', maxRows: 10,
        }),
        window.charts.table(
          ['Session', 'Size', 'Messages', 'Project'],
          disk.largest.map((entry) => [
            entry.title || entry.session_id, formatBytes(entry.file_size),
            entry.message_count, entry.project_path,
          ]),
        ),
      ));

      // 10. Cumulative growth.
      grid.append(card(
        'Transcript growth',
        'cumulative bytes written',
        window.charts.areaChart({
          rows: disk.growth.map((point) => ({
            period: point.period, value: point.cumulative_bytes,
          })),
          xKey: 'period', yKey: 'value', color: token('--series-4'),
          format: formatBytes, label: 'Cumulative size', height: 190, width: this.plotWidth(2),
        }),
        window.charts.table(
          ['Period', 'Written', 'Cumulative'],
          disk.growth.map((point) => [
            point.period, formatBytes(point.bytes), formatBytes(point.cumulative_bytes),
          ]),
        ),
      ));

      return grid;
    },
  };

  /** Bytes as a short human string. */
  function formatBytes(value) {
    const n = Number(value) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 ** 2) return (n / 1024).toFixed(0) + ' KB';
    if (n < 1024 ** 3) return (n / 1024 ** 2).toFixed(1) + ' MB';
    return (n / 1024 ** 3).toFixed(2) + ' GB';
  }

  window.usageView = usageView;
}());
