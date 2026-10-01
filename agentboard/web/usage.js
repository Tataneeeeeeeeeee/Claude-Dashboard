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

  /** The four token classes, in fixed slot order. Never cycled. */
  function tokenSeries() {
    return [
      { key: 'input', label: 'Input', color: token('--series-1') },
      { key: 'output', label: 'Output', color: token('--series-2') },
      { key: 'cache_write', label: 'Cache write', color: token('--series-3') },
      { key: 'cache_read', label: 'Cache read', color: token('--series-4') },
    ];
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
      if (preloaded && !this.state.data && !this.state.provider) {
        this.state.data = preloaded;
        this.paint();
        return;
      }
      await this.refresh();
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
        const params = new URLSearchParams({ granularity: this.state.granularity });
        if (this.state.project) params.set('project', this.state.project);
        if (this.state.provider) params.set('provider', this.state.provider);
        params.set('include_home_size', 'true');
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
      const page = Math.min(1320, available) - 40;          // page padding
      const column = columns === 1 ? page : (page - 13) / 2; // grid gap
      return Math.max(320, Math.round(column - 30));         // card padding
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

      page.append(...this.hero());
      page.append(this.kpis());
      page.append(this.cards());
      this.host.replaceChildren(page);
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

    /* --------------------------------------------------------------- hero */

    /** The one number the dashboard leads with, plus its caveat. */
    hero() {
      const totals = this.state.data.totals;
      const reported = totals.reported_cost;
      return [
        el('div.hero', {}, [
          el('span.hero-value', { text: window.charts.money(totals.estimated_cost) }),
          el('span.hero-label', {
            text: this.state.project ? 'estimated for this project' : 'estimated across all sessions',
          }),
        ]),
        el('div.hero-note', {
          text: 'A local estimate from your own pricing table, not a bill. '
            + (reported
              ? `Claude Code recorded ${window.charts.money(reported)} for the `
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
      const series = tokenSeries();
      const showTables = this.state.tables;
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
        window.charts.columnChart({
          rows: data.timeseries, xKey: 'period', series,
          format: window.charts.compact, height: 250, width: this.plotWidth(1),
        }),
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
        window.charts.areaChart({
          rows: data.timeseries, xKey: 'period', yKey: 'cost',
          color: token('--series-cost'), format: window.charts.money,
          label: 'Estimated cost', height: 210, width: this.plotWidth(1),
        }),
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
        window.charts.shareBar(
          series.map((s) => ({ label: s.label, value: totals[s.key] || 0, color: s.color })),
          window.charts.exact,
        ),
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
        window.charts.barChart({
          rows: data.tools.tools.map((entry) => ({ label: entry.name, value: entry.count })),
          color: token('--series-3'), format: window.charts.exact,
          label: 'Calls', maxRows: 12,
        }),
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
        disk.claude_home_bytes
          ? `${formatBytes(disk.claude_home_bytes)} in ~/.claude, `
            + `${formatBytes(disk.transcript_bytes)} of it transcripts`
          : `${formatBytes(disk.transcript_bytes)} of transcripts`,
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
