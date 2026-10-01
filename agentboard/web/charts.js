/* SVG chart primitives, bundled locally.
 *
 * Written here rather than vendored so the app needs no CDN and no build
 * step, and so the marks match the design rules exactly: bars capped at
 * 24px with a 4px rounded data-end, a 2px surface gap between touching
 * fills, 2px lines with >=8px end markers carrying a 2px surface ring, and
 * solid hairline gridlines one step off the surface.
 *
 * Colour is never chosen here. Every mark reads a CSS custom property
 * (`--series-1` .. `--series-4`, the sequential ramp, the chrome tokens) so
 * light and dark are two validated palettes rather than an automatic flip.
 *
 * Exposes `window.charts`.
 */
'use strict';

(function () {
  const SVG_NS = 'http://www.w3.org/2000/svg';

  /** Bar thickness ceiling; the band's leftover is deliberately air. */
  const MAX_BAR = 24;
  /** Surface-coloured gap separating touching fills. */
  const GAP = 2;
  /** Corner radius on the data end of a bar. */
  const RADIUS = 4;

  /** Create an SVG element with attributes. */
  function svg(tag, attrs = {}, children = []) {
    const node = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      node.setAttribute(key, String(value));
    }
    for (const child of [].concat(children)) {
      if (child) node.append(child);
    }
    return node;
  }

  /** Create an HTML element with attributes. */
  function html(tag, attrs = {}, children = []) {
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
      if (child) node.append(child.nodeType ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  /**
   * The width a chart should be drawn at.
   *
   * Charts are built into a detached node, so the container cannot be
   * measured yet; the caller passes the width it will occupy. Drawing at
   * that width and scaling 1:1 keeps every mark spec literal - a 24px bar
   * really is 24px, a 4px radius really is 4px.
   */
  function resolveWidth(requested, fallback = 720) {
    const value = Number(requested);
    return Number.isFinite(value) && value > 240 ? Math.round(value) : fallback;
  }

  /** Approximate rendered width of a label, for fit tests before placing it. */
  function textWidth(text, fontSize = 11) {
    return String(text).length * fontSize * 0.58;
  }

  /** Round a range to clean tick values. */
  function niceTicks(max, count = 4) {
    if (!(max > 0)) return [0];
    const rough = max / count;
    const magnitude = Math.pow(10, Math.floor(Math.log10(rough)));
    const normalised = rough / magnitude;
    const step = (normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10) * magnitude;
    const ticks = [];
    for (let value = 0; value <= max + step * 0.001; value += step) ticks.push(value);
    if (ticks[ticks.length - 1] < max) ticks.push(ticks[ticks.length - 1] + step);
    return ticks;
  }

  /** Compact number for axis ticks and labels. */
  function compact(value) {
    const n = Number(value) || 0;
    const sign = n < 0 ? '-' : '';
    const abs = Math.abs(n);
    if (abs < 1000) return sign + (Number.isInteger(abs) ? abs : abs.toFixed(1));
    if (abs < 1e6) return sign + (abs / 1e3).toFixed(abs < 1e4 ? 1 : 0) + 'k';
    if (abs < 1e9) return sign + (abs / 1e6).toFixed(abs < 1e7 ? 1 : 0) + 'M';
    return sign + (abs / 1e9).toFixed(1) + 'B';
  }

  /** Money, compact but never misleadingly rounded to zero. */
  function money(value) {
    const n = Number(value) || 0;
    if (n === 0) return '$0';
    if (Math.abs(n) < 0.01) return '<$0.01';
    if (Math.abs(n) < 1000) return '$' + n.toFixed(2);
    return '$' + compact(n);
  }

  /** Full-precision number with thousands separators, for tooltips. */
  function exact(value) {
    return (Number(value) || 0).toLocaleString();
  }

  /* ------------------------------------------------------------ tooltip */

  let tooltipNode = null;

  /** The single shared tooltip element. */
  function tooltip() {
    if (!tooltipNode) {
      tooltipNode = html('div.chart-tooltip', { role: 'status' });
      tooltipNode.style.display = 'none';
      document.body.append(tooltipNode);
    }
    return tooltipNode;
  }

  /**
   * Show the tooltip near a pointer position.
   * @param {{title: string, rows: Array<{label: string, value: string, color?: string}>}} content
   */
  function showTooltip(event, content) {
    const node = tooltip();
    node.replaceChildren(
      html('div.tt-title', { text: content.title }),
      ...content.rows.map((row) => html('div.tt-row', {}, [
        row.color ? html('span.tt-dot', { style: `background:${row.color}` }) : null,
        html('span.tt-label', { text: row.label }),
        html('span.tt-value', { text: row.value }),
      ])),
    );
    node.style.display = 'block';
    const pad = 14;
    const width = node.offsetWidth;
    const height = node.offsetHeight;
    let left = event.clientX + pad;
    let top = event.clientY + pad;
    if (left + width > window.innerWidth - 8) left = event.clientX - width - pad;
    if (top + height > window.innerHeight - 8) top = event.clientY - height - pad;
    node.style.left = Math.max(8, left) + 'px';
    node.style.top = Math.max(8, top) + 'px';
  }

  /** Hide the shared tooltip. */
  function hideTooltip() {
    if (tooltipNode) tooltipNode.style.display = 'none';
  }

  /* ------------------------------------------------------------- legend */

  /**
   * A legend, always rendered for two or more series.
   * @param {Array<{label: string, color: string}>} series
   */
  function legend(series) {
    if (!series || series.length < 2) return null;
    return html('div.chart-legend', {},
      series.map((s) => html('span.legend-item', {}, [
        html('span.legend-swatch', { style: `background:${s.color}` }),
        html('span', { text: s.label }),
      ])));
  }

  /* ---------------------------------------------------- stacked columns */

  /**
   * Columns over time, optionally stacked.
   *
   * @param {object} spec
   * @param {Array<object>} spec.rows      one object per period
   * @param {string} spec.xKey             field holding the period label
   * @param {Array<{key,label,color}>} spec.series  one entry per stacked band
   * @param {Function} [spec.format]       value formatter for labels/tooltips
   * @param {number} [spec.height]
   */
  function columnChart(spec) {
    const {
      rows = [], xKey = 'period', series = [], format = compact,
      height = 240, emphasiseLast = true,
    } = spec;

    const wrap = html('div.chart-body');
    if (!rows.length) {
      wrap.append(html('div.chart-empty', { text: 'No activity in this range.' }));
      return wrap;
    }

    const padding = { top: 14, right: 10, bottom: 26, left: 46 };
    const width = resolveWidth(spec.width);
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const totals = rows.map((row) => series.reduce((sum, s) => sum + (Number(row[s.key]) || 0), 0));
    const max = Math.max(...totals, 0);
    const ticks = niceTicks(max);
    const top = ticks[ticks.length - 1] || 1;
    const y = (value) => padding.top + plotH - (value / top) * plotH;

    const band = plotW / rows.length;
    const barWidth = Math.min(MAX_BAR, Math.max(3, band - 6));

    const root = svg('svg', {
      class: 'chart-svg',
      viewBox: `0 0 ${width} ${height}`,
      width, height,
      role: 'img',
    });

    // Gridlines first, so data sits above them.
    for (const tick of ticks) {
      root.append(svg('line', {
        class: 'grid-line',
        x1: padding.left, x2: width - padding.right,
        y1: y(tick), y2: y(tick),
      }));
      root.append(svg('text', {
        class: 'axis-text', x: padding.left - 7, y: y(tick) + 3.5,
        'text-anchor': 'end',
      }, [document.createTextNode(format(tick))]));
    }

    rows.forEach((row, index) => {
      const centre = padding.left + band * index + band / 2;
      const x = centre - barWidth / 2;
      let cursor = 0;

      // Build from the baseline up so the gap sits between segments.
      const segments = series
        .map((s) => ({ ...s, value: Number(row[s.key]) || 0 }))
        .filter((s) => s.value > 0);

      segments.forEach((segment, position) => {
        const bottom = y(cursor);
        cursor += segment.value;
        const topY = y(cursor);
        const isTop = position === segments.length - 1;
        // Reserve the surface gap below every segment except the first.
        const gap = position === 0 ? 0 : GAP;
        const h = Math.max(1, bottom - topY - gap);
        const rectY = bottom - gap - h;
        root.append(svg('path', {
          class: 'bar',
          d: roundedTopRect(x, rectY, barWidth, h, isTop ? Math.min(RADIUS, h / 2, barWidth / 2) : 0),
          fill: segment.color,
        }));
      });

      // A generous invisible hit area: the whole band, not just the bar.
      const hit = svg('rect', {
        class: 'hit',
        x: padding.left + band * index, y: padding.top,
        width: band, height: plotH, fill: 'transparent',
      });
      const content = {
        title: row[xKey],
        rows: series.map((s) => ({
          label: s.label,
          value: format(Number(row[s.key]) || 0),
          color: s.color,
        })),
      };
      if (series.length > 1) {
        content.rows.push({ label: 'Total', value: format(totals[index]) });
      }
      hit.addEventListener('mousemove', (event) => showTooltip(event, content));
      hit.addEventListener('mouseleave', hideTooltip);
      root.append(hit);

      // Selective direct labels: only the final column, and only if it fits.
      if (emphasiseLast && index === rows.length - 1 && totals[index] > 0) {
        const label = format(totals[index]);
        if (textWidth(label) <= band * 2) {
          root.append(svg('text', {
            class: 'value-label', x: centre, y: y(totals[index]) - 6,
            'text-anchor': 'middle',
          }, [document.createTextNode(label)]));
        }
      }
    });

    // Baseline.
    root.append(svg('line', {
      class: 'axis-line',
      x1: padding.left, x2: width - padding.right,
      y1: padding.top + plotH, y2: padding.top + plotH,
    }));

    // X labels, thinned so they never collide.
    const every = Math.max(1, Math.ceil((rows.length * 62) / plotW));
    rows.forEach((row, index) => {
      if (index % every !== 0 && index !== rows.length - 1) return;
      root.append(svg('text', {
        class: 'axis-text',
        x: padding.left + band * index + band / 2,
        y: height - 8,
        'text-anchor': 'middle',
      }, [document.createTextNode(shortPeriod(row[xKey]))]));
    });

    wrap.append(root);
    const box = legend(series);
    if (box) wrap.append(box);
    return wrap;
  }

  /** Path for a rectangle rounded only on its data end (the top). */
  function roundedTopRect(x, y, w, h, r) {
    const radius = Math.max(0, Math.min(r, w / 2, h));
    if (radius <= 0) return `M${x},${y}h${w}v${h}h${-w}Z`;
    return `M${x},${y + radius}a${radius},${radius} 0 0 1 ${radius},${-radius}`
      + `h${w - radius * 2}a${radius},${radius} 0 0 1 ${radius},${radius}`
      + `v${h - radius}h${-w}Z`;
  }

  /** Shorten an ISO day / ISO week / month key for an axis tick. */
  function shortPeriod(value) {
    const text = String(value || '');
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) {
      const date = new Date(text + 'T00:00:00');
      return date.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }
    if (/^\d{4}-W\d{2}$/.test(text)) return 'W' + text.slice(6);
    if (/^\d{4}-\d{2}$/.test(text)) {
      const date = new Date(text + '-01T00:00:00');
      return date.toLocaleDateString([], { month: 'short', year: '2-digit' });
    }
    return text;
  }

  /* -------------------------------------------------------- area / line */

  /**
   * A single series over time, drawn as a 2px line over a 10% wash.
   * One series, so no legend: the card title names it.
   */
  function areaChart(spec) {
    const { rows = [], xKey = 'period', yKey = 'value', color, format = compact,
            height = 200 } = spec;

    const wrap = html('div.chart-body');
    if (!rows.length) {
      wrap.append(html('div.chart-empty', { text: 'No activity in this range.' }));
      return wrap;
    }

    const padding = { top: 14, right: 12, bottom: 26, left: 46 };
    const width = resolveWidth(spec.width);
    const plotW = width - padding.left - padding.right;
    const plotH = height - padding.top - padding.bottom;

    const values = rows.map((row) => Number(row[yKey]) || 0);
    const ticks = niceTicks(Math.max(...values, 0));
    const top = ticks[ticks.length - 1] || 1;
    const x = (index) => rows.length === 1
      ? padding.left + plotW / 2
      : padding.left + (index / (rows.length - 1)) * plotW;
    const y = (value) => padding.top + plotH - (value / top) * plotH;

    const root = svg('svg', {
      class: 'chart-svg', viewBox: `0 0 ${width} ${height}`,
      width, height, role: 'img',
    });

    for (const tick of ticks) {
      root.append(svg('line', {
        class: 'grid-line', x1: padding.left, x2: width - padding.right,
        y1: y(tick), y2: y(tick),
      }));
      root.append(svg('text', {
        class: 'axis-text', x: padding.left - 7, y: y(tick) + 3.5, 'text-anchor': 'end',
      }, [document.createTextNode(format(tick))]));
    }

    const line = values.map((value, index) => `${index ? 'L' : 'M'}${x(index)},${y(value)}`).join('');
    root.append(svg('path', {
      class: 'area-fill',
      d: `${line}L${x(values.length - 1)},${y(0)}L${x(0)},${y(0)}Z`,
      fill: color,
    }));
    root.append(svg('path', { class: 'line-mark', d: line, stroke: color, fill: 'none' }));

    // End marker with a surface ring, so it stays legible over the line.
    const lastIndex = values.length - 1;
    root.append(svg('circle', {
      class: 'end-dot', cx: x(lastIndex), cy: y(values[lastIndex]), r: 4.5, fill: color,
    }));

    // Crosshair and nearest-point tooltip across the whole plot.
    const crosshair = svg('line', {
      class: 'crosshair', y1: padding.top, y2: padding.top + plotH, x1: 0, x2: 0,
    });
    crosshair.style.display = 'none';
    root.append(crosshair);

    const surface = svg('rect', {
      x: padding.left, y: padding.top, width: plotW, height: plotH, fill: 'transparent',
    });
    surface.addEventListener('mousemove', (event) => {
      const box = root.getBoundingClientRect();
      const ratio = (event.clientX - box.left) / box.width * width;
      let nearest = 0;
      let best = Infinity;
      for (let i = 0; i < rows.length; i += 1) {
        const distance = Math.abs(x(i) - ratio);
        if (distance < best) { best = distance; nearest = i; }
      }
      crosshair.setAttribute('x1', x(nearest));
      crosshair.setAttribute('x2', x(nearest));
      crosshair.style.display = 'block';
      showTooltip(event, {
        title: rows[nearest][xKey],
        rows: [{ label: spec.label || 'Value', value: format(values[nearest]), color }],
      });
    });
    surface.addEventListener('mouseleave', () => {
      crosshair.style.display = 'none';
      hideTooltip();
    });
    root.append(surface);

    root.append(svg('line', {
      class: 'axis-line', x1: padding.left, x2: width - padding.right,
      y1: padding.top + plotH, y2: padding.top + plotH,
    }));

    const every = Math.max(1, Math.ceil((rows.length * 62) / plotW));
    rows.forEach((row, index) => {
      if (index % every !== 0 && index !== rows.length - 1) return;
      root.append(svg('text', {
        class: 'axis-text', x: x(index), y: height - 8, 'text-anchor': 'middle',
      }, [document.createTextNode(shortPeriod(row[xKey]))]));
    });

    wrap.append(root);
    return wrap;
  }

  /* ------------------------------------------------------ horizontal bars */

  /**
   * Horizontal bars, for long category names.
   * A single hue by default: magnitude, not identity. A row may carry its
   * own `color` when the rows *are* identities (one bar per provider); the
   * label always names the row, so colour is never the only key.
   */
  function barChart(spec) {
    const { rows = [], labelKey = 'label', valueKey = 'value',
            color, format = compact, maxRows = 12 } = spec;

    const wrap = html('div.chart-body');
    const data = rows.slice(0, maxRows);
    if (!data.length) {
      wrap.append(html('div.chart-empty', { text: 'Nothing recorded yet.' }));
      return wrap;
    }

    const max = Math.max(...data.map((row) => Number(row[valueKey]) || 0), 0) || 1;
    const list = html('div.hbar-list');

    for (const row of data) {
      const value = Number(row[valueKey]) || 0;
      const percent = Math.max(0.6, (value / max) * 100);
      const fill = row.color || color;
      const track = html('div.hbar-track', {}, [
        html('div.hbar-fill', { style: `width:${percent}%;background:${fill}` }),
      ]);
      const node = html('div.hbar-row', {
        onmousemove: (event) => showTooltip(event, {
          title: String(row[labelKey]),
          rows: [
            { label: spec.label || 'Value', value: format(value), color: fill },
            ...(row.extra || []),
          ],
        }),
        onmouseleave: hideTooltip,
      }, [
        html('div.hbar-label.truncate', { text: String(row[labelKey]), title: String(row[labelKey]) }),
        track,
        html('div.hbar-value.num', { text: format(value) }),
      ]);
      list.append(node);
    }
    wrap.append(list);
    if (rows.length > maxRows) {
      wrap.append(html('div.chart-note', {
        text: `Showing the top ${maxRows} of ${rows.length}. The table view lists them all.`,
      }));
    }
    return wrap;
  }

  /* ------------------------------------------------------- share bar */

  /**
   * One horizontal 100% stacked bar: part-to-whole for a single total.
   *
   * Used where absolute stacking cannot show the mix, because one class
   * dominates the scale. Segments carry an inside label only when the text
   * fits with padding; otherwise the legend and table carry it.
   *
   * @param {Array<{label,value,color}>} parts
   */
  function shareBar(parts, format = compact) {
    const wrap = html('div.chart-body');
    const total = parts.reduce((sum, part) => sum + (Number(part.value) || 0), 0);
    if (!total) {
      wrap.append(html('div.chart-empty', { text: 'Nothing recorded yet.' }));
      return wrap;
    }

    const bar = html('div.sharebar');
    for (const part of parts) {
      const value = Number(part.value) || 0;
      if (value <= 0) continue;
      const percent = (value / total) * 100;
      const text = `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
      // Only label inside when the text genuinely fits the segment.
      const fits = percent > 8;
      bar.append(html('div.sharebar-seg', {
        style: `flex:${percent} 1 0;background:${part.color}`,
        title: `${part.label}: ${format(value)} (${text})`,
        onmousemove: (event) => showTooltip(event, {
          title: part.label,
          rows: [
            { label: 'Tokens', value: format(value), color: part.color },
            { label: 'Share', value: text },
          ],
        }),
        onmouseleave: hideTooltip,
      }, fits ? [html('span.sharebar-text', { text })] : []));
    }
    wrap.append(bar);

    const box = legend(parts.map((p) => ({ label: p.label, color: p.color })));
    if (box) wrap.append(box);
    return wrap;
  }

  /* ----------------------------------------------------------- heatmap */

  /**
   * Weekday x hour activity grid, on a single sequential hue.
   * @param {{weekdays: string[], grid: number[][], peak: number}} data
   */
  function heatmap(data, format = exact) {
    const wrap = html('div.chart-body');
    const peak = data.peak || 1;
    const table = html('div.heatmap');

    // Hour ruler.
    const ruler = html('div.heatmap-row');
    ruler.append(html('div.heatmap-day'));
    for (let hour = 0; hour < 24; hour += 1) {
      ruler.append(html('div.heatmap-hour', { text: hour % 6 === 0 ? String(hour) : '' }));
    }
    table.append(ruler);

    data.weekdays.forEach((day, index) => {
      const row = html('div.heatmap-row');
      row.append(html('div.heatmap-day', { text: day.slice(0, 3) }));
      for (let hour = 0; hour < 24; hour += 1) {
        const value = data.grid[index][hour] || 0;
        // Six discrete steps of one hue: empty cells stay at the surface.
        const step = value === 0 ? 0 : Math.min(6, Math.ceil((value / peak) * 6));
        const cell = html('div.heatmap-cell', {
          'data-step': String(step),
          onmousemove: (event) => showTooltip(event, {
            title: `${day} ${String(hour).padStart(2, '0')}:00`,
            rows: [{ label: 'Entries', value: format(value) }],
          }),
          onmouseleave: hideTooltip,
        });
        row.append(cell);
      }
      table.append(row);
    });

    wrap.append(table);
    wrap.append(html('div.heatmap-key', {}, [
      html('span.faint', { text: 'less' }),
      ...[0, 1, 2, 3, 4, 5, 6].map((step) =>
        html('span.heatmap-cell', { 'data-step': String(step) })),
      html('span.faint', { text: 'more' }),
    ]));
    return wrap;
  }

  /* --------------------------------------------------------- sparkline */

  /** A 12-point trend for a stat tile; the last point wears the accent. */
  function sparkline(values, color, width = 96, height = 26) {
    const data = (values || []).slice(-12);
    if (data.length < 2) return null;
    const max = Math.max(...data, 0) || 1;
    const step = width / (data.length - 1);
    const y = (value) => height - 2 - (value / max) * (height - 6);
    const path = data.map((value, index) => `${index ? 'L' : 'M'}${index * step},${y(value)}`).join('');
    const root = svg('svg', {
      class: 'sparkline', viewBox: `0 0 ${width} ${height}`, 'aria-hidden': 'true',
    });
    root.append(svg('path', { class: 'spark-line', d: path, stroke: 'currentColor', fill: 'none' }));
    root.append(svg('circle', {
      class: 'spark-dot', cx: (data.length - 1) * step, cy: y(data[data.length - 1]),
      r: 2.5, fill: color,
    }));
    return root;
  }

  /* -------------------------------------------------------- table view */

  /**
   * The table twin every chart carries, so no value is gated behind hover.
   * @param {string[]} headers
   * @param {Array<Array<string>>} body
   */
  function table(headers, body) {
    const head = html('tr', {}, headers.map((h) => html('th', { text: h })));
    const rows = body.map((row) => html('tr', {},
      row.map((cell, index) => html('td', {
        text: String(cell),
        class: index === 0 ? '' : 'num',
      }))));
    return html('div.chart-table-wrap', {}, [
      html('table.chart-table', {}, [html('thead', {}, [head]), html('tbody', {}, rows)]),
    ]);
  }

  window.charts = {
    columnChart, areaChart, barChart, shareBar, heatmap, sparkline, table, legend,
    resolveWidth,
    compact, money, exact, niceTicks, shortPeriod, textWidth,
    showTooltip, hideTooltip,
  };
}());
