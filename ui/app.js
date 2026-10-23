/* Task Force Wallex Agent - dashboard logic (unofficial, read-only). */
'use strict';

/* ---------------- helpers ---------------- */
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => {
  if (c === '&') return '&' + 'amp;';
  if (c === '<') return '&' + 'lt;';
  if (c === '>') return '&' + 'gt;';
  if (c === '"') return '&' + 'quot;';
  return '&' + '#39;';  // apostrophe
});
const fmtNum = (v, d = 0) => (v == null || isNaN(v)) ? '-'
  : Number(v).toLocaleString(undefined, { maximumFractionDigits: d });
const fmtPct = (v, d = 2) => (v == null || isNaN(v)) ? '-'
  : (v >= 0 ? '+' : '') + Number(v).toFixed(d) + '%';
const fmtAge = (sec) => {
  if (sec == null) return '-';
  if (sec < 60) return Math.round(sec) + 's';
  if (sec < 3600) return Math.round(sec / 60) + 'm';
  if (sec < 86400) return (sec / 3600).toFixed(1) + 'h';
  return (sec / 86400).toFixed(1) + 'd';
};
const shortTime = (ts) => {
  if (!ts) return '-';
  try { return new Date(ts).toLocaleTimeString(); } catch (e) { return '-'; }
};

/* ---------------- theme ---------------- */
(function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem('tfa-theme'); } catch (e) { /* private */ }
  if (saved === 'light' || saved === 'dark') {
    document.documentElement.setAttribute('data-theme', saved);
  }
  const btn = $('theme-btn');
  if (btn) btn.addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') === 'light'
      ? 'light' : 'dark';
    const next = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('tfa-theme', next); } catch (e) { /* private */ }
  });
})();

/* ---------------- state ---------------- */
const S = {
  last: null,              // last all.json payload
  filters: { symbol: null, action: null, search: '', onlySignals: false },
  decisionsOpen: {},       // id -> detail already fetched (cached)
  tradesSort: { key: null, dir: 1 },
  logOpen: false,
};

/* ---------------- fetch + poll ---------------- */
async function fetchJson(path) {
  const r = await fetch(path, { headers: { 'Cache-Control': 'no-store' } });
  if (!r.ok) throw new Error(path + ' -> ' + r.status);
  return r.json();
}

function setConn(ok) {
  const dot = $('conn-dot');
  if (dot) { dot.className = 'dot ' + (ok ? 'ok' : 'err'); }
}

async function poll() {
  try {
    const data = await fetchJson('/api/export/all.json');
    S.last = data;
    setConn(true);
    renderOverview(data.overview || {});
    refreshSymbolChips(data.overview && data.overview.symbols
      ? data.overview.symbols : []);
    renderEquity(data.equity || {});
    renderGuardian(data.guardian || {});
    renderPositions(data.positions || {});
    renderDecisions(data.decisions || {});
    renderTrades(data.trades || {});
    renderGuardRejections(data.guard || {});
    renderLlm(data.llm || {});
    renderEvents(data.events || {});
    const foot = $('foot-refresh');
    if (foot) foot.textContent = 'refreshed ' + new Date().toLocaleTimeString();
  } catch (e) {
    setConn(false);
  }
  try {
    const tail = await fetchJson('/api/logtail?lines=150');
    renderLogtail(tail);
  } catch (e) { /* keep old log */ }
}

setInterval(() => { if (!document.hidden) poll(); }, 5000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) poll();
});
poll();

/* ---------------- export menu ---------------- */
(function wireExportMenu() {
  const btn = $('export-btn');
  const menu = $('export-menu');
  if (!btn || !menu) return;
  btn.addEventListener('click', (ev) => {
    ev.stopPropagation();
    menu.classList.toggle('hidden');
  });
  document.addEventListener('click', () => menu.classList.add('hidden'));
})();

