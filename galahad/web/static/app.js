'use strict';
/* GALAHAD dashboard — vanilla JS, no external requests. Every value shown comes from the local engine API.
   Text is always inserted with textContent (never innerHTML) so news titles / emails can't inject markup. */

// ───────────────────────────── helpers ─────────────────────────────
const $ = (s, r = document) => r.querySelector(s);
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
    else if (k in el && k !== 'list' && k !== 'form') el[k] = v;
    else el.setAttribute(k, v === true ? '' : v);
  }
  const add = (c) => { if (c == null || c === false) return; if (Array.isArray(c)) return c.forEach(add); el.append(c.nodeType ? c : document.createTextNode(String(c))); };
  kids.forEach(add);
  return el;
}
const clear = (el) => { while (el.firstChild) el.removeChild(el.firstChild); return el; };
const fmtTs = (t, o) => t ? new Date(t * 1000).toLocaleString([], o || { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' }) : '';
const fmtBytes = (n) => { n = Number(n) || 0; const u = ['B', 'KB', 'MB', 'GB', 'TB']; let i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; };
const fmtDur = (s) => { s = Math.floor(s); const d = Math.floor(s / 86400), hr = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60); return (d ? d + 'd ' : '') + (hr || d ? hr + 'h ' : '') + m + 'm'; };
const ago = (t) => { const s = Date.now() / 1000 - t; return s < 60 ? 'just now' : s < 3600 ? Math.floor(s / 60) + ' min ago' : s < 86400 ? Math.floor(s / 3600) + ' h ago' : Math.floor(s / 86400) + ' d ago'; };
const pct = (v) => Math.max(0, Math.min(100, Number(v) || 0));

async function api(path, opts = {}) {
  const o = { method: opts.method || 'GET', headers: { 'X-Galahad': '1' }, credentials: 'same-origin' };
  if (opts.body !== undefined) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(opts.body); }
  let r;
  try { r = await fetch(path, o); } catch (e) { throw new Error('The GALAHAD engine is not reachable.'); }
  if (r.status === 401) throw new Error('Session expired — reopen the dashboard from GALAHAD.');
  let j = null; try { j = await r.json(); } catch (e) { /* non-JSON */ }
  if (!r.ok) throw new Error((j && (j.message || j.error || j.detail)) || ('HTTP ' + r.status));
  return j;
}
const post = (p, body) => api(p, { method: 'POST', body: body || {} });
async function cmd(plugin, command, args) {
  const r = await post('/api/command', { plugin, command, args: args || {} });
  if (r.status === 'awaiting_confirmation') { toast('Waiting for your confirmation — see the banner above.', ''); refreshBanner(); }
  return r;
}
function toast(msg, kind) {
  const t = h('div', { class: 'toast ' + (kind || '') }, msg);
  $('#toasts').append(t); setTimeout(() => t.remove(), kind === 'err' ? 8000 : 4500);
}
const report = (r, okMsg) => toast(r.message || okMsg || (r.ok ? 'Done.' : 'Failed.'), r.status === 'awaiting_confirmation' ? '' : (r.ok ? 'ok' : 'err'));

// state helper: show loading, then render or error
async function load(root, promise, render) {
  clear(root).append(h('div', { class: 'loading' }, 'Loading'));
  try { const d = await promise; clear(root); render(d); }
  catch (e) { clear(root).append(h('div', { class: 'err-state' }, '⚠ ' + e.message, ' ', h('button', { class: 'sm', onclick: () => load(root, promise, render) }, 'Retry'))); }
}
const emptyState = (t) => h('div', { class: 'empty' }, t);
function modal(title, body, actions) {
  const root = $('#modal-root'), bg = h('div', { class: 'modal-bg', onclick: (e) => { if (e.target === bg) close(); } });
  const close = () => bg.remove();
  bg.append(h('div', { class: 'modal', role: 'dialog', 'aria-label': title }, h('h3', {}, title), body, h('div', { class: 'row', style: { marginTop: '16px', justifyContent: 'flex-end' } }, actions ? actions(close) : h('button', { onclick: close }, 'Close'))));
  root.append(bg); return close;
}
function field(label, input) { return h('div', { class: 'field' }, h('label', {}, label), input); }
function kv(obj) { const d = h('dl', { class: 'kv' }); for (const [k, v] of Object.entries(obj)) d.append(h('dt', {}, k), h('dd', {}, v == null || v === '' ? '—' : v)); return d; }
function statusTag(s) { const c = { completed: 'ok', ok: 'ok', active: 'ok', running: 'acc', awaiting_confirmation: 'warn', pending: 'warn', failed: 'err', error: 'err', cancelled: '', degraded: 'warn', unconfigured: 'warn', disabled: '', unloaded: '' }[s] || ''; return h('span', { class: 'tag ' + c }, s.replace('_', ' ')); }

function spark(canvas, series, color, max) {
  const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth, hgt = canvas.clientHeight;
  canvas.width = w * dpr; canvas.height = hgt * dpr;
  const c = canvas.getContext('2d'); c.scale(dpr, dpr); c.clearRect(0, 0, w, hgt);
  const css = getComputedStyle(document.documentElement);
  c.strokeStyle = css.getPropertyValue('--border'); c.lineWidth = 1;
  for (let i = 1; i < 4; i++) { c.beginPath(); c.moveTo(0, hgt * i / 4); c.lineTo(w, hgt * i / 4); c.stroke(); }
  if (series.length < 2) return;
  const m = max || Math.max(1, ...series), step = w / (series.length - 1);
  const pts = series.map((v, i) => [i * step, hgt - 3 - (hgt - 8) * Math.min(v, m) / m]);
  c.beginPath(); pts.forEach(([x, y], i) => i ? c.lineTo(x, y) : c.moveTo(x, y));
  c.strokeStyle = color || css.getPropertyValue('--accent'); c.lineWidth = 1.8; c.stroke();
  c.lineTo(w, hgt); c.lineTo(0, hgt); c.closePath(); c.globalAlpha = .12; c.fillStyle = c.strokeStyle; c.fill(); c.globalAlpha = 1;
}

// ───────────────────────────── shell / router ─────────────────────────────
const ICONS = {
  home: 'M3 11l9-8 9 8v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z', assistant: 'M12 3a4 4 0 0 0-4 4v4a4 4 0 0 0 8 0V7a4 4 0 0 0-4-4zM5 11a7 7 0 0 0 14 0M12 18v3',
  system: 'M4 5h16v11H4zM8 20h8M12 16v4', media: 'M9 18V5l11-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0zM20 16a3 3 0 1 1-6 0 3 3 0 0 1 6 0z',
  productivity: 'M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9', intelligence: 'M4 5h13v14H6a2 2 0 0 1-2-2zM17 9h3v8a2 2 0 0 1-2 2M8 9h5M8 13h5',
  finance: 'M12 3v18M16 7.5C16 6 14.2 5 12 5S8 6 8 7.7 9.8 10 12 10.7s4 1.1 4 3-1.8 2.3-4 2.3-4-1-4-2.5', documents: 'M7 3h7l5 5v13H7zM14 3v5h5',
  plugins: 'M10 3h4v4h3a2 2 0 0 1 2 2v3h-4a2 2 0 1 0 0 4h4v3a2 2 0 0 1-2 2h-3v-4a2 2 0 1 0-4 0v4H6a2 2 0 0 1-2-2v-3h4a2 2 0 1 0 0-4H4V9a2 2 0 0 1 2-2h4z',
  execution: 'M5 4l14 8-14 8z', notifications: 'M6 9a6 6 0 1 1 12 0c0 5 2 6 2 7H4c0-1 2-2 2-7zM10 20a2 2 0 0 0 4 0', activity: 'M3 12h4l3-8 4 16 3-8h4',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zM19.4 15l1.6 1-2 3.5-1.9-.7a7 7 0 0 1-1.7 1l-.3 2h-4l-.3-2a7 7 0 0 1-1.7-1l-1.9.7-2-3.5 1.6-1a7 7 0 0 1 0-2L3.2 11l2-3.5 1.9.7a7 7 0 0 1 1.7-1l.3-2h4l.3 2a7 7 0 0 1 1.7 1l1.9-.7 2 3.5-1.6 1a7 7 0 0 1 0 2z',
};
const icon = (n) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 24 24'); const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', ICONS[n] || ICONS.home); s.append(p); return s; };

const VIEWS = {};
const ORDER = ['home', 'assistant', 'system', 'media', 'productivity', 'intelligence', 'finance', 'documents', 'plugins', 'execution', 'notifications', 'activity', 'settings'];
const TITLES = { home: 'Home', assistant: 'Assistant', system: 'System Monitor', media: 'Media', productivity: 'Productivity', intelligence: 'Intelligence', finance: 'Finance', documents: 'Documents', plugins: 'Plugins', execution: 'AI Execution', notifications: 'Notifications', activity: 'Activity', settings: 'Settings' };
const S = { status: null, current: null, cleanups: [], badges: {} };

function buildNav() {
  const nav = clear($('#nav'));
  nav.append(h('div', { class: 'brand' }, (() => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg'); s.setAttribute('viewBox', '0 0 32 32'); const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', 'M16 4l10 5v8c0 5-4 8-10 11C10 25 6 22 6 17V9z'); p.setAttribute('fill', 'none'); p.setAttribute('stroke', 'var(--accent)'); p.setAttribute('stroke-width', '2'); s.append(p); return s; })(), h('span', {}, (S.status && S.status.settings['general.app_name']) || 'GALAHAD')));
  for (const id of ORDER) {
    const b = h('button', { class: 'nav-btn', id: 'nav-' + id, onclick: () => { location.hash = '#/' + id; } }, icon(id), h('span', {}, TITLES[id]), h('span', { class: 'badge', style: { display: 'none' }, id: 'badge-' + id }));
    nav.append(b);
  }
}
function setBadge(id, n, cls) { const b = $('#badge-' + id); if (!b) return; b.style.display = n ? '' : 'none'; b.textContent = n; b.className = 'badge ' + (cls || ''); }

