/* Whale Screener dashboard — fetch, render, refresh */

const REFRESH_MS = 60_000;
const tooltip = document.getElementById('tooltip');

// ---------------------------------------------------------------------------
// Formatting helpers
// ---------------------------------------------------------------------------
const fmtUsdAbbrev = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 1e12) return `${sign}$${(a / 1e12).toFixed(2)}T`;
  if (a >= 1e9) return `${sign}$${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}$${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}$${(a / 1e3).toFixed(0)}K`;
  return `${sign}$${a.toFixed(0)}`;
};

const fmtCount = (v) => {
  const n = Number(v);
  if (!isFinite(n)) return '—';
  const sign = n < 0 ? '-' : '';
  const a = Math.abs(n);
  if (a >= 1e9) return `${sign}${(a / 1e9).toFixed(2)}B`;
  if (a >= 1e6) return `${sign}${(a / 1e6).toFixed(1)}M`;
  if (a >= 1e3) return `${sign}${(a / 1e3).toFixed(1)}K`;
  return `${sign}${a.toLocaleString()}`;
};

const fmtPrice = (v) => {
  const n = Number(v);
  return isFinite(n) ? `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}` : '—';
};

const fmtPct = (v, digits = 1) => {
  const n = Number(v);
  return isFinite(n) ? `${n >= 0 ? '+' : ''}${(n * 100).toFixed(digits)}%` : '—';
};

const fmtTime = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? '—' : d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
};

const escapeHtml = (s) =>
  String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
  return res.json();
}

function markStale(el, stale) {
  el.classList.toggle('stale', stale);
}

// ---------------------------------------------------------------------------
// Market tide: stat tiles + two-series line chart with crosshair tooltip
// ---------------------------------------------------------------------------
async function loadMarketTide() {
  const box = document.getElementById('tide-chart');
  markStale(box, true);
  try {
    const { data } = await getJSON('/api/market-tide?interval_5m=true');
    const ticks = (data || [])
      .map((d) => ({
        t: new Date(d.timestamp),
        call: Number(d.net_call_premium),
        put: Number(d.net_put_premium),
        vol: Number(d.net_volume),
      }))
      .filter((d) => !isNaN(d.t) && isFinite(d.call) && isFinite(d.put));

    renderTideTiles(ticks);
    renderTideChart(box, ticks);
  } catch (err) {
    box.innerHTML = `<div class="error-note">Market tide unavailable: ${escapeHtml(err.message)}</div>`;
  } finally {
    markStale(box, false);
  }
}

function renderTideTiles(ticks) {
  if (!ticks.length) return;
  const last = ticks[ticks.length - 1];
  // Compare against ~30 minutes earlier (6 ticks at 5m) for the delta line
  const prior = ticks[Math.max(0, ticks.length - 7)];

  // bullishWhenRising: rising call premium is bullish (green up), but rising
  // put premium is bearish, so its delta colors are inverted.
  const setTile = (valueId, deltaId, now, before, bullishWhenRising) => {
    document.getElementById(valueId).textContent = fmtUsdAbbrev(now);
    const el = document.getElementById(deltaId);
    const diff = now - before;
    el.textContent = `${diff >= 0 ? '▲' : '▼'} ${fmtUsdAbbrev(Math.abs(diff))} vs 30m ago`;
    const good = bullishWhenRising ? diff >= 0 : diff < 0;
    el.className = `tile-delta ${good ? 'up' : 'down'}`;
  };
  setTile('tile-call-prem', 'tile-call-delta', last.call, prior.call, true);
  setTile('tile-put-prem', 'tile-put-delta', last.put, prior.put, false);

  document.getElementById('tile-net-vol').textContent = fmtCount(last.vol);

  // Signed spread: negative net put premium (put selling) is itself bullish
  const spread = last.call - last.put;
  const sentiment = document.getElementById('tile-sentiment');
  const note = document.getElementById('tile-sentiment-note');
  if (spread > 0) {
    sentiment.textContent = 'Bullish';
    sentiment.style.color = 'var(--series-call)';
  } else {
    sentiment.textContent = 'Bearish';
    sentiment.style.color = 'var(--series-put)';
  }
  note.textContent = `call vs put premium spread ${fmtUsdAbbrev(spread)}`;
}