/* ---------------- overview + KPIs ---------------- */
function renderOverview(o) {
  const badge = $('mode-badge');
  if (badge) {
    const mode = (o.mode || 'UNKNOWN').toLowerCase();
    badge.textContent = mode.toUpperCase();
    badge.className = 'badge mode ' + (mode === 'paper' || mode === 'live'
      ? mode : 'unknown');
  }
  if ($('run-id')) $('run-id').textContent = 'run ' + (o.run_id == null
    ? '-' : o.run_id);
  if ($('uptime')) $('uptime').textContent = o.uptime_sec != null
    ? 'up ' + fmtAge(o.uptime_sec) : '';
  const setVal = (id, txt, cls) => {
    const el = $(id);
    if (el) { el.textContent = txt; el.className = el.className
      .replace(/\s*(pos|neg)/g, '') + (cls ? ' ' + cls : ''); }
  };
  setVal('kpi-equity', fmtNum(o.equity_last == null ? null : o.equity_last));
  setVal('kpi-free', 'free ' + fmtNum(o.free_tmn) + ' TMN');
  const ret = o.return_pct;
  setVal('kpi-return', fmtPct(ret), ret != null && ret < 0 ? 'neg'
    : (ret != null ? 'pos' : ''));
  setVal('kpi-dd', 'max DD ' + fmtPct(o.max_drawdown_pct == null ? null
    : -Math.abs(o.max_drawdown_pct)));
  setVal('kpi-open', String(o.open_positions == null ? '-' : o.open_positions));
  const trades = S.last && S.last.trades ? S.last.trades.trades || [] : [];
  const closed = trades.filter((t) => t.status === 'CLOSED');
  const wins = closed.filter((t) => (t.pnl_tmn || 0) > 0).length;
  setVal('kpi-trades', trades.length + ' trades'
    + (closed.length ? ' / ' + Math.round(wins / closed.length * 100)
      + '% win' : ''));
  const dec = S.last && S.last.decisions ? S.last.decisions.decisions || [] : [];
  const b = dec.filter((d) => d.final_action === 'BUY').length;
  const h = dec.filter((d) => d.final_action === 'HOLD').length;
  const s = dec.filter((d) => d.final_action === 'SELL').length;
  setVal('kpi-decisions', String(dec.length));
  setVal('kpi-decisions-break', 'B/H/S ' + b + '/' + h + '/' + s);
  const note = $('foot-paper-note');
  if (note) note.textContent = (o.mode === 'PAPER')
    ? 'DRY_RUN: paper results are optimistic (fills at book price, no queue position, no market impact).'
    : '';
  const dbfoot = $('foot-db');
  if (dbfoot) dbfoot.textContent = 'read-only dashboard; DB: ' + (o.run_id == null
    ? '-' : 'run ' + o.run_id);
}

/* ---------------- equity (plain SVG, no animation) ---------------- */
function renderEquity(eq) {
  const svg = $('equity-svg');
  const box = $('equity-chart');
  const empty = $('equity-empty');
  if (!svg || !box || !empty) return;
  const pts = (eq.points || []).filter((p) => p.value != null);
  if (!pts.length) {
    svg.classList.add('hidden');
    empty.classList.remove('hidden');
    return;
  }
  svg.classList.remove('hidden');
  empty.classList.add('hidden');
  const w = Math.max(300, box.clientWidth || 600);
  const h = 220;
  svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  const values = pts.map((p) => Number(p.value));
  const min = Math.min.apply(null, values);
  const max = Math.max.apply(null, values);
  const span = (max - min) || Math.abs(max) || 1;
  const pad = 8;
  const x = (i) => pad + i * (w - 2 * pad) / Math.max(1, pts.length - 1);
  const y = (v) => pad + (1 - (v - min) / span) * (h - 2 * pad);
  let line = '';
  let area = 'M ' + x(0) + ' ' + (h - pad) + ' ';
  pts.forEach((p, i) => {
    const px = x(i).toFixed(1), py = y(p.value).toFixed(1);
    line += (i ? 'L ' : 'M ') + px + ' ' + py + ' ';
    area += 'L ' + px + ' ' + py + ' ';
  });
  area += 'L ' + x(pts.length - 1).toFixed(1) + ' ' + (h - pad) + ' Z';
  svg.innerHTML =
    '<defs><linearGradient id="eqgrad" x1="0" y1="0" x2="0" y2="1">' +
    '<stop offset="0" stop-color="var(--accent)" stop-opacity="0.35"/>' +
    '<stop offset="1" stop-color="var(--accent)" stop-opacity="0.02"/>' +
    '</linearGradient></defs>' +
    '<path d="' + area + '" fill="url(#eqgrad)"/>' +
    '<path d="' + line + '" fill="none" stroke="var(--accent)" stroke-width="2"/>';
  const range = $('equity-range');
  if (range) range.textContent = fmtNum(min) + ' .. ' + fmtNum(max)
    + ' TMN (' + pts.length + ' pts)';
}