function buildTop() {
  const top = clear($('#top'));
  const results = h('div', { id: 'results', style: { display: 'none' } });
  let timer;
  const input = h('input', { type: 'search', placeholder: 'Search files, apps, tasks, news, conversations…', 'aria-label': 'Unified search',
    oninput: () => { clearTimeout(timer); const q = input.value.trim(); if (q.length < 2) { results.style.display = 'none'; return; } timer = setTimeout(() => doSearch(q, results), 300); },
    onkeydown: (e) => { if (e.key === 'Escape') { results.style.display = 'none'; input.blur(); } } });
  document.addEventListener('click', (e) => { if (!$('#search').contains(e.target)) results.style.display = 'none'; });
  top.append(h('h1', { id: 'page-title' }, ''), h('div', { id: 'search' }, input, results), h('span', { class: 'spacer' }),
    h('span', { class: 'pill', id: 'ai-pill', title: 'Current AI provider and model' }, h('i', { class: 'dot' }), 'AI'),
    h('span', { class: 'pill', id: 'clock' }, ''),
    h('button', { class: 'ghost', title: 'Notifications', 'aria-label': 'Notifications', onclick: () => { location.hash = '#/notifications'; } }, icon('notifications'), h('span', { class: 'badge err', id: 'bell', style: { display: 'none', marginLeft: '4px' } })));
  $('#top svg') && ($('#top svg').style.cssText = 'width:20px;height:20px;stroke:currentColor;fill:none;stroke-width:1.7;vertical-align:middle');
}
async function doSearch(q, box) {
  clear(box).append(h('div', { class: 'loading', style: { padding: '12px' } }, 'Searching')); box.style.display = '';
  try {
    const r = await api('/api/search?q=' + encodeURIComponent(q)); clear(box);
    const res = (r.data && r.data.results) || [];
    if (!res.length) { box.append(h('div', { class: 'empty', style: { padding: '12px' } }, 'No results for “' + q + '”.')); return; }
    for (const x of res.slice(0, 40)) {
      box.append(h('div', { class: 'r', tabindex: 0, onclick: async () => {
        box.style.display = 'none';
        if (x.action) { const rr = await post('/api/command', { plugin: 'search', command: 'open_result', args: x.action }); report(rr); }
        else if (x.section) location.hash = '#/' + x.section;
      } }, h('span', { class: 't' }, x.type), h('div', {}, h('div', {}, x.title), h('div', { class: 'sub' }, x.snippet))));
    }
  } catch (e) { clear(box).append(h('div', { class: 'err-state', style: { padding: '12px' } }, e.message)); }
}
function tickClock() {
  const el = $('#clock'); if (!el) return;
  el.textContent = new Date().toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

function applyAppearance(st) {
  const s = (st && st.settings) || {};
  document.documentElement.dataset.theme = s['appearance.theme'] === 'light' ? 'light' : 'dark';
  if (s['appearance.accent']) document.documentElement.style.setProperty('--accent', s['appearance.accent']);
  document.documentElement.style.setProperty('--op', s['appearance.transparency'] ?? .85);
  document.body.classList.toggle('noanim', s['appearance.animations'] === false);
  const bg = s['appearance.background'] || '';
  if (/^https?:\/\//.test(bg) || /^\/static\//.test(bg)) { document.body.style.backgroundImage = `linear-gradient(rgba(7,11,20,.7),rgba(7,11,20,.7)),url("${bg.replace(/"/g, '')}")`; document.body.classList.add('bgimg'); }
  else if (/^#[0-9a-fA-F]{3,8}$/.test(bg)) { document.body.style.background = bg; document.body.classList.remove('bgimg'); }
  else { document.body.style.background = ''; document.body.classList.remove('bgimg'); }
  document.title = (s['general.app_name'] || 'GALAHAD') + ' — ' + (TITLES[S.current] || '');
}

async function refreshStatus() {
  try {
    const st = await api('/api/status'); S.status = st; applyAppearance(st);
    const p = $('#ai-pill');
    if (p) { clear(p).append(h('i', { class: 'dot ' + (st.ai.configured ? 'ok' : 'warn') }), st.ai.configured ? `${st.ai.provider} · ${st.ai.model || 'default'}` : 'No AI provider — offline mode'); }
    setBadge('notifications', st.unread_notifications, 'err');
    setBadge('execution', st.pending_confirmations + st.active_runs.length, st.pending_confirmations ? 'warn' : '');
    const bell = $('#bell'); if (bell) { bell.style.display = st.unread_notifications ? '' : 'none'; bell.textContent = st.unread_notifications; }
  } catch (e) { const p = $('#ai-pill'); if (p) clear(p).append(h('i', { class: 'dot err' }), 'engine offline'); }
}
async function refreshBanner() {
  try {
    const r = await api('/api/confirmations'), b = clear($('#banner'));
    for (const c of r.pending) {
      b.append(h('div', { class: 'confirm', role: 'alertdialog', 'aria-label': 'Confirmation needed' },
        h('div', { style: { flex: 1 } }, h('strong', {}, 'Confirmation needed · ' + c.plugin + '.' + c.command), h('pre', {}, c.summary), h('div', { class: 'sub' }, 'Expires in ' + Math.round(c.expires_in / 60) + ' min')),
        h('div', { class: 'row' },
          h('button', { class: 'primary', onclick: async (e) => { e.target.disabled = true; try { const x = await post('/api/confirmations/' + c.id + '/approve'); report(x); } catch (err) { toast(err.message, 'err'); } refreshBanner(); refreshStatus(); } }, 'Approve'),
          h('button', { class: 'danger', onclick: async () => { await post('/api/confirmations/' + c.id + '/deny'); toast('Cancelled — nothing was done.'); refreshBanner(); refreshStatus(); } }, 'Deny'))));
    }
  } catch (e) { /* banner is best-effort */ }
}

function ctxFor(root) {
  return { every(ms, fn) { const t = setInterval(() => { if (!document.hidden) fn(); }, ms); S.cleanups.push(() => clearInterval(t)); return t; }, root };
}
function route() {
  const id = (location.hash.replace(/^#\//, '') || 'home').split('?')[0];
  const view = VIEWS[id] ? id : 'home';
  S.cleanups.forEach((f) => { try { f(); } catch (e) { /* ignore */ } }); S.cleanups = [];
  S.current = view;
  document.querySelectorAll('.nav-btn').forEach((b) => b.classList.toggle('on', b.id === 'nav-' + view));
  $('#page-title').textContent = TITLES[view]; document.title = 'GALAHAD — ' + TITLES[view];
  const root = clear($('#view')); root.scrollTop = 0;
  try { VIEWS[view](root, ctxFor(root)); } catch (e) { root.append(h('div', { class: 'err-state' }, '⚠ ' + e.message)); }
}

// ───────────────────────────── HOME ─────────────────────────────
const HOME_WIDGETS = { clock: 'Time & greeting', weather: 'Weather', system: 'System', tasks: "Today's tasks", reminders: 'Reminders', news: 'Headlines', notifications: 'Notifications', quick: 'Quick actions', assistant: 'Assistant status' };
VIEWS.home = (root, ctx) => {
  const grid = h('div', { class: 'grid' }), tools = h('div', { class: 'row', style: { marginBottom: '14px' } });
  root.append(tools, grid);
  const cards = {};
  const mk = (id, title, body, extra) => { const c = h('div', { class: 'card ' + (extra || ''), id: 'w-' + id }, h('h2', {}, title), body); cards[id] = c; return c; };
  const clockBody = h('div', {}), wxBody = h('div', {}, h('div', { class: 'loading' }, 'Loading')), sysBody = h('div', {}), taskBody = h('div', {}), remBody = h('div', {}), newsBody = h('div', {}), notifBody = h('div', {}), asstBody = h('div', {});
  const quick = h('div', { class: 'row' }, ...[['Daily briefing', () => runChat('Give me my daily briefing')], ['Today’s news', () => { location.hash = '#/intelligence'; }], ['Add task', () => { location.hash = '#/productivity'; }], ['Ask JARVIS', () => { location.hash = '#/assistant'; }], ['Take a screenshot', () => cmd('computer', 'screenshot').then((r) => report(r))]].map(([t, f]) => h('button', { onclick: f }, t)));
  async function runChat(m) { toast('Working…'); const r = await post('/api/chat', { message: m }); modal('Daily briefing', h('div', { class: 'report' }, r.reply)); }
  const order = ['clock', 'weather', 'system', 'tasks', 'reminders', 'news', 'notifications', 'assistant', 'quick'];
  const defs = { clock: () => mk('clock', 'Now', clockBody), weather: () => mk('weather', 'Weather', wxBody), system: () => mk('system', 'System', sysBody), tasks: () => mk('tasks', 'Tasks', taskBody),
    reminders: () => mk('reminders', 'Upcoming', remBody), news: () => mk('news', 'Headlines', newsBody, 'span2'), notifications: () => mk('notifications', 'Notifications', notifBody), assistant: () => mk('assistant', 'Assistant', asstBody), quick: () => mk('quick', 'Quick actions', quick) };
  const layout = () => ((S.status && S.status.settings['appearance.layout']) || {}).home || order;
  const build = () => { clear(grid); for (const id of order) if (layout().includes(id)) grid.append(defs[id]()); fill(); };
  tools.append(h('button', { class: 'sm ghost', onclick: () => customise() }, '⚙ Customise widgets'));
  function customise() {
    const cur = new Set(layout()), boxes = {};
    const body = h('div', { class: 'col' }, ...Object.entries(HOME_WIDGETS).map(([id, label]) => h('label', { class: 'switch' }, boxes[id] = h('input', { type: 'checkbox', checked: cur.has(id) }), label)));
    modal('Home widgets', body, (close) => [h('button', { onclick: close }, 'Cancel'), h('button', { class: 'primary', onclick: async () => { const ids = order.filter((i) => boxes[i].checked); await post('/api/settings', { key: 'appearance.layout', value: { home: ids } }); await refreshStatus(); close(); build(); } }, 'Save')]);
  }
  function fill() {
    const now = new Date(), hr = now.getHours(), greet = hr < 12 ? 'Good morning' : hr < 18 ? 'Good afternoon' : 'Good evening';
    clear(clockBody).append(h('div', { class: 'big' }, now.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })), h('div', { class: 'sub' }, now.toLocaleDateString([], { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' })), h('div', { style: { marginTop: '8px' } }, greet + '.'));
    clear(asstBody).append(h('div', { class: 'col' }, kv({ Provider: S.status ? `${S.status.ai.provider} · ${S.status.ai.model || 'default'}` : '', Mode: S.status && S.status.ai.configured ? 'AI planning' : 'Offline command router', Voice: S.status && S.status.voice.available ? ('desktop app connected · ' + S.status.voice.mode) : 'no desktop voice session in this process', 'Active runs': S.status ? S.status.active_runs.length : 0 }), h('button', { onclick: () => { location.hash = '#/assistant'; } }, 'Open assistant')));
  }
  build();
  async function data() {
    try {
      const d = await api('/api/home');
      const s = d.system;
      clear(sysBody).append(s ? h('div', { class: 'col' },
        meter('CPU', s.cpu), meter('RAM', s.ram.percent, fmtBytes(s.ram.used) + ' / ' + fmtBytes(s.ram.total)),
        s.battery ? meter('Battery', s.battery.percent, s.battery.charging ? 'charging' : 'on battery', true) : h('div', { class: 'sub' }, 'No battery detected'),
        h('div', { class: 'sub' }, 'Network: ' + (s.network.connected ? 'connected' : 'offline') + ' · up ' + fmtDur(s.uptime_seconds))) : emptyState('System monitor unavailable.'));
      clear(taskBody).append(d.tasks.length ? h('div', { class: 'list' }, d.tasks.map((t) => h('div', { class: 'item' }, h('input', { type: 'checkbox', 'aria-label': 'Complete ' + t.title, onchange: async () => { report(await cmd('productivity', 'complete_task', { id: t.id })); data(); } }), h('div', { class: 'grow' }, h('div', { class: 'title' }, t.title), t.due ? h('div', { class: 'meta' }, 'due ' + fmtTs(t.due)) : null)))) : emptyState('Nothing pending. 🎉'));
      const up = [...d.reminders.map((r) => ({ t: r.due, l: r.text, k: 'reminder' })), ...d.events.map((e) => ({ t: e.start, l: e.title, k: 'event' }))].sort((a, b) => a.t - b.t).slice(0, 6);
      clear(remBody).append(up.length ? h('div', { class: 'list' }, up.map((u) => h('div', { class: 'item' }, h('span', { class: 'tag acc' }, u.k), h('div', { class: 'grow' }, h('div', {}, u.l), h('div', { class: 'meta' }, fmtTs(u.t)))))) : emptyState('No upcoming reminders or events.'));
      clear(newsBody).append(d.headlines.length ? h('div', { class: 'list' }, d.headlines.map((a) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer', style: { color: 'var(--text)' } }, a.title), h('div', { class: 'meta' }, a.publisher + (a.published ? ' · ' + fmtTs(a.published) : '')))))) : h('div', { class: 'empty' }, 'No headlines stored yet. ', h('button', { class: 'sm', onclick: async () => { toast('Fetching news…'); report(await cmd('news', 'refresh')); data(); } }, 'Fetch now')));
      clear(notifBody).append(d.notifications.length ? h('div', { class: 'list' }, d.notifications.map((n) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', {}, n.title), h('div', { class: 'meta' }, ago(n.created)))))) : emptyState('You’re all caught up.'));
    } catch (e) { [sysBody, taskBody, remBody, newsBody, notifBody].forEach((b) => clear(b).append(h('div', { class: 'err-state' }, '⚠ ' + e.message))); }
  }
  async function weather() {
    try { const w = await api('/api/weather'); clear(wxBody);
      if (!w.ok) { wxBody.append(h('div', { class: 'empty' }, w.message, ' ', h('button', { class: 'sm', onclick: () => { location.hash = '#/settings'; } }, 'Set location'))); return; }
      const c = w.data.current, d = w.data.daily; wxBody.append(h('div', { class: 'big' }, Math.round(c.temp) + '°C'), h('div', {}, c.summary + ' · feels ' + Math.round(c.feels_like) + '°'), h('div', { class: 'sub' }, w.data.place + ' · humidity ' + c.humidity + '% · wind ' + Math.round(c.wind_kmh) + ' km/h'),
        h('div', { class: 'row', style: { marginTop: '10px' } }, d.map((x) => h('span', { class: 'tag' }, x.date.slice(5) + ' ' + Math.round(x.min) + '–' + Math.round(x.max) + '°'))), h('div', { class: 'sub', style: { marginTop: '6px' } }, 'Updated ' + (w.data.updated || '') + ' · Open-Meteo'));
    } catch (e) { clear(wxBody).append(h('div', { class: 'err-state' }, e.message)); }
  }
  data(); weather(); ctx.every(10000, data); ctx.every(30000, () => { fill(); });
};
function meter(label, v, note, invert, neutral) {
  const p = pct(v), cls = neutral ? '' : invert ? (p < 15 ? 'err' : p < 30 ? 'warn' : '') : (p > 90 ? 'err' : p > 75 ? 'warn' : '');
  return h('div', {}, h('div', { class: 'row between' }, h('span', {}, label), h('span', { class: 'mono sub' }, Math.round(p) + '%' + (note ? ' · ' + note : ''))), h('div', { class: 'bar ' + cls }, h('i', { style: { width: p + '%' } })));
}

// ───────────────────────────── ASSISTANT ─────────────────────────────
VIEWS.assistant = (root, ctx) => {
  const msgs = h('div', { class: 'msgs', 'aria-live': 'polite' }), input = h('input', { placeholder: 'Ask JARVIS to do something… (e.g. “remind me to call Sam in 20 minutes”)', 'aria-label': 'Message', autofocus: true });
  const side = h('div', { class: 'col' }), progress = h('div', {});
  const add = (role, text) => { const m = h('div', { class: 'msg ' + role }, role === 'assistant' ? h('button', { class: 'sm ghost copy', title: 'Copy', onclick: () => { navigator.clipboard && navigator.clipboard.writeText(text); toast('Copied'); } }, '⧉') : null, text); msgs.append(m); msgs.scrollTop = msgs.scrollHeight; return m; };
  const send = async () => {
    const t = input.value.trim(); if (!t) return; input.value = ''; add('user', t); const pending = add('assistant', '…'); btn.disabled = true;
    try {
      const r = await post('/api/chat', { message: t }); pending.remove(); add('assistant', r.reply + (r.route ? '' : '')); showRun(r);
      refreshBanner(); refreshStatus();
    } catch (e) { pending.remove(); add('assistant', '⚠ ' + e.message); }
    btn.disabled = false; input.focus();
  };
  const btn = h('button', { class: 'primary', onclick: send }, 'Send');
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
  root.append(h('div', { class: 'grid', style: { gridTemplateColumns: 'minmax(0,2fr) minmax(280px,1fr)' } },
    h('div', { class: 'card chat' }, h('h2', {}, 'Conversation'), msgs, h('div', { class: 'compose' }, input, btn)),
    side));
  const showRun = (r) => { clear(progress); if (r.steps && r.steps.length) progress.append(h('div', { class: 'steps' }, r.steps.map((s, i) => h('div', { class: 'step' }, h('span', { class: 'n' }, 's' + (i + 1)), h('span', {}, s.plugin + '.' + s.command), statusTag(s.status))))); };
  async function loadSide() {
    const st = S.status || {}; await refreshStatus();
    const v = S.status.voice;
    clear(side).append(
      h('div', { class: 'card' }, h('h2', {}, 'Assistant'), kv({ Provider: S.status.ai.provider, Model: S.status.ai.model || 'default', Planner: S.status.ai.configured ? 'AI (understands free-form requests)' : 'Offline router (common commands only)' }),
        S.status.ai.configured ? null : h('div', { style: { marginTop: '10px' } }, h('button', { onclick: () => { location.hash = '#/settings'; } }, 'Connect an AI provider'))),
      h('div', { class: 'card' }, h('h2', {}, 'Voice'), v.available ? kv({ Microphone: v.muted === null ? 'unknown' : (v.muted ? 'muted' : 'live'), 'Wake word': v.wake_enabled ? (v.awake ? 'awake' : 'sleeping') + ' · “' + v.wake_word + '”' : 'off', Mode: v.mode, Service: v.enabled ? 'enabled' : 'disabled' }) :
        h('div', { class: 'sub' }, 'The voice session belongs to the desktop app (main.py). This dashboard is running without it, so voice status and microphone control are not available here — text chat works fully.'),
        v.available && v.muted !== null ? h('div', { style: { marginTop: '10px' } }, h('span', { class: 'sub' }, 'Use the HUD to mute/unmute and push-to-talk.')) : null),
      h('div', { class: 'card' }, h('h2', {}, 'Active task'), progress, S.status.active_runs.length ? kv({ Run: '#' + S.status.active_runs[0].id + ' ' + S.status.active_runs[0].name, State: S.status.active_runs[0].status }) : h('div', { class: 'sub' }, 'Idle.')));
  }
  loadSide(); ctx.every(5000, loadSide);
  api('/api/chat/history').then((h_) => { h_.messages.forEach((m) => add(m.role === 'user' ? 'user' : 'assistant', m.content)); if (!h_.messages.length) add('assistant', 'At your service. Try “what are my tasks?”, “weather in London”, or “research the latest on solid-state batteries”.'); }).catch(() => {});
};

// ───────────────────────────── SYSTEM ─────────────────────────────
VIEWS.system = (root, ctx) => {
  let paused = false, sortKey = 'memory', filter = '', interval = 5000, timer;
  const cpuC = h('canvas'), ramC = h('canvas'), netC = h('canvas'), top = h('div', { class: 'grid' }), procBody = h('div', {}), pauseBtn = h('button', { onclick: () => { paused = !paused; pauseBtn.textContent = paused ? '▶ Resume' : '⏸ Pause'; } }, '⏸ Pause');
  const sel = h('select', { 'aria-label': 'Refresh interval', onchange: () => { interval = Number(sel.value); setT(); } }, [[2000, '2 s'], [5000, '5 s'], [15000, '15 s']].map(([v, l]) => h('option', { value: v, selected: v === 5000 }, l)));
  root.append(h('div', { class: 'row', style: { marginBottom: '14px' } }, pauseBtn, h('button', { onclick: refresh }, '⟳ Refresh now'), h('label', {}, 'Every '), sel), top,
    h('div', { class: 'grid', style: { marginTop: '16px' } }, h('div', { class: 'card' }, h('h2', {}, 'CPU %'), cpuC), h('div', { class: 'card' }, h('h2', {}, 'Memory %'), ramC), h('div', { class: 'card' }, h('h2', {}, 'Network KB/s ↓'), netC)),
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', {}, 'Processes', h('span', { class: 'spacer' }), h('input', { placeholder: 'Filter by name', 'aria-label': 'Filter processes', oninput: (e) => { filter = e.target.value; procs(); } })), procBody));
  async function refresh() {
    try {
      const d = await api('/api/system'), s = d.snapshot; if (!s) throw new Error(d.message || 'System monitor is unavailable (is the psutil package installed?)');
      clear(top).append(
        h('div', { class: 'card' }, h('h2', {}, 'CPU'), meter('Usage', s.cpu, s.cpu_cores + ' cores'), s.gpu ? h('div', { style: { marginTop: '8px' } }, meter('GPU ' + s.gpu.name, s.gpu.util)) : h('div', { class: 'sub', style: { marginTop: '8px' } }, 'No NVIDIA GPU data available.'), Object.keys(s.temperatures).length ? h('div', { class: 'sub', style: { marginTop: '8px' } }, 'Temps: ' + Object.entries(s.temperatures).map(([k, v]) => k + ' ' + v + '°C').join(', ')) : h('div', { class: 'sub', style: { marginTop: '8px' } }, 'No temperature sensors exposed.')),
        h('div', { class: 'card' }, h('h2', {}, 'Memory'), meter('RAM', s.ram.percent, fmtBytes(s.ram.used) + ' / ' + fmtBytes(s.ram.total))),
        h('div', { class: 'card' }, h('h2', {}, 'Storage'), h('div', { class: 'col' }, s.disks.map((x) => meter(x.mount, x.percent, fmtBytes(x.used) + ' / ' + fmtBytes(x.total))))),
        h('div', { class: 'card' }, h('h2', {}, 'Power & network'), kv({ Battery: s.battery ? s.battery.percent + '% · ' + (s.battery.charging ? 'charging' : 'on battery') + (s.battery.minutes_left ? ' · ' + s.battery.minutes_left + ' min left' : '') : 'no battery', Network: s.network.connected ? 'connected (' + s.network.interfaces_up.join(', ') + ')' : 'offline', Throughput: '↓ ' + fmtBytes(s.network.down_bps) + '/s  ↑ ' + fmtBytes(s.network.up_bps) + '/s', Uptime: fmtDur(s.uptime_seconds), Processes: s.processes })));
      const hist = d.history; spark(cpuC, hist.map((x) => x.cpu), null, 100); spark(ramC, hist.map((x) => x.ram), 'var(--ok)', 100); spark(netC, hist.map((x) => x.net_down / 1024));
    } catch (e) { clear(top).append(h('div', { class: 'err-state' }, '⚠ ' + e.message)); }
    procs();
  }
  async function procs() {
    try {
      const r = await cmd('sysmon', 'processes', { sort: sortKey, limit: 40, name: filter }); const rows = (r.data && r.data.processes) || [];
      clear(procBody).append(rows.length ? h('table', {}, h('thead', {}, h('tr', {}, h('th', {}, 'PID'), h('th', {}, 'Name'), h('th', { onclick: () => { sortKey = 'cpu'; procs(); } }, 'CPU % ' + (sortKey === 'cpu' ? '▼' : '')), h('th', { onclick: () => { sortKey = 'memory'; procs(); } }, 'Memory ' + (sortKey === 'memory' ? '▼' : '')), h('th', {}, ''))),
        h('tbody', {}, rows.map((p) => h('tr', {}, h('td', { class: 'mono' }, p.pid), h('td', {}, p.name), h('td', {}, p.cpu.toFixed(1)), h('td', {}, fmtBytes(p.memory)), h('td', {}, h('button', { class: 'sm danger', onclick: async () => { report(await cmd('sysmon', 'end_process', { pid: p.pid })); } }, 'End')))))) : emptyState('No matching processes.'));
    } catch (e) { clear(procBody).append(h('div', { class: 'err-state' }, e.message)); }
  }
  const setT = () => { clearInterval(timer); timer = setInterval(() => { if (!paused && !document.hidden) refresh(); }, interval); };
  S.cleanups.push(() => clearInterval(timer)); refresh(); setT();
};

// ───────────────────────────── MEDIA ─────────────────────────────
VIEWS.media = (root, ctx) => {
  const sp = h('div', { class: 'card' }), yt = h('div', { class: 'card' }), recent = h('div', { class: 'card span2' });
  root.append(h('div', { class: 'grid wide' }, sp, yt), h('div', { style: { height: '16px' } }), recent);
  async function load_() {
    let m; try { m = await api('/api/media'); } catch (e) { clear(sp).append(h('div', { class: 'err-state' }, e.message)); return; }
    // Spotify
    const s = m.spotify; clear(sp).append(h('h2', {}, 'Spotify ', statusTag(s.health ? s.health.status : 'error')));
    if (!s.ok) sp.append(h('div', { class: 'empty' }, s.message || 'Not available.'), h('div', { class: 'sub' }, (s.health && s.health.detail) || ''), h('button', { onclick: () => { location.hash = '#/plugins'; } }, 'Set up Spotify'), h('span', { style: { margin: '0 6px' } }), h('button', { onclick: async () => report(await cmd('spotify', 'connect')) }, 'Connect'));
    else if (!s.data.playing && !s.data.name) sp.append(h('div', { class: 'empty' }, 'Nothing is playing on Spotify.'), playerSearch());
    else { const d = s.data; sp.append(h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, d.artwork ? h('img', { class: 'art', src: d.artwork, alt: 'Album artwork', referrerPolicy: 'no-referrer' }) : h('div', { class: 'art' }),
        h('div', { class: 'col', style: { minWidth: 0 } }, h('strong', {}, d.name), h('div', { class: 'sub' }, (d.artists || []).join(', ') + ' · ' + (d.album || '')), h('div', { class: 'sub' }, (d.playing ? '▶ Playing' : '⏸ Paused') + ' on ' + (d.device || '?')))),
      h('div', { class: 'row', style: { margin: '12px 0' } }, ...[['⏮', 'previous'], [d.playing ? '⏸' : '▶', d.playing ? 'pause' : 'play'], ['⏭', 'next']].map(([l, a]) => h('button', { 'aria-label': a, onclick: async () => { report(await cmd('spotify', 'playback', { action: a })); load_(); } }, l))),
      h('div', { class: 'row' }, h('label', {}, 'Volume'), h('input', { type: 'range', min: 0, max: 100, value: d.volume ?? 50, 'aria-label': 'Spotify volume', onchange: async (e) => report(await cmd('spotify', 'playback', { action: 'volume', value: Number(e.target.value) })) })), playerSearch()); }
    // YouTube
    const y = m.youtube; clear(yt).append(h('h2', {}, 'YouTube ', statusTag(y.health ? y.health.status : 'error')));
    if (y.ok) { const d = y.data; yt.append(h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, d.thumbnail ? h('img', { class: 'art', src: d.thumbnail, alt: 'Video thumbnail', referrerPolicy: 'no-referrer' }) : h('div', { class: 'art' }), h('div', { class: 'col' }, h('strong', {}, d.title), h('div', { class: 'sub' }, (d.channel || '') + ' · ' + d.time + 's / ' + d.duration + 's'), h('div', { class: 'sub' }, d.paused ? '⏸ Paused' : '▶ Playing'))),
      h('div', { class: 'row', style: { margin: '12px 0' } }, h('button', { onclick: async () => { report(await cmd('youtube', 'control', { action: d.paused ? 'play' : 'pause' })); load_(); } }, d.paused ? '▶ Play' : '⏸ Pause'), h('button', { onclick: async () => { report(await cmd('youtube', 'control', { action: 'seek', value: Math.max(0, d.time - 10) })); } }, '−10 s'), h('button', { onclick: async () => { report(await cmd('youtube', 'control', { action: 'seek', value: d.time + 10 })); } }, '+10 s')),
      h('div', { class: 'row' }, h('label', {}, 'Volume'), h('input', { type: 'range', min: 0, max: 100, value: d.volume, 'aria-label': 'YouTube volume', onchange: async (e) => report(await cmd('youtube', 'control', { action: 'volume', value: Number(e.target.value) })) }))); }
    else yt.append(h('div', { class: 'empty' }, y.message || 'No video is open in the controlled browser.'), h('div', { class: 'sub' }, 'Playback control works in the GALAHAD-controlled browser only.'), h('div', { class: 'row', style: { marginTop: '10px' } }, h('button', { onclick: async () => { toast('Starting browser…'); report(await cmd('browser', 'start_controlled_browser')); load_(); } }, 'Start controlled browser')));
    const q = h('input', { placeholder: 'Search YouTube…', 'aria-label': 'YouTube search' }), out = h('div', {});
    yt.append(h('div', { class: 'row', style: { marginTop: '12px' } }, q, h('button', { onclick: async () => { const r = await cmd('youtube', 'search', { query: q.value, open: true }); clear(out); report(r); ((r.data && r.data.results) || []).forEach((v) => out.append(h('div', { class: 'item' }, v.thumbnail ? h('img', { src: v.thumbnail, class: 'art', style: { width: '64px', height: '40px' }, alt: '' }) : null, h('div', { class: 'grow' }, h('div', {}, v.title), h('div', { class: 'meta' }, v.channel)), h('button', { class: 'sm', onclick: async () => report(await cmd('youtube', 'open_video', { video: v.url })) }, 'Open')))); } }, 'Search')), out);
    // recently played
    if (s.ok) { try { const r = await cmd('spotify', 'recently_played', { limit: 8 }); clear(recent).append(h('h2', {}, 'Recently played'), r.ok ? h('div', { class: 'list' }, r.data.tracks.map((t) => h('div', { class: 'item' }, t.artwork ? h('img', { class: 'art', style: { width: '40px', height: '40px' }, src: t.artwork, alt: '', referrerPolicy: 'no-referrer' }) : null, h('div', { class: 'grow' }, h('div', {}, t.name), h('div', { class: 'meta' }, t.artists.join(', ')))))) : emptyState(r.message)); } catch (e) { /* optional */ } }
    else clear(recent).append(h('h2', {}, 'Recently played'), emptyState('Connect Spotify to see your listening history.'));
  }
  function playerSearch() {
    const q = h('input', { placeholder: 'Play a song, artist or playlist on Spotify…', 'aria-label': 'Spotify search' }), t = h('select', { 'aria-label': 'Type' }, ['track', 'artist', 'album', 'playlist'].map((x) => h('option', { value: x }, x)));
    return h('div', { class: 'row', style: { marginTop: '12px' } }, q, t, h('button', { onclick: async () => { report(await cmd('spotify', 'play', { query: q.value, type: t.value })); load_(); } }, 'Play'));
  }
  load_(); ctx.every(5000, load_);
};

// ───────────────────────────── PRODUCTIVITY ─────────────────────────────
VIEWS.productivity = (root, ctx) => {
  const tasksC = h('div', {}), remC = h('div', {}), calC = h('div', {}), doneC = h('div', {}); let filter = 'pending';
  const tTitle = h('input', { placeholder: 'New task…', 'aria-label': 'Task title' }), tDue = h('input', { placeholder: 'due (e.g. friday 5pm)', 'aria-label': 'Due' }), tPri = h('select', { 'aria-label': 'Priority' }, ['normal', 'high', 'urgent', 'low'].map((p) => h('option', {}, p)));
  const rText = h('input', { placeholder: 'Remind me to…', 'aria-label': 'Reminder text' }), rWhen = h('input', { placeholder: 'when (in 20 minutes · tomorrow 8am · every day at 7am)', 'aria-label': 'When', style: { flex: 1 } });
  const eTitle = h('input', { placeholder: 'Event title', 'aria-label': 'Event title' }), eWhen = h('input', { placeholder: 'start (tomorrow 3pm)', 'aria-label': 'Event start' });
  root.append(h('div', { class: 'row', style: { marginBottom: '14px' } }, h('button', { class: 'primary', onclick: async () => { toast('Building your briefing…'); const r = await cmd('productivity', 'daily_briefing'); modal('Daily overview', h('div', { class: 'report' }, r.message)); } }, 'Daily overview')),
    h('div', { class: 'grid wide' },
      h('div', { class: 'card' }, h('h2', {}, 'Tasks', h('span', { class: 'spacer' }), h('select', { 'aria-label': 'Filter', onchange: (e) => { filter = e.target.value; loadTasks(); } }, ['pending', 'all', 'completed'].map((x) => h('option', {}, x)))),
        h('div', { class: 'row', style: { marginBottom: '10px' } }, tTitle, tDue, tPri, h('button', { class: 'primary', onclick: async () => { if (!tTitle.value.trim()) return; const r = await cmd('productivity', 'add_task', { title: tTitle.value, due: tDue.value, priority: tPri.value }); report(r); if (r.ok) { tTitle.value = tDue.value = ''; loadTasks(); } } }, 'Add')), tasksC),
      h('div', { class: 'card' }, h('h2', {}, 'Reminders'), h('div', { class: 'row', style: { marginBottom: '10px' } }, rText, rWhen, h('button', { class: 'primary', onclick: async () => { const r = await cmd('productivity', 'add_reminder', { text: rText.value, when: rWhen.value }); report(r); if (r.ok) { rText.value = rWhen.value = ''; loadRem(); } } }, 'Set')), remC),
      h('div', { class: 'card' }, h('h2', {}, 'Calendar · next 14 days'), h('div', { class: 'row', style: { marginBottom: '10px' } }, eTitle, eWhen, h('button', { class: 'primary', onclick: async () => { const r = await cmd('productivity', 'add_event', { title: eTitle.value, start: eWhen.value }); report(r); if (r.ok) { eTitle.value = eWhen.value = ''; loadCal(); } } }, 'Add')), calC),
      h('div', { class: 'card' }, h('h2', {}, 'Completed'), doneC)));
  async function loadTasks() {
    const r = await cmd('productivity', 'list_tasks', { status: filter }); const ts = (r.data && r.data.tasks) || [];
    clear(tasksC).append(ts.length ? h('div', { class: 'list' }, ts.map((t) => h('div', { class: 'item' }, h('input', { type: 'checkbox', checked: t.status === 'completed', disabled: t.status === 'completed', 'aria-label': 'Complete ' + t.title, onchange: async () => { report(await cmd('productivity', 'complete_task', { id: t.id })); loadTasks(); loadDone(); } }),
      h('div', { class: 'grow' }, h('div', { class: 'title', style: t.status === 'completed' ? { textDecoration: 'line-through', opacity: .6 } : {} }, t.title), h('div', { class: 'meta' }, [t.priority_name, t.due ? 'due ' + fmtTs(t.due) : '', t.category].filter(Boolean).join(' · '))),
      h('button', { class: 'sm ghost danger', 'aria-label': 'Delete task', onclick: async () => { report(await cmd('productivity', 'delete_task', { id: t.id })); } }, '✕')))) : emptyState('No tasks here.'));
  }
  async function loadDone() { const r = await cmd('productivity', 'list_tasks', { status: 'completed' }); const ts = (r.data && r.data.tasks) || []; clear(doneC).append(ts.length ? h('div', { class: 'list' }, ts.slice(0, 12).map((t) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', { class: 'title' }, t.title), h('div', { class: 'meta' }, 'done ' + fmtTs(t.completed)))))) : emptyState('Nothing completed yet.')); }
  async function loadRem() {
    const r = await cmd('productivity', 'list_reminders', {}); const rs = (r.data && r.data.reminders) || [];
    clear(remC).append(rs.length ? h('div', { class: 'list' }, rs.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', {}, x.text), h('div', { class: 'meta' }, fmtTs(x.due) + (x.recurrence ? ' · repeats ' + x.recurrence : ''))), h('button', { class: 'sm ghost danger', 'aria-label': 'Delete reminder', onclick: async () => { report(await cmd('productivity', 'delete_reminder', { id: x.id })); loadRem(); } }, '✕')))) : emptyState('No active reminders.'));
  }
  async function loadCal() {
    const r = await cmd('productivity', 'upcoming_events', { days: 14 }); const es = (r.data && r.data.events) || [];
    clear(calC).append(es.length ? h('div', { class: 'list' }, es.map((x) => h('div', { class: 'item' }, h('span', { class: 'tag acc' }, x.source), h('div', { class: 'grow' }, h('div', {}, x.title), h('div', { class: 'meta' }, fmtTs(x.start) + (x.location ? ' · ' + x.location : '')))))) : emptyState('Nothing scheduled in the next 14 days.'));
  }
  loadTasks(); loadRem(); loadCal(); loadDone(); ctx.every(15000, () => { loadRem(); loadTasks(); });
};

// ───────────────────────────── INTELLIGENCE ─────────────────────────────
const NEWS_CATS = { '': 'All', medicine: 'Medicine', science: 'Science', ai_tech: 'AI & Tech', finance: 'Finance', economics: 'Economics', india: 'India', international: 'International' };
VIEWS.intelligence = (root, ctx) => {
  let cat = '', tab = 'headlines';
  const tabs = h('div', { class: 'tabs' }), body = h('div', {});
  const draw = () => { clear(tabs).append(...[['headlines', 'Headlines'], ['briefing', 'Daily briefing'], ['saved', 'Saved'], ['research', 'Research'], ['sources', 'Sources']].map(([k, l]) => h('button', { class: 'tab ' + (tab === k ? 'on' : ''), onclick: () => { tab = k; draw(); } }, l))); clear(body); ({ headlines, briefing, saved, research, sources })[tab](); };
  root.append(tabs, body);
  function catTabs(fn) { return h('div', { class: 'tabs' }, Object.entries(NEWS_CATS).map(([k, l]) => h('button', { class: 'tab ' + (cat === k ? 'on' : ''), onclick: () => { cat = k; draw(); } }, l))); }
  function articleRow(a, reload) {
    return h('div', { class: 'item' }, h('div', { class: 'grow' }, h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer', style: { color: 'var(--text)', fontWeight: 600 }, onclick: () => cmd('news', 'mark_read', { id: a.id }) }, a.title),
      h('div', { class: 'meta' }, [a.publisher, a.published ? fmtTs(a.published) : 'undated', NEWS_CATS[a.category]].filter(Boolean).join(' · ')), a.summary ? h('div', { class: 'sub' }, a.summary) : null),
      h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, h('button', { class: 'sm', title: 'Summarise', onclick: async (e) => { e.target.disabled = true; const r = await cmd('news', 'summarise', { article: String(a.id) }); e.target.disabled = false; modal(a.title, h('div', { class: 'report' }, r.message)); } }, 'Summarise'),
        h('button', { class: 'sm', title: 'Save', onclick: async () => { report(await cmd('news', 'save', { id: a.id, unsave: !!a.saved })); reload && reload(); } }, a.saved ? '★' : '☆'),
        h('button', { class: 'sm', title: 'Bookmark', onclick: async () => { report(await cmd('news', 'save', { id: a.id, bookmark: true, unsave: !!a.bookmarked })); reload && reload(); } }, a.bookmarked ? '🔖' : '📑')));
  }
  function headlines() {
    const list = h('div', {}); body.append(catTabs(), h('div', { class: 'row', style: { marginBottom: '12px' } }, h('button', { onclick: async () => { toast('Fetching feeds…'); const r = await cmd('news', 'refresh', { category: cat }); report(r); fetchList(); } }, '⟳ Refresh feeds'), h('span', { class: 'sub' }, 'Items older than 2 weeks are ignored; near-duplicate stories are merged.')), h('div', { class: 'card' }, list));
    const fetchList = () => load(list, cmd('news', 'headlines', { category: cat, limit: 25 }), (r) => { const as = (r.data && r.data.articles) || []; if (!r.ok || !as.length) { list.append(h('div', { class: 'empty' }, r.message || 'No headlines yet.')); return; } list.append(h('div', { class: 'sub', style: { marginBottom: '8px' } }, 'Fetched ' + fmtTs(r.data.fetched)), h('div', { class: 'list' }, as.map((a) => articleRow(a, fetchList)))); });
    fetchList();
  }
  function briefing() {
    const out = h('div', {}), kind = h('select', { 'aria-label': 'Kind' }, ['daily', 'weekly', 'category'].map((k) => h('option', {}, k))), catSel = h('select', { 'aria-label': 'Category' }, Object.entries(NEWS_CATS).filter(([k]) => k).map(([k, l]) => h('option', { value: k }, l)));
    body.append(h('div', { class: 'row', style: { marginBottom: '12px' } }, kind, catSel, h('button', { class: 'primary', onclick: () => load(out, cmd('news', 'briefing', { kind: kind.value, category: kind.value === 'category' ? catSel.value : '' }), (r) => out.append(h('div', { class: 'card' }, r.ok ? [h('div', { class: 'report' }, r.message), r.data ? h('div', { class: 'sub', style: { marginTop: '10px' } }, 'Method: ' + r.data.method + ' · generated ' + r.data.generated) : null] : h('div', { class: 'err-state' }, r.message)))) }, 'Generate')), out);
  }
  function saved() {
    const a = h('div', { class: 'card' }), b = h('div', { class: 'card', style: { marginTop: '16px' } });
    body.append(a, b);
    const go = () => { load(a, cmd('news', 'saved', {}), (r) => a.append(h('h2', {}, 'Saved articles'), r.data.articles.length ? h('div', { class: 'list' }, r.data.articles.map((x) => articleRow(x, go))) : emptyState('Nothing saved yet — use ☆ on a headline.')));
      load(b, cmd('news', 'saved', { bookmarked: true }), (r) => b.append(h('h2', {}, 'Bookmarks'), r.data.articles.length ? h('div', { class: 'list' }, r.data.articles.map((x) => articleRow(x, go))) : emptyState('No bookmarks.'))); }; go();
    const hist = h('div', { class: 'card', style: { marginTop: '16px' } }); body.append(hist);
    load(hist, cmd('news', 'history', {}), (r) => hist.append(h('h2', {}, 'Reading history'), r.data.articles.length ? h('div', { class: 'list' }, r.data.articles.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', {}, x.title), h('div', { class: 'meta' }, 'read ' + ago(x.read_at)))))) : emptyState('Nothing read yet.')));
  }
  function research() {
    const q = h('input', { placeholder: 'What do you want researched?', style: { flex: 1 }, 'aria-label': 'Research question' }), fmt = h('select', { 'aria-label': 'Format' }, ['detailed', 'brief', 'comparison', 'notes', 'table', 'timeline', 'pros_cons', 'technical'].map((x) => h('option', {}, x))), depth = h('select', { 'aria-label': 'Depth' }, ['normal', 'quick', 'deep'].map((x) => h('option', {}, x)));
    const out = h('div', {}), hist = h('div', { class: 'card', style: { marginTop: '16px' } });
    body.append(h('div', { class: 'card' }, h('h2', {}, 'Deep research'), h('div', { class: 'row' }, q, fmt, depth, h('button', { class: 'primary', onclick: () => { if (!q.value.trim()) return; load(out, cmd('research', 'research', { question: q.value, format: fmt.value, depth: depth.value }), (r) => { if (!r.ok) { out.append(h('div', { class: 'err-state' }, r.message)); return; } out.append(h('div', { class: 'report', style: { marginTop: '12px' } }, r.data.report), h('div', { class: 'row', style: { marginTop: '10px' } }, h('button', { onclick: async () => report(await cmd('research', 'export', { id: r.data.id })) }, 'Export Markdown'), h('span', { class: 'sub' }, r.data.method))); loadHist(); }); } }, 'Research')), h('div', { class: 'sub', style: { marginTop: '6px' } }, 'Takes up to a few minutes. Every reference is a page actually fetched; claims are labelled established / evidence-based / opinion / disputed / uncertain.'), out), hist);
    const loadHist = () => load(hist, cmd('research', 'history', {}), (r) => hist.append(h('h2', {}, 'Research history'), r.data.reports.length ? h('div', { class: 'list' }, r.data.reports.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', {}, x.question), h('div', { class: 'meta' }, fmtTs(x.created) + ' · ' + x.fmt)), h('button', { class: 'sm', onclick: async () => { const g = await cmd('research', 'get_report', { id: x.id }); modal(x.question, h('div', { class: 'report' }, g.data.report)); } }, 'Open')))) : emptyState('No research yet.')));
    loadHist();
  }
  function sources() { const c = h('div', { class: 'card' }); body.append(c); load(c, cmd('news', 'sources', {}), (r) => c.append(h('h2', {}, 'News feeds'), h('table', {}, h('thead', {}, h('tr', {}, ['Category', 'Source', 'Status', 'Items'].map((x) => h('th', {}, x)))), h('tbody', {}, r.data.feeds.map((f) => h('tr', {}, h('td', {}, NEWS_CATS[f.category]), h('td', {}, f.name), h('td', {}, f.ok === null ? h('span', { class: 'tag' }, 'not fetched') : f.ok ? h('span', { class: 'tag ok' }, 'ok') : h('span', { class: 'tag err', title: f.error }, 'failing')), h('td', {}, f.items ?? '—'))))))); }
  draw();
};

// ───────────────────────────── FINANCE ─────────────────────────────
VIEWS.finance = (root, ctx) => {
  const c = h('div', {}); root.append(c);
  const money = (n) => (Number(n) || 0).toLocaleString(undefined, { maximumFractionDigits: 0 });
  async function go() {
    await load(c, api('/api/finance'), (f) => {
      if (f.needs_permission) { c.append(h('div', { class: 'card' }, h('h2', {}, 'Finance is private'), h('p', {}, f.message), h('button', { class: 'primary', onclick: async () => { await post('/api/plugins/finance/permission', { permission: 'finance.data', granted: true }); toast('Permission granted.', 'ok'); go(); } }, 'Grant access'))); return; }
      const d = f.data, s = d.summary; const cat = h('div', {}), amount = h('input', { type: 'number', min: 0, step: '0.01', placeholder: 'Amount', 'aria-label': 'Amount' }), kind = h('select', { 'aria-label': 'Kind' }, ['expense', 'income', 'saving'].map((x) => h('option', {}, x))), catIn = h('input', { placeholder: 'Category', 'aria-label': 'Category' });
      c.append(h('div', { class: 'grid' },
        h('div', { class: 'card' }, h('h2', {}, 'This month · ' + s.month), kv({ Income: money(s.income), Expenses: money(s.expenses), 'Savings added': money(s.savings_added), Net: money(s.net), 'Total recorded savings': money(s.total_recorded_savings) }), h('div', { class: 'sub', style: { marginTop: '8px' } }, 'Source: your manual entries')),
        h('div', { class: 'card' }, h('h2', {}, 'Add entry'), h('div', { class: 'col' }, kind, amount, catIn, h('button', { class: 'primary', onclick: async () => { report(await cmd('finance', 'add_entry', { kind: kind.value, amount: Number(amount.value), category: catIn.value || 'general' })); go(); } }, 'Record'))),
        h('div', { class: 'card' }, h('h2', {}, 'Spending by category'), s.categories.length ? h('div', { class: 'col' }, s.categories.map((x) => meter(x.category, s.expenses ? x.s / s.expenses * 100 : 0, money(x.s), false, true))) : emptyState('No expenses this month.')),
        h('div', { class: 'card' }, h('h2', {}, 'Budgets'), s.budgets.length ? h('div', { class: 'col' }, s.budgets.map((b) => meter(b.category, b.percent, money(b.spent) + ' / ' + money(b.limit)))) : emptyState('No budgets set. Say “set a budget for food of 8000”.'), (() => { const bc = h('input', { placeholder: 'category' }), bl = h('input', { type: 'number', placeholder: 'monthly limit' }); return h('div', { class: 'row', style: { marginTop: '10px' } }, bc, bl, h('button', { onclick: async () => { report(await cmd('finance', 'set_budget', { category: bc.value, monthly_limit: Number(bl.value) })); go(); } }, 'Set')); })()),
        h('div', { class: 'card' }, h('h2', {}, 'Goals'), d.goals.length ? h('div', { class: 'col' }, d.goals.map((g) => meter(g.name, g.percent, money(g.saved) + ' / ' + money(g.target), false, true))) : emptyState('No goals yet.')),
        h('div', { class: 'card span2' }, h('h2', {}, 'Holdings (manual) · total ' + money(d.holdings_total)), d.holdings.length ? h('table', {}, h('thead', {}, h('tr', {}, ['Type', 'Name', 'Value', 'Rate', 'Projection (estimate)'].map((x) => h('th', {}, x)))), h('tbody', {}, d.holdings.map((x) => h('tr', {}, h('td', {}, x.kind), h('td', {}, x.name), h('td', {}, money(x.amount)), h('td', {}, x.rate ? x.rate + '%' : '—'), h('td', {}, x.projection ? (x.projection.maturity_value ? 'matures ≈ ' + money(x.projection.maturity_value) : x.projection.future_value ? 'grows to ≈ ' + money(x.projection.future_value) + ' in ' + x.projection.months + ' mo' : x.projection.error || '') : '—'))))) : emptyState('Track FDs, RDs, SIPs, mutual funds and accounts by telling JARVIS or via the Finance plugin.')),
        h('div', { class: 'card' }, h('h2', {}, 'Markets (live, delayed)', h('span', { class: 'spacer' }), h('button', { class: 'sm', onclick: markets }, 'Load')), h('div', { id: 'mk' }, emptyState('Press Load to fetch index levels.'))),
        h('div', { class: 'card' }, h('h2', {}, 'Currency (ECB)', h('span', { class: 'spacer' }), h('button', { class: 'sm', onclick: fx }, 'Load')), h('div', { id: 'fx' }, emptyState('Press Load.'))),
        h('div', { class: 'card span2' }, h('h2', {}, 'Finance news', h('span', { class: 'spacer' }), h('button', { class: 'sm', onclick: fnews }, 'Load')), h('div', { id: 'fn' }, emptyState('Press Load.')))));
    });
  }
  async function markets() { const r = await cmd('finance', 'markets', {}); const b = $('#mk'); clear(b).append(r.ok ? h('div', { class: 'list' }, r.data.indices.map((i) => h('div', { class: 'item' }, h('div', { class: 'grow' }, i.name.toUpperCase()), h('span', { class: 'mono' }, i.close.toLocaleString()), h('span', { class: 'tag ' + (i.change_pct >= 0 ? 'ok' : 'err') }, (i.change_pct >= 0 ? '+' : '') + i.change_pct + '%')))) : h('div', { class: 'err-state' }, r.message)); }
  async function fx() { const r = await cmd('finance', 'fx', { base: 'USD', targets: ['INR', 'EUR', 'GBP', 'JPY'] }); clear($('#fx')).append(r.ok ? h('div', {}, h('div', { class: 'list' }, Object.entries(r.data.rates).map(([k, v]) => h('div', { class: 'item' }, h('div', { class: 'grow' }, '1 USD → ' + k), h('span', { class: 'mono' }, v.toFixed(4))))), h('div', { class: 'sub' }, 'ECB reference rates, ' + r.data.date)) : h('div', { class: 'err-state' }, r.message)); }
  async function fnews() { const r = await cmd('finance', 'news', {}); clear($('#fn')).append(r.ok && r.data ? h('div', { class: 'list' }, r.data.articles.map((a) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('a', { href: a.url, target: '_blank', rel: 'noopener noreferrer', style: { color: 'var(--text)' } }, a.title), h('div', { class: 'meta' }, a.publisher))))) : h('div', { class: 'err-state' }, r.message)); }
  go();
};

// ───────────────────────────── DOCUMENTS ─────────────────────────────
VIEWS.documents = (root, ctx) => {
  let kind = '';
  const list = h('div', {}), q = h('input', { placeholder: 'Search files by name…', 'aria-label': 'Search files', style: { flex: 1 } });
  const kinds = h('div', { class: 'tabs' });
  const drawKinds = () => clear(kinds).append(...[['', 'All'], ['word', 'Word'], ['excel', 'Excel'], ['powerpoint', 'PowerPoint'], ['pdf', 'PDF']].map(([k, l]) => h('button', { class: 'tab ' + (kind === k ? 'on' : ''), onclick: () => { kind = k; drawKinds(); go(); } }, l)));
  const create = (type, ext) => { const n = h('input', { placeholder: 'File name (saved in Documents/GALAHAD)', style: { width: '100%' } }); modal('New ' + type + ' document', n, (close) => [h('button', { onclick: close }, 'Cancel'), h('button', { class: 'primary', onclick: async () => { const p = n.value.trim(); if (!p) return; const args = type === 'Word' ? { path: p, title: p } : type === 'Excel' ? { path: p } : { path: p, title: p }; const r = await cmd(type.toLowerCase(), 'create', args); report(r); close(); if (r.ok) { await cmd(type.toLowerCase(), 'open', { path: r.data.path }); go(); } } }, 'Create & open')]); };
  root.append(h('div', { class: 'row', style: { marginBottom: '14px' } }, q, h('button', { class: 'primary', onclick: go }, 'Search'), h('button', { onclick: () => create('Word') }, '+ Word'), h('button', { onclick: () => create('Excel') }, '+ Excel'), h('button', { onclick: () => create('PowerPoint') }, '+ PowerPoint')), kinds, h('div', { class: 'card' }, list));
  q.addEventListener('keydown', (e) => { if (e.key === 'Enter') go(); });
  const ico = (n) => /\.docx?$/i.test(n) ? '📄' : /\.xlsx?$|\.csv$/i.test(n) ? '📊' : /\.pptx?$/i.test(n) ? '📽' : /\.pdf$/i.test(n) ? '📕' : '📁';
  function go() {
    const search = q.value.trim();
    const p = search ? cmd('files', 'search', { query: search, kind, limit: 40 }) : cmd('files', 'recent', { days: 30, kind, limit: 40 });
    load(list, p, (r) => { const rs = (r.data && r.data.results) || []; if (!r.ok) { list.append(h('div', { class: 'err-state' }, r.message)); return; } list.append(h('h2', { style: { fontSize: '11.5px', letterSpacing: '.16em', color: 'var(--muted)', textTransform: 'uppercase' } }, search ? 'Results' : 'Recent documents (30 days)'), rs.length ? h('div', { class: 'list' }, rs.map((f) => h('div', { class: 'item' }, h('span', {}, ico(f.name)), h('div', { class: 'grow' }, h('div', { class: 'title' }, f.name), h('div', { class: 'meta mono' }, f.path + (f.size ? ' · ' + fmtBytes(f.size) : '') + (f.modified ? ' · ' + fmtTs(f.modified) : ''))),
      h('button', { class: 'sm', onclick: async () => report(await cmd('files', 'open', { path: f.path })) }, 'Open'), /\.(docx|xlsx|pptx)$/i.test(f.name) ? h('button', { class: 'sm', onclick: async () => { const t = f.name.endsWith('docx') ? ['word', 'summarise'] : f.name.endsWith('xlsx') ? ['excel', 'info'] : ['powerpoint', 'read']; const x = await cmd(t[0], t[1], { path: f.path }); modal(f.name, h('div', { class: 'report' }, x.message)); } }, 'Preview') : null, h('button', { class: 'sm danger', onclick: async () => report(await cmd('files', 'delete', { path: f.path })) }, 'Delete')))) : emptyState('No documents found. Searches cover Documents, Desktop, Downloads, Pictures, Music and Videos (add folders in Settings → Privacy).')); });
  }
  drawKinds(); go();
};

// ───────────────────────────── PLUGINS ─────────────────────────────
VIEWS.plugins = (root, ctx) => {
  let data, cat = '';
  const grid = h('div', { class: 'grid' }), tabs = h('div', { class: 'tabs' });
  root.append(tabs, grid);
  async function go() {
    await load(grid, api('/api/plugins'), (d) => { data = d; drawTabs(); drawGrid(); });
  }
  function drawTabs() { clear(tabs).append(...['', ...data.categories].map((c) => h('button', { class: 'tab ' + (cat === c ? 'on' : ''), onclick: () => { cat = c; drawTabs(); drawGrid(); } }, c || 'All')), h('span', { class: 'spacer' }), h('span', { class: 'sub' }, data.plugins.filter((p) => p.state === 'active').length + ' active / ' + data.plugins.length)); }
  function drawGrid() {
    clear(grid);
    const shown = data.plugins.filter((p) => !cat || p.category === cat);
    if (!shown.length) grid.append(emptyState('No plugins in this category.'));
    for (const p of shown) {
      const hs = p.health ? p.health.status : 'error';
      grid.append(h('div', { class: 'card', style: { cursor: 'pointer' }, tabindex: 0, onclick: () => detail(p.id), onkeydown: (e) => { if (e.key === 'Enter') detail(p.id); } },
        h('h2', {}, p.name || p.id, h('span', { class: 'spacer' }), statusTag(p.state)),
        h('div', { class: 'sub', style: { minHeight: '38px' } }, p.description), h('div', { class: 'row', style: { marginTop: '10px' } }, h('span', { class: 'tag' }, p.category || '—'), h('span', { class: 'tag' }, 'v' + (p.version || '?')), h('span', { class: 'tag ' + (hs === 'ok' ? 'ok' : hs === 'error' ? 'err' : 'warn') }, 'health: ' + hs), p.source === 'user' ? h('span', { class: 'tag warn' }, 'user plugin') : null),
        p.health && p.health.detail ? h('div', { class: 'sub', style: { marginTop: '6px' } }, p.health.detail) : null));
    }
  }
  async function act(pid, action, body) {
    try { const r = await post('/api/plugins/' + pid + '/' + action, body); toast(r.message || (r.ok ? 'Done' : 'Failed'), r.ok ? 'ok' : 'err'); await go(); refreshStatus(); return r; } catch (e) { toast(e.message, 'err'); }
  }
  function detail(pid) {
    const p = data.plugins.find((x) => x.id === pid); if (!p) return;
    const body = h('div', { class: 'col' });
    body.append(h('div', { class: 'sub' }, p.description), kv({ ID: p.id, Version: p.version, Author: p.author, Category: p.category, State: p.state, Installed: p.installed ? 'yes' : 'no', Enabled: p.enabled ? 'yes' : 'no', Health: p.health.status + (p.health.detail ? ' — ' + p.health.detail : ''), Dependencies: [...(p.requires_python || []).map((x) => 'python:' + x), ...(p.requires_plugins || []).map((x) => 'plugin:' + x)].join(', ') || 'none', Optional: (p.optional_python || []).join(', ') || 'none', Missing: (p.missing || []).join(', ') || 'none', Subscriptions: (p.subscriptions || []).join(', ') || 'none', 'Last run': p.last_execution ? p.last_execution.action + ' → ' + p.last_execution.status + ' (' + fmtTs(p.last_execution.started) + ')' : 'never', 'Last error': p.last_error || p.error || 'none' }));
    if (p.setup_help) body.append(h('div', { class: 'card', style: { padding: '10px 14px' } }, h('strong', {}, 'Setup'), h('div', { class: 'sub' }, p.setup_help)));
    // permissions
    body.append(h('div', { class: 'cfg-section' }, h('strong', {}, 'Permissions')), ...(p.permissions || []).map((pm) => h('label', { class: 'switch', title: pm.description }, h('input', { type: 'checkbox', checked: pm.granted, onchange: async (e) => { await act(pid, 'permission', { permission: pm.permission, granted: e.target.checked }); } }), h('span', {}, pm.permission + ' — ' + pm.description), pm.sensitive ? h('span', { class: 'tag warn' }, 'sensitive') : null)));
    // config
    if (p.config && p.config.length) {
      const inputs = {};
      body.append(h('div', { class: 'cfg-section' }, h('strong', {}, 'Configuration')), ...p.config.map((f) => {
        const t = f.type === 'boolean' || f.type === 'bool' ? 'checkbox' : (f.type === 'integer' || f.type === 'number') ? 'number' : f.secret ? 'password' : 'text';
        const inp = inputs[f.key] = h('input', { type: t, placeholder: f.secret ? (f.is_set ? '•••••••• (saved — leave blank to keep)' : 'not set') : '', value: t === 'checkbox' ? undefined : (f.secret ? '' : (f.value ?? '')), checked: t === 'checkbox' ? !!f.value : undefined, autocomplete: 'off' });
        return field(f.label + (f.help ? ' — ' + f.help : ''), inp);
      }), h('button', { class: 'primary', onclick: async () => { const vals = {}; for (const f of p.config) { const i = inputs[f.key]; vals[f.key] = i.type === 'checkbox' ? i.checked : i.value; } const r = await act(pid, 'config', { values: vals }); } }, 'Save configuration'));
    }
    // commands
    body.append(h('div', { class: 'cfg-section' }, h('strong', {}, 'Commands (' + (p.commands || []).length + ')')), h('table', {}, h('tbody', {}, (p.commands || []).map((c) => h('tr', {}, h('td', { class: 'mono' }, c.name), h('td', {}, c.description, h('div', { class: 'sub mono' }, Object.entries(c.params).map(([k, v]) => k + (v.required ? '*' : '') + ':' + (v.type || 'string')).join(', '))), h('td', {}, [c.confirm ? h('span', { class: 'tag warn' }, 'asks') : null, c.readonly ? h('span', { class: 'tag' }, 'read-only') : null]), h('td', {}, c.readonly && !Object.values(c.params).some((v) => v.required) ? h('button', { class: 'sm', onclick: async () => { const r = await cmd(pid, c.name, {}); modal(pid + '.' + c.name, h('div', { class: 'report' }, r.message || '(no message)')); } }, 'Run') : null))))));
    const acts = (close) => [p.state === 'active' ? h('button', { onclick: async () => { const r = await act(pid, 'test'); if (r) modal('Test result', h('div', {}, (r.ok ? '✓ ' : '✗ ') + r.message)); } }, 'Test') : null,
      p.enabled ? h('button', { onclick: async () => { await act(pid, 'disable'); close(); } }, 'Disable') : h('button', { onclick: async () => { await act(pid, 'enable'); close(); } }, 'Enable'),
      p.installed ? h('button', { class: 'danger', onclick: async () => { if (confirm('Uninstall ' + p.name + '? Your data is kept.')) { await act(pid, 'uninstall'); close(); } } }, 'Uninstall') : h('button', { class: 'primary', onclick: async () => { await act(pid, 'install'); close(); } }, 'Install'),
      h('button', { onclick: async () => { await act(pid, 'update'); close(); } }, 'Update'), h('button', { onclick: close }, 'Close')];
    modal(p.name + ' · v' + p.version, body, acts);
  }
  go(); ctx.every(20000, () => { if (!$('#modal-root').firstChild) go(); });
};

// ───────────────────────────── AI EXECUTION ─────────────────────────────
VIEWS.execution = (root, ctx) => {
  const pend = h('div', {}), runs = h('div', {}), wf = h('div', {}), fails = h('div', {}), calls = h('div', {});
  root.append(h('div', { class: 'grid wide' }, h('div', { class: 'card' }, h('h2', {}, 'Pending confirmations'), pend), h('div', { class: 'card' }, h('h2', {}, 'Active & recent runs'), runs), h('div', { class: 'card' }, h('h2', {}, 'Workflows', h('span', { class: 'spacer' }), h('button', { class: 'sm', onclick: () => editWf() }, '+ New')), wf), h('div', { class: 'card' }, h('h2', {}, 'Failures'), fails), h('div', { class: 'card span2' }, h('h2', {}, 'Tool calls'), calls)));
  const stepsView = (r) => h('div', { class: 'steps' }, r.progress.map((s, i) => h('div', { class: 'step' }, h('span', { class: 'n' }, s.id), h('span', { class: 'grow' }, s.plugin + '.' + s.command), statusTag(s.status), s.message ? h('span', { class: 'sub', title: s.message }, s.message.slice(0, 50)) : null)));
  async function refresh() {
    try {
      const [c, w, a] = await Promise.all([api('/api/confirmations'), api('/api/workflows'), api('/api/activity?limit=60')]);
      clear(pend).append(c.pending.length ? h('div', { class: 'list' }, c.pending.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('strong', {}, x.plugin + '.' + x.command), h('pre', { class: 'mono', style: { whiteSpace: 'pre-wrap', margin: '4px 0', fontSize: '12.5px' } }, x.summary), h('div', { class: 'meta' }, 'origin: ' + x.origin)),
        h('div', { class: 'col' }, h('button', { class: 'sm primary', onclick: async () => { report(await post('/api/confirmations/' + x.id + '/approve')); refresh(); refreshBanner(); } }, 'Approve'), h('button', { class: 'sm danger', onclick: async () => { await post('/api/confirmations/' + x.id + '/deny'); refresh(); refreshBanner(); } }, 'Deny'))))) : emptyState('Nothing is waiting for you.'));
      clear(runs).append(w.runs.length ? h('div', { class: 'list' }, w.runs.slice(0, 8).map((r) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', { class: 'row between' }, h('strong', {}, '#' + r.id + ' ' + r.name), statusTag(r.status)), stepsView(r), r.error ? h('div', { class: 'err-state', style: { padding: '4px 0' } }, r.error) : null, h('div', { class: 'meta' }, fmtTs(r.started))), ['running', 'awaiting_confirmation'].includes(r.status) ? h('button', { class: 'sm danger', onclick: async () => { await post('/api/runs/' + r.id + '/cancel'); refresh(); } }, 'Cancel') : null))) : emptyState('No runs yet. Ask JARVIS for something multi-step, e.g. “search for solar news and save a task to read it”.'));
      clear(wf).append(w.workflows.length ? h('div', { class: 'list' }, w.workflows.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('strong', {}, x.name), h('div', { class: 'meta' }, x.description || ''), h('div', { class: 'meta' }, 'trigger: ' + trig(x.trigger) + ' · ' + x.steps.length + ' steps' + (x.last_run ? ' · last ' + ago(x.last_run) : '') + (x.next_run ? ' · next ' + fmtTs(x.next_run) : ''))),
        h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, h('button', { class: 'sm', onclick: async () => { report(await cmd('automation', 'run', { workflow: String(x.id) })); refresh(); } }, '▶ Run'), h('button', { class: 'sm', onclick: async () => { await cmd('automation', 'set_enabled', { workflow: String(x.id), enabled: !x.enabled }); refresh(); } }, x.enabled ? 'Disable' : 'Enable'), h('button', { class: 'sm', onclick: () => editWf(x) }, 'Edit'), h('button', { class: 'sm danger', onclick: async () => { report(await cmd('automation', 'delete', { workflow: String(x.id) })); } }, '✕'))))) : emptyState('No workflows. Create one, or tell JARVIS “every weekday at 8 give me my briefing”.'));
      const bad = a.activity.filter((x) => x.status === 'failed').slice(0, 8);
      clear(fails).append(bad.length ? h('div', { class: 'list' }, bad.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('div', {}, x.action), h('div', { class: 'meta' }, fmtTs(x.started)), h('div', { class: 'err-state', style: { padding: '2px 0' } }, x.error))))) : emptyState('No failures. 👌'));
      clear(calls).append(h('table', {}, h('thead', {}, h('tr', {}, ['Time', 'Tool', 'Origin', 'Status', 'Result'].map((x) => h('th', {}, x)))), h('tbody', {}, a.activity.slice(0, 20).map((x) => h('tr', {}, h('td', {}, fmtTs(x.started, { hour: '2-digit', minute: '2-digit', second: '2-digit' })), h('td', { class: 'mono' }, x.action), h('td', {}, x.origin), h('td', {}, statusTag(x.status)), h('td', {}, (x.result || x.error || '').slice(0, 110)))))));
    } catch (e) { clear(pend).append(h('div', { class: 'err-state' }, e.message)); }
  }
  const trig = (t) => t.type === 'schedule' ? 'daily at ' + t.at : t.type === 'interval' ? 'every ' + t.minutes + ' min' : t.type === 'event' ? 'on ' + t.topic : 'manual';
  function editWf(w) {
    const name = h('input', { value: w ? w.name : '', placeholder: 'Name' }), desc = h('input', { value: w ? w.description : '', placeholder: 'Description' });
    const steps = h('textarea', { 'aria-label': 'Steps JSON', spellcheck: false }, JSON.stringify(w ? w.steps : [{ id: 's1', plugin: 'news', command: 'headlines', args: { category: 'ai_tech', limit: 3 } }], null, 1));
    const trg = h('textarea', { 'aria-label': 'Trigger JSON', style: { minHeight: '60px' }, spellcheck: false }, JSON.stringify(w ? w.trigger : { type: 'manual' }));
    const req = h('input', { placeholder: 'Or describe what it should do in plain English (needs AI)…' });
    modal(w ? 'Edit workflow' : 'New workflow', h('div', { class: 'col' }, field('Name', name), field('Description', desc), field('Describe it (optional)', req), h('button', { onclick: async () => { const r = await cmd('automation', 'plan', { request: req.value }); if (r.ok) { steps.value = JSON.stringify(r.data.steps, null, 1); toast('Plan filled in below — review it.', 'ok'); } else toast(r.message, 'err'); } }, 'Plan from description'), field('Steps (JSON) — {{s1.message}} passes data between steps', steps), field('Trigger (JSON): manual · {"type":"schedule","at":"08:00","days":[0,1,2,3,4]} · {"type":"interval","minutes":30} · {"type":"event","topic":"reminder.fired"}', trg)),
      (close) => [h('button', { onclick: close }, 'Cancel'), h('button', { class: 'primary', onclick: async () => { let s, t; try { s = JSON.parse(steps.value); t = JSON.parse(trg.value); } catch (e) { toast('Steps/trigger must be valid JSON: ' + e.message, 'err'); return; } const r = await cmd('automation', 'save', { name: name.value, description: desc.value, steps: s, trigger: t, id: w ? w.id : undefined }); report(r); if (r.ok) { close(); refresh(); } } }, 'Save')]);
  }
  refresh(); ctx.every(2500, refresh);
};

// ───────────────────────────── NOTIFICATIONS ─────────────────────────────
VIEWS.notifications = (root, ctx) => {
  let cat = '', unread = false; const list = h('div', {}), tabs = h('div', { class: 'tabs' });
  const cats = ['', 'reminder', 'task', 'workflow', 'email', 'system', 'plugin', 'assistant', 'briefing'];
  const drawTabs = () => clear(tabs).append(...cats.map((c) => h('button', { class: 'tab ' + (cat === c ? 'on' : ''), onclick: () => { cat = c; drawTabs(); go(); } }, c || 'All')), h('span', { class: 'spacer' }), h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: unread, onchange: (e) => { unread = e.target.checked; go(); } }), 'Unread only'));
  root.append(h('div', { class: 'row', style: { marginBottom: '10px' } }, h('button', { onclick: async () => { await post('/api/notifications/read'); go(); refreshStatus(); } }, 'Mark all read'), h('button', { onclick: async () => { await post('/api/notifications/dismiss'); go(); refreshStatus(); } }, 'Dismiss all'), h('button', { class: 'ghost', onclick: () => { location.hash = '#/settings'; } }, '⚙ Notification settings')), tabs, h('div', { class: 'card' }, list));
  const pri = ['low', 'normal', 'high', 'urgent'];
  function go() {
    load(list, api('/api/notifications?category=' + cat + '&unread=' + unread), (r) => {
      list.append(r.notifications.length ? h('div', { class: 'list' }, r.notifications.map((n) => h('div', { class: 'item', style: { opacity: n.read ? .65 : 1 } }, h('span', { class: 'dot ' + (n.priority >= 3 ? 'err' : n.priority === 2 ? 'warn' : n.read ? '' : 'ok'), style: { marginTop: '7px' } }),
        h('div', { class: 'grow' }, h('div', { class: 'title' }, n.title), n.body ? h('div', { class: 'sub' }, n.body) : null, h('div', { class: 'meta' }, n.category + ' · ' + pri[n.priority] + ' · ' + ago(n.created) + (n.source ? ' · ' + n.source : ''))),
        !n.read ? h('button', { class: 'sm', onclick: async () => { await post('/api/notifications/read', { id: n.id }); go(); refreshStatus(); } }, 'Read') : null, h('button', { class: 'sm ghost', 'aria-label': 'Dismiss', onclick: async () => { await post('/api/notifications/dismiss', { id: n.id }); go(); refreshStatus(); } }, '✕')))) : emptyState('No notifications here.'));
    });
  }
  drawTabs(); go(); ctx.every(8000, go);
};

// ───────────────────────────── ACTIVITY ─────────────────────────────
VIEWS.activity = (root, ctx) => {
  let plugin = '', status = ''; const list = h('div', {});
  const pSel = h('input', { placeholder: 'plugin id', 'aria-label': 'Plugin filter', onchange: (e) => { plugin = e.target.value.trim(); go(); } }), sSel = h('select', { 'aria-label': 'Status filter', onchange: (e) => { status = e.target.value; go(); } }, ['', 'completed', 'failed', 'running', 'awaiting_confirmation', 'cancelled'].map((x) => h('option', { value: x }, x || 'any status')));
  root.append(h('div', { class: 'row', style: { marginBottom: '12px' } }, pSel, sSel, h('button', { onclick: go }, '⟳')), h('div', { class: 'card' }, list));
  function go() {
    load(list, api('/api/activity?limit=200&plugin=' + encodeURIComponent(plugin) + '&status=' + status), (r) => {
      list.append(r.activity.length ? h('table', {}, h('thead', {}, h('tr', {}, ['When', 'Action', 'Origin', 'Status', 'Confirmation', 'Took', 'Result / error', ''].map((x) => h('th', {}, x)))), h('tbody', {}, r.activity.map((x) => h('tr', {}, h('td', {}, fmtTs(x.started, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })), h('td', { class: 'mono' }, x.action), h('td', {}, x.origin), h('td', {}, statusTag(x.status)), h('td', {}, x.confirmation === 'not_required' ? '—' : x.confirmation), h('td', {}, x.finished ? ((x.finished - x.started).toFixed(2) + ' s') : '…'), h('td', {}, (x.error || x.result || '').slice(0, 160), x.args && x.args !== '{}' ? h('div', { class: 'sub mono', title: x.args }, x.args.slice(0, 90)) : null),
        h('td', {}, ['running', 'awaiting_confirmation', 'pending'].includes(x.status) ? h('button', { class: 'sm danger', onclick: async () => { await post('/api/activity/' + x.id + '/cancel'); go(); } }, 'Cancel') : null))))) : emptyState('No activity matches.'));
    });
  }
  go(); ctx.every(4000, go);
};

// ───────────────────────────── SETTINGS ─────────────────────────────
const SECTION_TITLES = { general: 'General', startup: 'Startup & background', ai: 'AI', voice: 'Voice', privacy: 'Privacy', appearance: 'Appearance', notifications: 'Notifications', location: 'Location', dashboard: 'Dashboard', memory: 'Memory' };
VIEWS.settings = (root, ctx) => {
  let tab = 'ai-providers'; const tabs = h('div', { class: 'tabs' }), body = h('div', {});
  root.append(tabs, body);
  let data;
  const sections = () => ['ai-providers', 'memory-manager', ...Array.from(new Set(data.settings.map((s) => s.section)))];
  const label = (s) => ({ 'ai-providers': 'AI providers', 'memory-manager': 'Memory manager' })[s] || SECTION_TITLES[s] || s;
  async function go() { await load(body, api('/api/settings'), (d) => { data = d; draw(); }); }
  function draw() { clear(tabs).append(...sections().map((s) => h('button', { class: 'tab ' + (tab === s ? 'on' : ''), onclick: () => { tab = s; draw(); } }, label(s)))); clear(body); if (tab === 'ai-providers') providers(); else if (tab === 'memory-manager') memory(); else form(tab); }
  async function save(key, value, el) { try { const r = await post('/api/settings', { key, value }); toast(r.message || 'Saved', 'ok'); await refreshStatus(); if (el) el.classList.remove('err'); return true; } catch (e) { toast(e.message, 'err'); return false; } }
  function form(sec) {
    const card = h('div', { class: 'card' }, h('h2', {}, label(sec)), h('div', { class: 'col' })); const col = card.lastChild;
    for (const s of data.settings.filter((x) => x.section === sec)) {
      let inp;
      if (s.type === 'bool') inp = h('label', { class: 'switch' }, h('input', { type: 'checkbox', checked: !!s.value, onchange: (e) => save(s.key, e.target.checked) }), s.description);
      else if (s.key === 'appearance.theme') inp = field(s.description, h('select', { onchange: (e) => save(s.key, e.target.value) }, ['dark', 'light'].map((o) => h('option', { selected: s.value === o }, o))));
      else if (s.key === 'voice.listening_mode') inp = field(s.description, h('select', { onchange: (e) => save(s.key, e.target.value) }, ['wake_word', 'push_to_talk', 'continuous'].map((o) => h('option', { selected: s.value === o }, o))));
      else if (s.key === 'appearance.accent') inp = field(s.description, h('input', { type: 'color', value: s.value, onchange: (e) => save(s.key, e.target.value) }));
      else if (s.key === 'appearance.transparency') inp = field(s.description + ' (' + s.value + ')', h('input', { type: 'range', min: .3, max: 1, step: .05, value: s.value, onchange: (e) => save(s.key, Number(e.target.value)) }));
      else if (s.type === 'list' || s.type === 'dict') { const ta = h('textarea', { style: { minHeight: '56px' }, spellcheck: false }, JSON.stringify(s.value)); inp = field(s.description, h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, ta, h('button', { class: 'sm', onclick: async () => { try { await save(s.key, JSON.parse(ta.value)); } catch (e) { toast('Invalid JSON', 'err'); } } }, 'Save'))); }
      else { const i = h('input', { type: s.type === 'int' || s.type === 'float' ? 'number' : 'text', step: s.type === 'float' ? 'any' : undefined, value: s.value ?? '', onchange: () => save(s.key, i.value) }); inp = field(s.description, i); }
      col.append(inp);
    }
    if (sec === 'startup') col.append(h('div', { class: 'sub' }, 'Start with Windows ' + (data.autostart ? 'is currently ON' : 'is currently off') + ' (per-user registry Run key; no admin rights).'));
    if (sec === 'privacy') col.append(h('div', { class: 'sub' }, 'Secrets are stored using: ' + data.secret_backend + '. Data folder: ' + data.paths.data), h('div', { class: 'row' }, h('button', { onclick: () => { location.hash = '#/activity'; } }, 'View what was sent to AI (activity → ai.private_send)')));
    body.append(card);
  }
  function providers() {
    body.append(h('div', { class: 'card', style: { marginBottom: '16px' } }, h('h2', {}, 'Important'), h('div', { class: 'sub' }, 'An AI API key is separate from a chat subscription. ChatGPT Plus, Claude Pro and Gemini Advanced do not include API credits — create a key in the provider’s developer console. Keys are stored encrypted (' + data.secret_backend + ') and never shown again.')),
      h('div', { class: 'grid' }, data.ai.map((p) => {
        const key = h('input', { type: 'password', placeholder: p.configured ? '•••• saved (' + p.key_source + ') — leave blank to keep' : p.needs_key ? 'API key' : 'no key needed', autocomplete: 'off' }), model = h('input', { value: p.model, 'aria-label': 'Model' }), base = h('input', { value: p.base_url, 'aria-label': 'Base URL' }), temp = h('input', { type: 'number', step: '0.1', min: 0, max: 2, value: p.settings.temperature ?? '', placeholder: 'temp (default)' });
        return h('div', { class: 'card' }, h('h2', {}, p.label, h('span', { class: 'spacer' }), p.active ? h('span', { class: 'tag ok' }, 'active') : null, p.configured ? h('span', { class: 'tag acc' }, 'ready') : h('span', { class: 'tag warn' }, 'needs key')),
          h('div', { class: 'col' }, field('API key' + (p.console ? ' — get one at ' + p.console : ''), key), field('Model', model), p.id === 'openai_compatible' ? field('Base URL (Ollama / LM Studio / Groq / OpenRouter)', base) : null, temp,
            h('div', { class: 'row' }, h('button', { class: 'primary', onclick: async () => { const r = await cmd('ai_providers', 'configure', { provider: p.id, api_key: key.value, model: model.value, base_url: p.id === 'openai_compatible' ? base.value : '', temperature: temp.value === '' ? undefined : Number(temp.value) }); report(r); go(); } }, 'Save'),
              h('button', { onclick: async (e) => { e.target.disabled = true; const r = await cmd('ai_providers', 'test_connection', { provider: p.id }); e.target.disabled = false; report(r); } }, 'Test connection'),
              p.active ? null : h('button', { onclick: async () => { report(await cmd('ai_providers', 'select', { provider: p.id })); go(); } }, 'Use for conversation'))));
      })));
    const fb = h('input', { value: (data.settings.find((s) => s.key === 'ai.fallback_providers').value || []).join(', '), placeholder: 'e.g. openai, anthropic' });
    body.append(h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', {}, 'Fallback & per-workflow providers'), field('Fallback order when the primary fails (comma separated)', h('div', { class: 'row', style: { flexWrap: 'nowrap' } }, fb, h('button', { class: 'sm', onclick: async () => { report(await cmd('ai_providers', 'select', { fallbacks: fb.value.split(',').map((x) => x.trim()).filter(Boolean) })); } }, 'Save'))), h('div', { class: 'sub', style: { marginTop: '8px' } }, 'Per-purpose overrides (assistant, research, news, email, documents, vision) are edited under AI → “ai.workflow_providers”.')));
    const u = h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', {}, 'Usage (30 days)')); body.append(u);
    cmd('ai_providers', 'usage', { days: 30 }).then((r) => u.append(r.data.by_model.length ? h('table', {}, h('thead', {}, h('tr', {}, ['Provider', 'Model', 'Calls', 'Prompt tokens', 'Completion tokens', 'Errors'].map((x) => h('th', {}, x)))), h('tbody', {}, r.data.by_model.map((m) => h('tr', {}, h('td', {}, m.provider), h('td', {}, m.model), h('td', {}, m.calls), h('td', {}, m.pt || '—'), h('td', {}, m.ct || '—'), h('td', {}, m.errors))))) : emptyState('No AI calls yet.')));
  }
  function memory() {
    const c = h('div', {}); body.append(c);
    load(c, api('/api/memory'), (m) => {
      const key = h('input', { placeholder: 'key (e.g. favourite coffee)' }), val = h('input', { placeholder: 'value', style: { flex: 1 } });
      c.append(h('div', { class: 'grid wide' },
        h('div', { class: 'card' }, h('h2', {}, 'Long-term memory (you control this)'), h('div', { class: 'row', style: { marginBottom: '10px' } }, key, val, h('button', { class: 'primary', onclick: async () => { try { await post('/api/memory/remember', { key: key.value, value: val.value }); go(); } catch (e) { toast(e.message, 'err'); } } }, 'Save')),
          m.long_term.length ? h('div', { class: 'list' }, m.long_term.map((x) => h('div', { class: 'item' }, h('div', { class: 'grow' }, h('strong', {}, x.key), h('div', {}, x.value), h('div', { class: 'meta' }, x.category + ' · ' + ago(x.updated))), h('button', { class: 'sm', onclick: () => { key.value = x.key; val.value = x.value; val.focus(); } }, 'Edit'), h('button', { class: 'sm danger', onclick: async () => { await api('/api/memory/' + encodeURIComponent(x.key), { method: 'DELETE' }); go(); } }, 'Forget')))) : emptyState('Nothing saved. Say “remember that I prefer tea”.')),
        h('div', { class: 'card' }, h('h2', {}, 'Short-term (this conversation)'), m.short_term.messages.length ? h('div', { class: 'list' }, m.short_term.messages.slice(-8).map((x) => h('div', { class: 'item' }, h('span', { class: 'tag' }, x.role), h('div', { class: 'grow sub' }, x.content.slice(0, 160))))) : emptyState('Empty.')),
        h('div', { class: 'card' }, h('h2', {}, 'Operational (what GALAHAD did)'), h('div', { class: 'list' }, m.operational.slice(0, 8).map((x) => h('div', { class: 'item' }, h('span', { class: 'tag' }, x.type), h('div', { class: 'grow sub' }, (x.action || x.name) + ' → ' + x.status))))),
        h('div', { class: 'card' }, h('h2', {}, 'Controls'), h('div', { class: 'col' }, h('a', { href: '/api/memory/export', download: 'galahad-memory.json' }, h('button', {}, '⬇ Export everything (JSON)')),
          ...[['short_term', 'Clear conversation history'], ['long_term', 'Erase long-term memory'], ['operational', 'Erase activity history'], ['all', 'Erase ALL memory']].map(([w, l]) => h('button', { class: 'danger', onclick: async () => { if (confirm(l + '? This cannot be undone.')) { const r = await post('/api/memory/clear', { which: w }); toast('Deleted: ' + JSON.stringify(r.deleted), 'ok'); go(); } } }, l))))));
    });
  }
  go();
};

// ───────────────────────────── boot ─────────────────────────────
async function boot() {
  for (const id of ORDER) if (!VIEWS[id]) VIEWS[id] = (r) => r.append(emptyState('Not available.'));
  buildTop(); buildNav();
  await refreshStatus(); buildNav(); route(); refreshBanner(); tickClock();
  window.addEventListener('hashchange', route);
  setInterval(tickClock, 15000); setInterval(refreshStatus, 10000); setInterval(refreshBanner, 6000);
  try {
    const es = new EventSource('/api/stream');
    es.onmessage = (m) => { try { const ev = JSON.parse(m.data); if (ev.topic.startsWith('confirm.')) refreshBanner(); if (ev.topic === 'notify.deliver') { toast(ev.payload.title, ev.payload.priority >= 3 ? 'err' : ''); } if (/^(confirm|notify|workflow)\./.test(ev.topic)) refreshStatus(); } catch (e) { /* ignore */ } };
  } catch (e) { /* SSE optional: polling covers it */ }
}
boot().catch((e) => { document.body.prepend(h('div', { class: 'err-state', style: { padding: '30px' } }, 'GALAHAD could not start: ' + e.message)); });