function renderTideChart(box, ticks) {
  if (!ticks.length) {
    box.innerHTML = '<div class="empty">No tide data yet today.</div>';
    return;
  }

  const W = 760;
  const H = 300;
  const pad = { top: 12, right: 16, bottom: 28, left: 56 };
  const plotW = W - pad.left - pad.right;
  const plotH = H - pad.top - pad.bottom;

  const t0 = ticks[0].t.getTime();
  const t1 = ticks[ticks.length - 1].t.getTime() || t0 + 1;
  const values = ticks.flatMap((d) => [d.call, d.put]);
  let vMin = Math.min(0, ...values);
  let vMax = Math.max(0, ...values);
  if (vMin === vMax) { vMin -= 1; vMax += 1; }
  const range = vMax - vMin;
  vMin -= range * 0.05;
  vMax += range * 0.05;

  const x = (t) => pad.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
  const y = (v) => pad.top + (1 - (v - vMin) / (vMax - vMin)) * plotH;

  const linePath = (key) =>
    ticks.map((d, i) => `${i ? 'L' : 'M'}${x(d.t.getTime()).toFixed(1)},${y(d[key]).toFixed(1)}`).join('');

  // Horizontal gridlines at ~4 nice steps
  const gridLines = [];
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const v = vMin + ((vMax - vMin) * i) / yTicks;
    gridLines.push(
      `<line x1="${pad.left}" x2="${W - pad.right}" y1="${y(v)}" y2="${y(v)}" stroke="var(--gridline)" stroke-width="1"/>` +
      `<text x="${pad.left - 8}" y="${y(v) + 3.5}" text-anchor="end" font-size="10" fill="var(--text-muted)" font-variant-numeric="tabular-nums">${fmtUsdAbbrev(v)}</text>`
    );
  }
  // Zero baseline slightly stronger than the grid
  const zeroLine = vMin < 0 && vMax > 0
    ? `<line x1="${pad.left}" x2="${W - pad.right}" y1="${y(0)}" y2="${y(0)}" stroke="var(--baseline)" stroke-width="1"/>`
    : '';

  // X axis labels: ~5 evenly spaced time labels
  const xLabels = [];
  for (let i = 0; i < 5; i++) {
    const idx = Math.round((ticks.length - 1) * (i / 4));
    const d = ticks[idx];
    xLabels.push(
      `<text x="${x(d.t.getTime())}" y="${H - 8}" text-anchor="middle" font-size="10" fill="var(--text-muted)">${fmtTime(d.t)}</text>`
    );
  }

  const last = ticks[ticks.length - 1];

  box.innerHTML = `
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Market tide net call and put premium over the trading day">
      ${gridLines.join('')}
      ${zeroLine}
      <path d="${linePath('call')}" fill="none" stroke="var(--series-call)" stroke-width="2" stroke-linejoin="round"/>
      <path d="${linePath('put')}" fill="none" stroke="var(--series-put)" stroke-width="2" stroke-linejoin="round"/>
      <circle cx="${x(last.t.getTime())}" cy="${y(last.call)}" r="3" fill="var(--series-call)" stroke="var(--surface-1)" stroke-width="2"/>
      <circle cx="${x(last.t.getTime())}" cy="${y(last.put)}" r="3" fill="var(--series-put)" stroke="var(--surface-1)" stroke-width="2"/>
      <line id="tide-crosshair" y1="${pad.top}" y2="${pad.top + plotH}" stroke="var(--baseline)" stroke-width="1" visibility="hidden"/>
      <rect id="tide-hit" x="${pad.left}" y="${pad.top}" width="${plotW}" height="${plotH}" fill="transparent"/>
    </svg>`;

  const svg = box.querySelector('svg');
  const crosshair = box.querySelector('#tide-crosshair');
  const hit = box.querySelector('#tide-hit');

  const onMove = (evt) => {
    const rect = svg.getBoundingClientRect();
    const px = ((evt.clientX - rect.left) / rect.width) * W;
    const frac = Math.min(1, Math.max(0, (px - pad.left) / plotW));
    const idx = Math.round(frac * (ticks.length - 1));
    const d = ticks[idx];
    const cx = x(d.t.getTime());
    crosshair.setAttribute('x1', cx);
    crosshair.setAttribute('x2', cx);
    crosshair.setAttribute('visibility', 'visible');
    tooltip.style.display = 'block';
    tooltip.innerHTML = `
      <div class="t-title">${fmtTime(d.t)}</div>
      <div class="t-row"><span>Net call prem</span><b>${fmtUsdAbbrev(d.call)}</b></div>
      <div class="t-row"><span>Net put prem</span><b>${fmtUsdAbbrev(d.put)}</b></div>
      <div class="t-row"><span>Net volume</span><b>${fmtCount(d.vol)}</b></div>`;
    const pageX = evt.clientX + 14;
    const pageY = evt.clientY + 14;
    const tw = tooltip.offsetWidth;
    tooltip.style.left = `${Math.min(pageX, window.innerWidth - tw - 12) + window.scrollX}px`;
    tooltip.style.top = `${pageY + window.scrollY}px`;
  };
  const onLeave = () => {
    crosshair.setAttribute('visibility', 'hidden');
    tooltip.style.display = 'none';
  };
  hit.addEventListener('mousemove', onMove);
  hit.addEventListener('mouseleave', onLeave);
}