async function toggleDecisionDetail(id, cardEl) {
  const box = $('dec-detail-' + id);
  if (!box) return;
  if (!box.classList.contains('hidden')) {
    box.classList.add('hidden');
    return;
  }
  box.classList.remove('hidden');
  if (!box.innerHTML) {
    box.innerHTML = '<div class="dim">loading detail...</div>';
    try {
      const detail = await fetchJson('/api/decision/' + id);
      renderDecisionDetail(detail, box);
    } catch (e) {
      box.innerHTML = '<div class="guard-fail">detail failed: '
        + esc(e.message) + '</div>';
    }
  }
}

function renderDecisionDetail(detail, box) {
  const d = detail.decision || {};
  const models = detail.models || [];
  const rules = detail.guard_rules || [];
  const orders = detail.orders || [];
  const failed = rules.filter((r) => !r.passed);
  let html = '';
  html += '<div class="' + (failed.length ? 'guard-fail' : 'guard-pass')
    + '">guard: ' + (rules.length ? (failed.length ? 'REJECTED' : 'PASSED')
      : 'not consulted') + '</div>';
  rules.forEach((r) => {
    html += '<div class="' + (r.passed ? 'guard-pass' : 'guard-fail') + '">'
      + '[' + (r.passed ? 'PASS' : 'FAIL') + '] ' + esc(r.rule)
      + ' value=' + fmtNum(r.value, 4) + ' threshold=' + fmtNum(r.threshold, 4)
      + (r.message ? ' ' + esc(r.message) : '') + '</div>';
  });
  models.forEach((m) => {
    html += '<div style="margin-top:8px"><strong>' + esc(m.model) + '</strong>'
      + ' <span class="dim">' + (m.valid ? 'valid' : 'invalid')
      + (m.latency_ms != null ? ', ' + Math.round(m.latency_ms) + ' ms' : '')
      + (m.prompt_tokens != null ? ', ' + m.prompt_tokens + '+'
        + m.completion_tokens + ' tokens' : '')
      + '</span></div>';
    if (m.error) {
      html += '<div class="guard-fail" dir="auto">error: ' + esc(m.error)
        + '</div>';
    }
    html += '<details><summary>full raw response' + (m.raw_truncated
      ? ' (capped)' : '') + '</summary>'
      + '<div class="rawbox" dir="auto">' + esc(m.raw || '(empty)') + '</div>'
      + '<button class="btn" type="button" data-copy="1">Copy raw</button>'
      + '</details>';
  });
  if (orders.length) {
    html += '<div class="dim" style="margin-top:8px">orders:</div>';
    orders.forEach((o) => {
      html += '<div class="dim">' + esc(o.side) + ' ' + esc(o.type) + ' '
        + fmtNum(o.quantity, 6) + ' @ ' + fmtNum(o.price, 2)
        + ' status=' + esc(o.status || '-') + ' (row ' + o.id + ')</div>';
    });
  }
  box.innerHTML = html;
  box.querySelectorAll('button[data-copy]').forEach((btn) => {
    btn.addEventListener('click', (ev) => {
      ev.stopPropagation();
      const raw = btn.parentElement.querySelector('.rawbox');
      if (!raw) return;
      const text = raw.textContent || '';
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          btn.textContent = 'Copied';
          setTimeout(() => { btn.textContent = 'Copy raw'; }, 1200);
        }).catch(() => { btn.textContent = 'Copy failed'; });
      } else {
        btn.textContent = 'Copy unavailable';
      }
    });
  });
}
function renderGuardian(g) {
  const list = $('guardian-list');
  const empty = $('guardian-empty');
  const hb = $('guardian-hb');
  if (!list || !empty) return;
  const symbols = g.symbols || {};
  list.innerHTML = '';
  const names = Object.keys(symbols);
  if (!names.length) { empty.classList.remove('hidden'); }
  else { empty.classList.add('hidden'); }
  if (hb) {
    const age = g.last_heartbeat_age_sec;
    hb.textContent = age == null ? 'no heartbeat'
      : 'heartbeat ' + fmtAge(age) + ' ago';
    hb.style.color = age == null ? 'var(--muted)'
      : (age > 120 ? 'var(--danger-text)'
        : (age > 60 ? 'var(--amber)' : 'var(--success-text)'));
  }
  names.forEach((name) => {
    const w = symbols[name];
    const item = document.createElement('div');
    item.className = 'dec-card';
    item.id = 'gw-' + name;
    const bid = w.best_bid;
    let gauge = '';
    if (w.move_fraction_of_threshold != null) {
      const frac = Math.max(0, Math.min(100,
        w.move_fraction_of_threshold * 100));
      gauge = '<div class="confbar" style="width:100%" title="move '
        + fmtPct(w.observed_move_pct_signed) + ' of trigger '
        + fmtPct((w.trigger_move_pct || 0) * 100) + '">'
        + '<i style="width:' + frac.toFixed(0) + '%;background:'
        + (w.trigger_fired ? 'var(--danger)' : 'var(--accent)') + '"></i></div>';
    }
    let posLine = '';
    if (w.has_position) {
      const entry = w.entry, sl = w.sl_price, tp = w.tp_price;
      let pct = null;
      if (sl != null && tp != null && bid != null && tp > sl) {
        pct = Math.max(0, Math.min(100, (bid - sl) / (tp - sl) * 100));
      }
      posLine = '<div class="confbar" style="width:100%;margin-top:6px" '
        + 'title="price between SL and TP"><i style="width:'
        + (pct == null ? 0 : pct).toFixed(1) + '%"></i></div>'
        + '<div class="dim">entry ' + fmtNum(entry, 2)
        + ' | SL ' + fmtNum(sl, 2) + ' (' + fmtPct(w.distance_to_sl_pct) + ')'
        + ' | TP ' + fmtNum(tp, 2) + ' (' + fmtPct(w.distance_to_tp_pct) + ')'
        + '</div>';
    }
    item.innerHTML =
      '<div class="row1">' +
        '<strong>' + esc(name) + '</strong>' +
        (bid != null ? '<span>bid ' + fmtNum(bid, 2) + '</span>'
                     : '<span class="dim">bid -</span>') +
        (w.has_position
          ? '<span class="badge" style="color:var(--accent-light)">position</span>'
          : '<span class="dim">no position</span>') +
        (w.trigger_fired
          ? '<span class="badge" style="color:var(--danger-text)">TRIGGERED</span>'
          : '') +
        (w.cooldown_remaining_sec != null
          ? '<span class="dim">cooldown '
            + Math.round(w.cooldown_remaining_sec) + 's</span>' : '') +
        (w.age_sec != null ? '<span class="dim">age '
          + fmtAge(w.age_sec) + '</span>' : '') +
      '</div>' +
      gauge +
      '<div class="dim" style="margin-top:4px">move '
        + fmtPct(w.observed_move_pct_signed) + ' / trigger '
        + fmtPct((w.trigger_move_pct || 0) * 100) + ' (window '
        + (w.trigger_window_sec || '-') + 's)</div>' +
      posLine;
    list.appendChild(item);
  });
  const triggers = g.last_triggers || [];
  if (triggers.length) {
    const t = document.createElement('div');
    t.className = 'dim';
    t.textContent = 'last trigger: ' + (triggers[0].message || '');
    list.appendChild(t);
  }
}
/* ---------------- open positions ---------------- */
function renderPositions(p) {
  const list = $('positions-list');
  const empty = $('positions-empty');
  if (!list || !empty) return;
  const positions = p.positions || [];
  list.innerHTML = '';
  if (!positions.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  positions.forEach((pos) => {
    const bid = pos.last_known_bid;
    const entry = pos.entry_price;
    const sl = pos.stop_price, tp = pos.target_price;
    // progress: where the price sits between SL and TP
    let pct = null;
    if (sl != null && tp != null && bid != null && tp > sl) {
      pct = Math.max(0, Math.min(100, (bid - sl) / (tp - sl) * 100));
    }
    const pnl = pos.unrealized_pnl_tmn;
    const pnlPct = pos.unrealized_pnl_pct;
    const item = document.createElement('div');
    item.className = 'dec-card';
    item.id = 'pos-' + pos.id;
    item.innerHTML =
      '<div class="row1">' +
        '<strong>' + esc(pos.symbol) + '</strong>' +
        '<span class="badge">' + esc(pos.status) + '</span>' +
        '<span class="dim">entry ' + fmtNum(entry, 2) + '</span>' +
        '<span class="dim">qty ' + fmtNum(pos.quantity, 6) + '</span>' +
        (bid != null ? '<span class="dim">bid ' + fmtNum(bid, 2) + '</span>'
                     : '<span class="dim">bid -</span>') +
        (pnl != null
          ? '<span class="' + (pnl >= 0 ? 'pnl-pos' : 'pnl-neg') + '">' +
            fmtNum(pnl) + ' TMN (' + fmtPct(pnlPct) + ')</span>'
          : '') +
      '</div>' +
      '<div class="confbar" style="width:100%;margin-top:8px" title="' +
        (pct == null ? 'price unknown' : 'price position between SL and TP') +
        '"><i style="width:' + (pct == null ? 0 : pct).toFixed(1) + '%;' +
        'background:var(--accent)"></i></div>' +
      '<div class="dim" style="margin-top:4px">SL ' + fmtNum(sl, 2) +
        ' | TP ' + fmtNum(tp, 2) +
        (bid != null && entry ? ' | dist SL ' +
          ((bid - sl) / entry * 100).toFixed(2) + '% / TP ' +
          ((tp - bid) / entry * 100).toFixed(2) + '%' : '') +
      '</div>';
    list.appendChild(item);
  });
}
/* ---------------- decision feed ---------------- */
function decisionMatches(d) {
  const f = S.filters;
  if (f.symbol && d.symbol !== f.symbol) return false;
  if (f.action && d.final_action !== f.action) return false;
  if (f.onlySignals && d.final_action === 'HOLD') return false;
  if (f.search) {
    const needle = f.search.toLowerCase();
    const hay = ((d.symbol || '') + ' ' + (d.reason || '') + ' '
      + (d.decision_uid || '') + ' ' + (d.outcome || '')).toLowerCase();
    if (hay.indexOf(needle) === -1) return false;
  }
  return true;
}

function voteHtml(v) {
  const conf = v.confidence == null ? 0 : Math.round(v.confidence * 100);
  const cls = v.action === 'BUY' ? 'pnl-pos' : (v.action === 'SELL'
    ? 'pnl-neg' : 'dim');
  return '<div class="vote-line">' +
    '<span class="' + cls + '" style="min-width:44px">' + esc(v.action) + '</span>' +
    '<strong>' + esc(v.model) + '</strong>' +
    '<span class="confbar" title="confidence ' + conf + '%">' +
      '<i style="width:' + conf + '%"></i></span>' +
    '<span class="dim">' + conf + '%</span>' +
    (v.stop_pct != null
      ? '<span class="dim">stop ' + Number(v.stop_pct).toFixed(3) +
        ' target ' + Number(v.target_pct).toFixed(3) + '</span>' : '') +
    '</div>' +
    (v.reason
      ? '<div class="reason" dir="auto">' + esc(v.reason) + '</div>' : '') +
    (v.error ? '<div class="guard-fail" dir="auto">error: '
      + esc(v.error) + '</div>' : '');
}

function renderDecisions(d) {
  const list = $('decisions-list');
  const empty = $('decisions-empty');
  if (!list || !empty) return;
  wireDecisionFilters(d);
  const all = d.decisions || [];
  const shown = all.filter(decisionMatches);
  list.innerHTML = '';
  if (!shown.length) {
    empty.classList.remove('hidden');
    empty.textContent = all.length ? 'No decisions match the filters.'
      : 'No decisions yet.';
    return;
  }
  empty.classList.add('hidden');
  shown.forEach((d0) => {
    const votes = d0.votes || [];
    const act = d0.final_action;
    const item = document.createElement('div');
    item.className = 'dec-card';
    item.id = 'dec-' + d0.id;
    const actClass = act === 'BUY' ? 'pnl-pos' : (act === 'SELL' ? 'pnl-neg'
      : 'dim');
    item.innerHTML =
      '<div class="row1">' +
        '<span class="dec-time">' + esc((d0.ts || '').replace('T', ' ')
          .slice(0, 19)) + '</span>' +
        '<strong>' + esc(d0.symbol) + '</strong>' +
        '<span class="badge">' + esc(d0.source || '-') + '</span>' +
        '<span class="' + actClass + '" style="font-weight:700">' +
          esc(act) + '</span>' +
        '<span class="dim">' + esc(d0.outcome || '') + '</span>' +
        '<span class="dim">uid ' + esc((d0.decision_uid || '').slice(0, 8)) +
          '</span>' +
        (d0.guard_allowed === 0
          ? '<span class="guard-fail">guard rejected</span>' : '') +
      '</div>' +
      votes.map(voteHtml).join('') +
      '<div class="detail hidden" id="dec-detail-' + d0.id + '"></div>';
    item.addEventListener('click', () => toggleDecisionDetail(d0.id, item));
    list.appendChild(item);
  });
}

let filtersWired = false;
function wireDecisionFilters(d) {
  if (filtersWired) return;
  filtersWired = true;
  const search = $('search');
  if (search) search.addEventListener('input', () => {
    S.filters.search = search.value || '';
    if (S.last) renderDecisions(S.last.decisions || {});
  });
  const only = $('only-signals');
  if (only) only.addEventListener('change', () => {
    S.filters.onlySignals = only.checked;
    if (S.last) renderDecisions(S.last.decisions || {});
  });
}

function actionChip(action) {
  const chips = $('action-chips');
  if (!chips) return;
  const el = document.createElement('span');
  el.className = 'chip' + (S.filters.action === action ? ' on' : '');
  el.textContent = action;
  el.addEventListener('click', () => {
    S.filters.action = S.filters.action === action ? null : action;
    if (S.last) renderDecisions(S.last.decisions || {});
  });
  chips.appendChild(el);
}
['BUY', 'HOLD', 'SELL'].forEach(actionChip);

function symbolChip(symbol) {
  const chips = $('symbol-chips');
  if (!chips || chips.querySelector('[data-sym="' + esc(symbol) + '"]')) return;
  const el = document.createElement('span');
  el.className = 'chip' + (S.filters.symbol === symbol ? ' on' : '');
  el.setAttribute('data-sym', symbol);
  el.textContent = symbol;
  el.addEventListener('click', () => {
    S.filters.symbol = S.filters.symbol === symbol ? null : symbol;
    if (S.last) renderDecisions(S.last.decisions || {});
  });
  chips.appendChild(el);
}
function refreshSymbolChips(symbols) { symbols.forEach(symbolChip); }
function renderTrades(t) {
  const body = $('trades-body');
  const empty = $('trades-empty');
  const table = $('trades-table');
  if (!body || !empty || !table) return;
  const trades = t.trades || [];
  body.innerHTML = '';
  if (!trades.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  const key = S.tradesSort.key;
  let rows = trades.slice();
  if (key) {
    const dir = S.tradesSort.dir;
    rows.sort((a, b) => {
      const av = a[key], bv = b[key];
      if (av == null) return 1;
      if (bv == null) return -1;
      if (typeof av === 'string') return av.localeCompare(bv) * dir;
      return (av - bv) * dir;
    });
  }
  rows.forEach((tr) => {
    const main = document.createElement('tr');
    main.className = 'tr-main';
    main.setAttribute('data-trade', tr.id);
    const pnl = tr.pnl_tmn;
    const pnlCls = pnl == null ? '' : (pnl >= 0 ? 'pnl-pos' : 'pnl-neg');
    main.innerHTML =
      '<td>' + esc(tr.symbol) + '</td>' +
      '<td>' + esc((tr.opened_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
      '<td>' + esc((tr.closed_at || '').replace('T', ' ').slice(0, 19)) + '</td>' +
      '<td class="num">' + fmtNum(tr.entry_price, 2) + '</td>' +
      '<td class="num">' + fmtNum(tr.close_price, 2) + '</td>' +
      '<td class="num ' + pnlCls + '">' + fmtNum(pnl) + '</td>' +
      '<td class="num ' + pnlCls + '">' + fmtPct(tr.pnl_pct) + '</td>' +
      '<td><span class="badge">' + esc(tr.close_reason || tr.status) + '</span></td>';
    main.addEventListener('click', () => toggleTradeDetail(tr, body));
    body.appendChild(main);
  });
  if (!table.getAttribute('data-sort-wired')) {
    table.setAttribute('data-sort-wired', '1');
    table.querySelectorAll('th[data-sort]').forEach((th) => {
      th.addEventListener('click', () => {
        const k = th.getAttribute('data-sort');
        if (S.tradesSort.key === k) { S.tradesSort.dir *= -1; }
        else { S.tradesSort.key = k; S.tradesSort.dir = 1; }
        if (S.last) renderTrades(S.last.trades || {});
      });
    });
  }
}

function toggleTradeDetail(tr, bodyEl) {
  const existing = bodyEl.querySelector('[data-detail="' + tr.id + '"]');
  if (existing) { existing.remove(); return; }
  const row = document.createElement('tr');
  row.className = 'tr-detail';
  row.setAttribute('data-detail', tr.id);
  const votes = tr.entry_votes || [];
  let html = '<td colspan="8"><div class="dim">entry decision '
    + esc((tr.decision_uid || '').slice(0, 8)) + ' votes:</div>';
  votes.forEach((v) => {
    html += '<div class="vote-line"><span class="dim" style="min-width:44px">'
      + esc(v.action) + '</span><strong>' + esc(v.model) + '</strong>'
      + '<span class="dim">conf ' + (v.confidence == null ? '-'
        : Math.round(v.confidence * 100) + '%') + '</span></div>'
      + (v.reason ? '<div class="reason" dir="auto">' + esc(v.reason)
        + '</div>' : '');
  });
  if (!votes.length) html += '<div class="dim">(no vote summary stored)</div>';
  html += '</td>';
  row.innerHTML = html;
  const main = bodyEl.querySelector('[data-trade="' + tr.id + '"]');
  if (main && main.nextSibling) { bodyEl.insertBefore(row, main.nextSibling); }
  else { bodyEl.appendChild(row); }
}
function renderGuardRejections(g) {
  const list = $('guardrej-list');
  const empty = $('guardrej-empty');
  if (!list || !empty) return;
  const rejections = g.rejections || [];
  list.innerHTML = '';
  if (!rejections.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  rejections.forEach((r) => {
    const row = document.createElement('div');
    row.className = 'bar-row' + (r.highlight ? ' hl' : '');
    row.innerHTML =
      '<span class="bar-name" title="' + esc(r.rule) + '">' + esc(r.rule) + '</span>'
      + '<span class="bar-track"><span class="bar-fill" style="width:'
      + (r.pct || 0) + '%"></span></span>'
      + '<span class="bar-count">' + r.count + ' (' + (r.pct || 0) + '%)</span>';
    list.appendChild(row);
  });
}

function renderLlm(l) {
  const body = $('llm-body');
  const empty = $('llm-empty');
  if (!body || !empty) return;
  const models = l.models || [];
  body.innerHTML = '';
  if (!models.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  models.forEach((m) => {
    const tr = document.createElement('tr');
    tr.innerHTML =
      '<td>' + esc(m.model) + '</td>' +
      '<td class="num">' + m.calls + '</td>' +
      '<td class="num">' + m.errors + ' (' + Math.round(m.error_rate * 100)
        + '%)</td>' +
      '<td class="num">' + (m.p95_latency_ms == null ? '-'
        : Math.round(m.p95_latency_ms)) + '</td>' +
      '<td class="num">' + fmtNum(m.tokens) + '</td>';
    body.appendChild(tr);
  });
}

function renderEvents(e) {
  const list = $('events-list');
  const empty = $('events-empty');
  const kindSel = $('events-kind');
  if (!list || !empty) return;
  const events = e.events || [];
  const currentKind = kindSel ? kindSel.value : '';
  if (kindSel && !kindSel.getAttribute('data-wired')) {
    kindSel.setAttribute('data-wired', '1');
    const kinds = Array.from(new Set(events.map((ev) => ev.kind)
      .filter(Boolean))).sort();
    kinds.forEach((k) => {
      const opt = document.createElement('option');
      opt.value = k;
      opt.textContent = k;
      kindSel.appendChild(opt);
    });
    kindSel.addEventListener('change', () => {
      if (!S.last) return;
      const q = kindSel.value ? ('/api/events?limit=200&kind='
        + encodeURIComponent(kindSel.value)) : '/api/events?limit=200';
      fetchJson(q).then((res) => renderEvents(res))
        .catch(() => { /* keep old */ });
    });
  }
  list.innerHTML = '';
  if (!events.length) { empty.classList.remove('hidden'); return; }
  empty.classList.add('hidden');
  events.forEach((ev) => {
    const line = document.createElement('div');
    line.className = 'ev-line ev-' + esc(ev.level);
    line.id = 'ev-' + ev.id;
    line.innerHTML = esc((ev.ts || '').slice(11, 19)) + ' '
      + '<span class="ev-kind">[' + esc(ev.kind) + ']</span> '
      + esc(ev.message || '');
    list.appendChild(line);
  });
  const auto = $('events-autoscroll');
  if (auto && auto.checked) list.scrollTop = list.scrollHeight;
}

function renderLogtail(t) {
  const box = $('logtail-lines');
  const empty = $('logtail-empty');
  if (!box || !empty) return;
  const lines = t.lines || [];
  if (!lines.length) {
    empty.textContent = t.warning || 'Log tail unavailable.';
    empty.classList.remove('hidden');
    box.innerHTML = '';
    return;
  }
  empty.classList.add('hidden');
  const text = lines.map(esc).join('\n');
  if (box.textContent !== text) box.textContent = text;
  const auto = $('logtail-autoscroll');
  if (auto && auto.checked) box.scrollTop = box.scrollHeight;
}