// ---------------------------------------------------------------------------
// Top net impact — diverging bars around a zero midline
// ---------------------------------------------------------------------------
async function loadTopImpact() {
  const list = document.getElementById('impact-list');
  markStale(list, true);
  try {
    const { data } = await getJSON('/api/top-net-impact?limit=12');
    const rows = (data || []).map((d) => ({ ticker: d.ticker, v: Number(d.net_premium) }));
    if (!rows.length) {
      list.innerHTML = '<div class="empty">No data.</div>';
      return;
    }
    const maxAbs = Math.max(...rows.map((r) => Math.abs(r.v)), 1);
    list.innerHTML = rows
      .map((r) => {
        const frac = Math.min(1, Math.abs(r.v) / maxAbs) * 50; // % of half-track
        const side = r.v >= 0
          ? `left:50%; width:${frac}%; background:var(--series-call);`
          : `right:50%; width:${frac}%; background:var(--series-put);`;
        return `
          <div class="impact-row" title="${escapeHtml(r.ticker)}: ${fmtUsdAbbrev(r.v)} net premium">
            <span class="tkr">${escapeHtml(r.ticker)}</span>
            <span class="impact-track"><span class="mid"></span><span class="bar" style="${side}"></span></span>
            <span class="val">${fmtUsdAbbrev(r.v)}</span>
          </div>`;
      })
      .join('');
  } catch (err) {
    list.innerHTML = `<div class="error-note">Unavailable: ${escapeHtml(err.message)}</div>`;
  } finally {
    markStale(list, false);
  }
}

// ---------------------------------------------------------------------------
// Screener table
// ---------------------------------------------------------------------------
function screenerQuery() {
  const form = document.getElementById('screener-filters');
  const fd = new FormData(form);
  const params = new URLSearchParams({ limit: '50' });
  const order = fd.get('order');
  params.set('order', order);
  params.set('order_direction', fd.get('order_direction'));
  for (const key of ['min_marketcap', 'min_volume', 'min_iv_rank']) {
    const v = String(fd.get(key) || '').trim();
    if (v) params.set(key, v);
  }
  const issueType = fd.get('issue_type');
  if (issueType) params.append('issue_types[]', issueType);
  const sector = fd.get('sector');
  if (sector) params.append('sectors[]', sector);
  return params;
}

async function loadScreener() {
  const table = document.getElementById('screener-table');
  const tbody = table.querySelector('tbody');
  const emptyNote = document.getElementById('screener-empty');
  markStale(table, true);
  try {
    const { data } = await getJSON(`/api/screener?${screenerQuery()}`);
    const rows = data || [];
    emptyNote.hidden = rows.length > 0;
    tbody.innerHTML = rows
      .map((r) => {
        const close = Number(r.close);
        const prev = Number(r.prev_close);
        const chg = isFinite(close) && isFinite(prev) && prev !== 0 ? (close - prev) / prev : NaN;
        const netPrem = Number(r.net_call_premium) - Number(r.net_put_premium);
        const pcr = Number(r.put_call_ratio);
        const ivRank = Number(r.iv_rank);
        const relVol = Number(r.relative_volume);
        return `
          <tr>
            <td class="tkr-cell">${escapeHtml(r.ticker)}</td>
            <td>${escapeHtml(r.full_name || '')}</td>
            <td>${escapeHtml(r.sector || (r.issue_type === 'ETF' ? 'ETF' : '—'))}</td>
            <td class="num">${fmtPrice(close)}</td>
            <td class="num ${chg >= 0 ? 'pos' : 'neg'}">${fmtPct(chg)}</td>
            <td class="num ${netPrem >= 0 ? 'pos' : 'neg'}">${fmtUsdAbbrev(netPrem)}</td>
            <td class="num">${fmtCount(r.call_volume)}</td>
            <td class="num">${fmtCount(r.put_volume)}</td>
            <td class="num">${isFinite(pcr) ? pcr.toFixed(2) : '—'}</td>
            <td class="num">${isFinite(ivRank) ? ivRank.toFixed(0) : '—'}</td>
            <td class="num">${isFinite(Number(r.iv30d)) ? `${(Number(r.iv30d) * 100).toFixed(0)}%` : '—'}</td>
            <td class="num">${isFinite(relVol) ? `${relVol.toFixed(2)}×` : '—'}</td>
            <td class="num">${fmtUsdAbbrev(r.marketcap)}</td>
          </tr>`;
      })
      .join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="13" class="error-note">Screener unavailable: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    markStale(table, false);
  }
}

// ---------------------------------------------------------------------------
// Flow alerts
// ---------------------------------------------------------------------------
const humanizeRule = (s) =>
  String(s || '')
    .replace(/([a-z])([A-Z])/g, '$1 $2')
    .replace(/Repeated Hits/, 'Repeated hits');

async function loadFlowAlerts() {
  const table = document.getElementById('flow-table');
  const tbody = table.querySelector('tbody');
  markStale(table, true);
  try {
    const { data } = await getJSON('/api/flow-alerts?limit=25&min_premium=50000');
    tbody.innerHTML = (data || [])
      .map((a) => {
        const voi = Number(a.volume_oi_ratio);
        return `
          <tr>
            <td>${fmtTime(a.created_at)}</td>
            <td class="tkr-cell">${escapeHtml(a.ticker)}</td>
            <td><span class="tag ${a.type === 'call' ? 'call' : 'put'}">${escapeHtml(a.type)}</span></td>
            <td class="num">${fmtPrice(a.strike)}</td>
            <td>${escapeHtml(a.expiry)}</td>
            <td class="num">${fmtUsdAbbrev(a.total_premium)}</td>
            <td class="num">${isFinite(voi) ? voi.toFixed(1) : '—'}</td>
            <td>${escapeHtml(humanizeRule(a.alert_rule))}</td>
          </tr>`;
      })
      .join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="8" class="error-note">Flow alerts unavailable: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    markStale(table, false);
  }
}

// ---------------------------------------------------------------------------
// Dark pool prints
// ---------------------------------------------------------------------------
async function loadDarkPool() {
  const table = document.getElementById('darkpool-table');
  const tbody = table.querySelector('tbody');
  markStale(table, true);
  try {
    const { data } = await getJSON('/api/darkpool?limit=20&min_premium=1000000');
    tbody.innerHTML = (data || [])
      .map(
        (d) => `
          <tr>
            <td>${fmtTime(d.executed_at)}</td>
            <td class="tkr-cell">${escapeHtml(d.ticker)}</td>
            <td class="num">${fmtPrice(d.price)}</td>
            <td class="num">${fmtCount(d.size)}</td>
            <td class="num">${fmtUsdAbbrev(d.premium)}</td>
          </tr>`
      )
      .join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="5" class="error-note">Dark pool unavailable: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    markStale(table, false);
  }
}

// ---------------------------------------------------------------------------
// Congress trades
// ---------------------------------------------------------------------------
async function loadCongress() {
  const table = document.getElementById('congress-table');
  const tbody = table.querySelector('tbody');
  markStale(table, true);
  try {
    const { data } = await getJSON('/api/congress?limit=20');
    tbody.innerHTML = (data || [])
      .map((t) => {
        const type = String(t.txn_type || '');
        const isBuy = /buy|purchase/i.test(type);
        const isSell = /sell|sale/i.test(type);
        return `
          <tr>
            <td>${escapeHtml(t.filed_at_date || '—')}</td>
            <td>${escapeHtml(t.transaction_date || '—')}</td>
            <td>${escapeHtml(t.name || t.reporter || '—')}</td>
            <td class="tkr-cell">${escapeHtml(t.ticker || '—')}</td>
            <td class="${isBuy ? 'pos' : isSell ? 'neg' : ''}">${escapeHtml(type || '—')}</td>
            <td class="num">${escapeHtml(t.amounts || '—')}</td>
          </tr>`;
      })
      .join('');
  } catch (err) {
    tbody.innerHTML = `<tr><td colspan="6" class="error-note">Congress trades unavailable: ${escapeHtml(err.message)}</td></tr>`;
  } finally {
    markStale(table, false);
  }
}

// ---------------------------------------------------------------------------
// Refresh orchestration
// ---------------------------------------------------------------------------
async function refreshAll() {
  await Promise.allSettled([
    loadMarketTide(),
    loadTopImpact(),
    loadScreener(),
    loadFlowAlerts(),
    loadDarkPool(),
    loadCongress(),
  ]);
  document.getElementById('last-updated').textContent =
    `Updated ${new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit', second: '2-digit' })}`;
}

document.getElementById('screener-filters').addEventListener('submit', (e) => {
  e.preventDefault();
  loadScreener();
});
document.getElementById('refresh-btn').addEventListener('click', refreshAll);

refreshAll();
setInterval(refreshAll, REFRESH_MS);
