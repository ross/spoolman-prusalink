'use strict';

// Control page for the printer simulator. State arrives whole over SSE and
// every render works from it; the only local state is UI state (open forms,
// typed values, log filters), kept per printer so re-renders don't lose it.

const SETTINGS = [
  ['time_scale', 'time scale', 'n'], ['feed_ratio', 'feed ratio', 'n'],
  ['print_rate_mm_s', 'print mm/s', 'n'], ['load_rate_mm_s', 'load mm/s', 'n'],
  ['unload_seconds', 'unload s', 'n'], ['load_feed_mm', 'load feed mm', 'n'],
  ['purge_mm', 'purge mm', 'n'], ['toolchange_purge_mm', 'toolchange purge mm', 'n'],
  ['jam_purge_mm', 'jam purge mm', 'n'], ['preheat_seconds', 'preheat s', 'n'],
  ['scan_seconds', 'scan s', 'n'], ['blip_seconds', 'blip s', 'n'],
  ['park_seconds', 'park s', 'n'], ['sync_job_seconds', 'sync job s', 'n'],
  ['auto_accept_model_check', 'auto-accept model check', 'b'], ['start_blip', 'start blip', 'b'],
];
const KINDS = ['action', 'state', 'request', 'load', 'unload', 'job', 'gcode', 'other'];
const IDLEISH = ['IDLE', 'FINISHED', 'STOPPED'];
const MAX_LOG = 500;

let S = null; // last full state
const ui = {}; // printer name -> {open: {}, v: {}}
const cardEls = new Map(); // printer name -> {el, body, toast, html, timer}
const logRows = []; // newest first
const logSeen = new Set();
const openRows = new Set();
const logFilter = { printer: '', kinds: new Set(KINDS), polls: false };
let logPending = false;

const $ = (id) => document.getElementById(id);
const up = (s) => String(s == null ? '' : s).toUpperCase();
const low = (s) => String(s == null ? '' : s).toLowerCase();
const esc = (s) =>
  String(s == null ? '' : s).replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);

function fmtTime(t, tenths) {
  t = Math.max(0, Number(t) || 0);
  let m = Math.floor(t / 60);
  let s = t - m * 60;
  if (tenths) {
    s = Math.floor(s * 10) / 10;
    return m + ':' + (s < 10 ? '0' : '') + s.toFixed(1);
  }
  s = Math.floor(s);
  return m + ':' + (s < 10 ? '0' : '') + s;
}

function pst(name) {
  return ui[name] || (ui[name] = { open: {}, v: {} });
}
function getv(name, k, def) {
  const v = pst(name).v;
  return k in v ? v[k] : def;
}

// ---- HTTP ----

async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let data = null;
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new Error((data && data.error) || r.status + ' ' + r.statusText);
  return data;
}

function showToast(el, text, ms) {
  el.textContent = text;
  el.hidden = false;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.hidden = true; }, ms || 4000);
}

async function doAction(pname, action, body, onOk) {
  const c = cardEls.get(pname);
  try {
    await api('/api/printers/' + encodeURIComponent(pname) + '/' + action, body);
    if (onOk) onOk();
  } catch (e) {
    showToast(c ? c.toast : $('gtoast'), e.message || String(e));
  }
}

// ---- settings strip ----

function buildSettings() {
  const box = $('settings');
  box.innerHTML = SETTINGS.map(([k, label, type]) =>
    '<label>' + esc(label) + ' <input data-s="' + k + '" ' +
    (type === 'b' ? 'type="checkbox"' : 'type="number" step="any"') + '></label>').join('');
  box.addEventListener('change', async (e) => {
    const k = e.target.dataset.s;
    if (!k) return;
    const isBool = e.target.type === 'checkbox';
    if (!isBool && e.target.value.trim() === '') return;
    const v = isBool ? e.target.checked : Number(e.target.value);
    try {
      await api('/api/settings', { [k]: v });
    } catch (err) {
      showToast($('gtoast'), err.message);
      renderSettings();
    }
  });
}

function renderSettings() {
  if (!S) return;
  for (const [k, , type] of SETTINGS) {
    const el = document.querySelector('[data-s="' + k + '"]');
    if (!el || el === document.activeElement || !(k in S.settings)) continue;
    if (type === 'b') el.checked = !!S.settings[k];
    else el.value = S.settings[k];
  }
  $('now').textContent = fmtTime(S.now);
  const sel = $('snap-list');
  const names = S.snapshots || [];
  if (sel !== document.activeElement && sel.dataset.sig !== names.join('\n')) {
    const cur = sel.value;
    sel.innerHTML = names.map((n) => '<option>' + esc(n) + '</option>').join('');
    if (names.includes(cur)) sel.value = cur;
    sel.dataset.sig = names.join('\n');
  }
  $('snap-restore').disabled = !names.length;
}

function wireSnapshots() {
  $('snap-save').onclick = async () => {
    const n = $('snap-name').value.trim();
    if (!n) return showToast($('gtoast'), 'Enter a snapshot name');
    try { await api('/api/snapshot/' + encodeURIComponent(n)); } catch (e) { showToast($('gtoast'), e.message); }
  };
  $('snap-restore').onclick = async () => {
    const n = $('snap-list').value;
    if (!n) return;
    try { await api('/api/restore/' + encodeURIComponent(n)); } catch (e) { showToast($('gtoast'), e.message); }
  };
}

// ---- cards ----

function inp(p, k, def, attrs) {
  return '<input data-k="' + esc(k) + '" value="' + esc(getv(p.name, k, def)) + '" ' + (attrs || 'type="text"') + '>';
}
function opts(list, cur) {
  return list.map((o) => '<option value="' + esc(o) + '"' + (o === cur ? ' selected' : '') + '>' + esc(o) + '</option>').join('');
}
function btn(label, act, o) {
  o = o || {};
  return '<button type="button" data-act="' + act + '"' +
    (o.tool != null ? ' data-tool="' + o.tool + '"' : '') +
    (o.form ? ' data-form="' + o.form + '"' : '') +
    (o.dis ? ' disabled' : '') + (o.cls ? ' class="' + o.cls + '"' : '') +
    (o.title ? ' title="' + esc(o.title) + '"' : '') + '>' + esc(label) + '</button>';
}
function isOpen(p, f) { return !!pst(p.name).open[f]; }

function matHtml(m, prev) {
  if (!m) return '<span class="muted">-</span>';
  const uuid = m.instance_uuid || m.uuid;
  return '<span class="' + (prev ? 'prev' : '') + '">' +
    (m.color ? '<span class="swatch" style="background:' + esc(m.color) + '" title="' + esc(m.color) + '"></span>' : '') +
    esc(m.name) + (m.adhoc === false ? ' <span class="tag" title="preset material">preset</span>' : '') +
    (uuid ? '<br><span class="uuid" title="' + esc(uuid) + '">' + esc(String(uuid).slice(0, 8)) + '</span>' : '') +
    '</span>';
}

function renderTool(p, t) {
  const st = up(p.state);
  const job = p.job;
  const ph = job ? up(job.phase) : '';
  const attn = job && job.attention ? String(job.attention) : '';
  const proc = t.process;
  const i = t.index;
  const mentions = new RegExp('tool ' + i + '\\b').test(attn);
  const running = st === 'PRINTING' || st === 'PAUSED';
  const multi = p.tools.length > 1;
  const midPrint = job && (ph === 'PRINTING' || ph === 'PREHEAT');
  const off = low(p.power) === 'off';
  const tdis = !t.enabled; // a disabled tool takes only Enable/Disable
  const hasJob = !!job;
  const wm = job && job.wait_tool != null ? job.wait_tool : (/tool (\d+)/.exec(attn) || [])[1];
  const canKeep = st === 'ATTENTION' && ph === 'WAIT_LOAD' && wm != null && Number(wm) === i;
  const canTag = !!(p.model && p.model.uuids);
  const dLoad = tdis || !!proc || (running && !(st === 'ATTENTION' && mentions));
  const dUnload = tdis || !t.loaded || !!proc || running || hasJob;
  const dMove = !IDLEISH.includes(st);
  const dRun = tdis || !(midPrint && t.loaded);

  let procTxt = '';
  if (proc) {
    const rem = proc.remaining_mm != null ? proc.remaining_mm.toFixed(1) + ' mm'
      : proc.remaining_s != null ? proc.remaining_s.toFixed(1) + ' s' : '';
    procTxt = '<span class="proc">' + esc(proc.kind) + ' ' + esc(proc.phase) + ' ' + rem + '</span>';
  }
  const flags = (t.sensor_fault ? '<span class="flag" title="sensor fault">sensor</span> ' : '') +
    (t.stuck_write ? '<span class="flag alt" title="stuck write">stuck</span>' : '');

  let h = '<tr class="t">' +
    '<td class="' + (p.selected_tool === i ? 'sel' : '') + '">' + (p.selected_tool === i ? '&#9654;' : '') + i + '</td>' +
    '<td>' + (t.enabled ? 'on' : '<span class="dis">off</span>') + '</td>' +
    '<td>' + (t.loaded ? 'yes' : '<span class="muted">no</span>') + '</td>' +
    '<td>' + matHtml(t.material, false) + '</td>' +
    '<td>' + matHtml(t.kept_type || t.previous, true) + '</td>' +
    '<td class="num">' + Math.floor(t.extruded || 0) + '</td>' +
    '<td class="num">' + t.unload_count + '</td>' +
    '<td class="num">' + t.extruded_at_unload + '</td>' +
    '<td class="num">' + Number(t.gap || 0).toFixed(1) + '</td></tr>';

  h += '<tr class="sub"><td colspan="9"><div class="toolbtns">' + procTxt + flags + (flags ? ' ' : '') +
    btn('Load', 'load_form', { tool: i, dis: dLoad, cls: isOpen(p, 'load' + i) ? 'on' : '' }) +
    btn('Unload', 'unload', { tool: i, dis: dUnload }) +
    btn('Extrude', 'extrude', { tool: i, dis: tdis || off || dMove || !t.loaded }) +
    btn('Retract', 'retract', { tool: i, dis: tdis || off || dMove }) +
    btn('Runout', 'runout', { tool: i, dis: dRun }) +
    btn('Jam', 'jam', { tool: i, dis: dRun }) +
    '<label title="unload on jam"><input type="checkbox" data-k="jam' + i + '"' +
    (getv(p.name, 'jam' + i, true) ? ' checked' : '') + '>unload</label>' +
    btn('Select', 'select_tool', { tool: i, dis: tdis || !multi || hasJob }) +
    btn('Sensor fault', 'sensor_fault', { tool: i, dis: tdis, cls: t.sensor_fault ? 'on' : '' }) +
    btn('Stuck write', 'stuck_write', { tool: i, dis: tdis, cls: t.stuck_write ? 'on' : '' }) +
    btn(t.enabled ? 'Disable' : 'Enable', 'tool_enabled', { tool: i }) +
    '</div>';

  if (isOpen(p, 'load' + i)) {
    const sources = (canTag ? ['tag'] : []).concat(['preset'], canKeep ? ['keep'] : []);
    let src = getv(p.name, 'load' + i + '.source', 'preset');
    if (!sources.includes(src)) src = 'preset';
    const nm = String(getv(p.name, 'load' + i + '.name', 'PLA')).trim();
    const needName = src !== 'keep';
    h += '<div class="form">' +
      '<label>source<select data-k="load' + i + '.source">' + opts(sources, src) + '</select></label>' +
      '<label>name' + inp(p, 'load' + i + '.name', 'PLA') + '</label>' +
      '<label>color' + inp(p, 'load' + i + '.color', '', 'type="text" placeholder="#rrggbb"') + '</label>' +
      '<label>uuid' + inp(p, 'load' + i + '.uuid', '', 'type="text" size="36"') + '</label>' +
      '<label>feed mm' + inp(p, 'load' + i + '.feed', '', 'type="number" step="any"') + '</label>' +
      '<label>purge mm' + inp(p, 'load' + i + '.purge', '', 'type="number" step="any"') + '</label>' +
      btn('Start load', 'load_submit', { tool: i, dis: tdis || (needName && !nm) }) + '</div>';
  }
  return h + '</td></tr>';
}

function renderPrinter(p) {
  const st = up(p.state);
  const job = p.job;
  const ph = job ? up(job.phase) : '';
  const off = low(p.power) === 'off';
  const m = p.model || {};
  const attn = job && job.attention ? String(job.attention) : '';

  let pct = job ? 0 : null;
  if (job) {
    if (job.progress != null) pct = job.progress;
    else if (job.total_mm) pct = (100 * job.fed_mm) / job.total_mm;
  }
  pct = pct == null ? null : Math.max(0, Math.min(100, pct));

  let h = '<div class="row"><b class="name">' + esc(p.name) + '</b>' +
    '<span>' + esc(m.name) + ' <span class="muted">(' + esc(m.code) + ')</span></span>' +
    '<span class="badge st-' + esc(st) + '">' + esc(st) + '</span>' +
    '<small class="muted mono" title="api key">:' + esc(p.port) + ' ' + esc(p.api_key) + '</small></div>' +
    '<div class="row">' +
    '<label>power <select data-sel="set_power" data-field="power">' + opts(['on', 'off', 'hung'], low(p.power)) + '</select></label>' +
    '<label>fault <select data-sel="set_fault" data-field="fault">' + opts(['none', 'bad_key', 'error'], low(p.fault)) + '</select></label>' +
    '<label>fw <select data-sel="set_firmware" data-field="firmware">' + opts(['full', 'no_counters', 'no_endpoint'], low(p.firmware)) + '</select></label>' +
    '<small class="muted">boot ' + esc(p.boot_id) + '</small>' +
    '<small>' + (p.homed ? 'homed' : '<span class="muted">not homed</span>') + '</small>' +
    '<small>tool <b>' + esc(p.selected_tool) + '</b></small></div>';

  h += '<div class="jobline">';
  if (job) {
    h += '<span>job <b>#' + (job.id == null ? '?' : esc(job.id)) + '</b> ' + esc(job.kind) + ' ' + esc(job.display_name || job.name) +
      ' <span class="muted">' + esc(ph) + '</span></span>';
    if (ph === 'HOLD') {
      // the attention text stands in for the bar
    } else if (pct != null) {
      h += '<span class="bar" title="' + pct.toFixed(1) + '%"><i style="width:' + pct.toFixed(1) + '%"></i></span><small>' +
        pct.toFixed(0) + '%' + (job.total_mm ? ' ' + Number(job.fed_mm || 0).toFixed(0) + '/' + Number(job.total_mm).toFixed(0) + ' mm' : '') + '</small>';
    } else if (job.total_mm) {
      h += '<small>' + Number(job.fed_mm || 0).toFixed(0) + '/' + Number(job.total_mm).toFixed(0) + ' mm</small>';
    }
    if (attn) h += '<span class="attn">' + esc(attn) + '</span>';
  } else {
    h += '<span class="muted">no job</span>';
  }
  h += '</div>';
  if (p.message) h += '<div style="margin-top:3px"><span class="pill" title="last M117">' + esc(p.message) + '</span></div>';

  const canStart = IDLEISH.includes(st) && !off && !(st === 'IDLE' && job) && !p.tools.some((t) => t.process);
  const canClearJam = !off && ph === 'HOLD' && !!job && job.jam === true;
  const aa = p.auto_accept_model_check;
  const tap = st === 'ATTENTION' && (attn === 'model check' || attn.startsWith('parse error'));
  h += '<div class="btns">' +
    btn('Start print', 'toggle', { form: 'start', dis: !canStart, cls: isOpen(p, 'start') ? 'on' : '' }) +
    btn('Pause', 'pause', { dis: off || !(ph === 'PRINTING' || ph === 'PREHEAT') }) +
    btn('Resume', 'resume', { dis: off || !(st === 'PAUSED' || ph === 'AWAIT_RESUME') }) +
    btn('Cancel', 'cancel', { dis: off || !job || ph === 'PARKING', cls: 'danger' }) +
    btn('Dismiss', 'dismiss', { dis: off || !(st === 'FINISHED' || st === 'STOPPED') }) +
    btn('Home', 'home', { dis: off || !!job || !(st === 'IDLE' || st === 'FINISHED' || st === 'STOPPED') }) +
    btn('Reboot', 'reboot', { dis: off }) +
    btn('Power-loss resume', 'power_loss', { dis: off, title: 'reboot, keeping the job' }) +
    '<label title="the bed cools during the outage, so the print waits for Resume"><input type="checkbox" data-k="cold"' +
    (getv(p.name, 'cold', false) ? ' checked' : '') + '>cold bed</label>' +
    btn('Tap through', 'tap_through', { dis: off || !tap }) +
    (canClearJam ? btn('Clear jam', 'clear_jam', {}) : '') +
    btn('Auto-accept: ' + (aa == null ? 'default' : aa ? 'on' : 'off'), 'auto_accept', { title: 'model check; cycles default, on, off' }) +
    btn('Counter step back', 'toggle', { form: 'counter', cls: isOpen(p, 'counter') ? 'on' : '' }) +
    btn('Set extruded_at_unload', 'toggle', { form: 'eau', cls: isOpen(p, 'eau') ? 'on' : '' }) +
    '</div>';

  if (isOpen(p, 'start')) {
    h += '<div class="form">' +
      '<label>segments (tool:mm per line)<textarea data-k="start.segments">' + esc(getv(p.name, 'start.segments', '0:1000')) + '</textarea></label>' +
      '<label>duration s' + inp(p, 'start.duration', '', 'type="number" step="any"') + '</label>' +
      '<label>preheat s' + inp(p, 'start.preheat', '', 'type="number" step="any"') + '</label>' +
      '<label>color changes mm (a, b)' + inp(p, 'start.cc', '', 'type="text"') + '</label>' +
      '<label>file name' + inp(p, 'start.name', 'part.bgcode', 'type="text"') + '</label>' +
      btn('Start', 'start_submit', {}) + '</div>';
  }
  const toolSel = (k) => '<select data-k="' + k + '">' +
    opts(p.tools.map((t) => String(t.index)), String(getv(p.name, k, p.tools.length ? p.tools[0].index : 0))) + '</select>';
  if (isOpen(p, 'counter')) {
    h += '<div class="form"><label>tool' + toolSel('counter.tool') + '</label>' +
      '<label>mm' + inp(p, 'counter.mm', '10', 'type="number" step="any"') + '</label>' +
      btn('Step back', 'counter_submit', {}) + '</div>';
  }
  if (isOpen(p, 'eau')) {
    h += '<div class="form"><label>tool' + toolSel('eau.tool') + '</label>' +
      '<label>value' + inp(p, 'eau.value', '0', 'type="number" step="1"') + '</label>' +
      btn('Set', 'eau_submit', {}) + '</div>';
  }

  h += '<div class="mmbar">extrude / retract mm ' + inp(p, 'mm', '10', 'type="number" step="any"') + '</div>';
  h += '<table class="tools"><thead><tr><th>#</th><th>en</th><th>loaded</th><th>material</th><th title="what a runout reload restores">kept</th>' +
    '<th class="num">extr</th><th class="num">ul</th><th class="num">at ul</th><th class="num">gap</th></tr></thead><tbody>' +
    p.tools.map((t) => renderTool(p, t)).join('') + '</tbody></table>';
  return h;
}

function typing(el) {
  const a = document.activeElement;
  if (!a || !el.contains(a)) return false;
  const tag = a.tagName;
  return tag === 'TEXTAREA' || tag === 'SELECT' ||
    (tag === 'INPUT' && a.type !== 'checkbox' && a.type !== 'button');
}

function renderCard(name) {
  const c = cardEls.get(name);
  const p = S && S.printers.find((x) => x.name === name);
  if (!c || !p) return;
  const html = renderPrinter(p);
  if (html === c.html) return;
  if (typing(c.el)) { c.dirty = true; return; }
  c.html = html;
  c.dirty = false;
  c.body.innerHTML = html;
}

function renderCards() {
  if (!S) return;
  const host = $('cards');
  const names = S.printers.map((p) => p.name);
  for (const [n, c] of cardEls) {
    if (!names.includes(n)) { c.el.remove(); cardEls.delete(n); }
  }
  names.forEach((n, idx) => {
    if (!cardEls.has(n)) {
      const el = document.createElement('section');
      el.className = 'card';
      el.dataset.p = n;
      el.innerHTML = '<div class="body"></div><div class="toast" hidden></div>';
      cardEls.set(n, { el, body: el.firstChild, toast: el.lastChild, html: null, dirty: false });
    }
    const c = cardEls.get(n);
    if (host.children[idx] !== c.el) host.insertBefore(c.el, host.children[idx] || null);
    renderCard(n);
  });
}

function numOrNull(s) {
  s = String(s == null ? '' : s).trim();
  if (s === '') return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : NaN;
}

function startBody(name) {
  const segs = [];
  for (const line of String(getv(name, 'start.segments', '0:1000')).split('\n')) {
    if (!line.trim()) continue;
    const m = /^\s*(\d+)\s*[:=]\s*([0-9.]+)\s*$/.exec(line);
    if (!m) throw new Error('Bad segment line: ' + line.trim());
    segs.push({ tool: Number(m[1]), slicer_mm: Number(m[2]) });
  }
  if (!segs.length) throw new Error('Add at least one segment');
  const body = { segments: segs };
  const dur = numOrNull(getv(name, 'start.duration', ''));
  const pre = numOrNull(getv(name, 'start.preheat', ''));
  if (Number.isNaN(dur) || Number.isNaN(pre)) throw new Error('Duration and preheat must be numbers');
  if (dur != null) body.duration_s = dur;
  if (pre != null) body.preheat_s = pre;
  const cc = String(getv(name, 'start.cc', '')).split(',').map((x) => x.trim()).filter(Boolean).map(Number);
  if (cc.some((x) => !Number.isFinite(x))) throw new Error('Color changes must be numbers');
  if (cc.length) body.color_changes_mm = cc;
  const fn = String(getv(name, 'start.name', 'part.bgcode')).trim();
  if (fn) body.name = fn;
  return body;
}

function loadBody(name, i) {
  const k = 'load' + i + '.';
  const src = getv(name, k + 'source', 'preset');
  const keep = src === 'keep';
  const body = { tool: i, source: keep ? null : src };
  for (const [f, key] of [['name', 'name'], ['color', 'color'], ['uuid', 'uuid']]) {
    const v = String(getv(name, k + f, f === 'name' && !keep ? 'PLA' : '')).trim();
    if (v) body[key] = v;
  }
  for (const [f, key] of [['feed', 'feed_mm'], ['purge', 'purge_mm']]) {
    const v = numOrNull(getv(name, k + f, ''));
    if (Number.isNaN(v)) throw new Error(key + ' must be a number');
    if (v != null) body[key] = v;
  }
  return body;
}

function onClick(e) {
  const b = e.target.closest('button[data-act]');
  if (!b || b.disabled) return;
  const name = b.closest('.card').dataset.p;
  const act = b.dataset.act;
  const tool = b.dataset.tool != null ? Number(b.dataset.tool) : null;
  const p = S && S.printers.find((x) => x.name === name);
  if (!p) return;
  const t = tool != null ? p.tools.find((x) => x.index === tool) : null;
  const go = (a, body, ok) => doAction(name, a, body, ok);
  const toast = (m) => showToast(cardEls.get(name).toast, m);
  const toggle = (f) => { const o = pst(name).open; o[f] = !o[f]; renderCard(name); };
  try {
    switch (act) {
      case 'toggle': return toggle(b.dataset.form);
      case 'load_form': return toggle('load' + tool);
      case 'pause': case 'resume': case 'cancel': case 'dismiss': case 'reboot': case 'home':
      case 'tap_through': case 'clear_jam':
        return go(act, {});
      case 'power_loss': return go('reboot', { keep_job: true, cold_bed: !!getv(name, 'cold', false) });
      case 'auto_accept': {
        const cur = p.auto_accept_model_check;
        return go('set_auto_accept', { value: cur == null ? true : cur ? false : null });
      }
      case 'start_submit':
        return go('start_print', startBody(name), () => { pst(name).open.start = false; renderCard(name); });
      case 'load_submit':
        return go('load', loadBody(name, tool), () => { pst(name).open['load' + tool] = false; renderCard(name); });
      case 'unload': case 'runout': case 'select_tool':
        return go(act, { tool });
      case 'extrude': case 'retract': {
        const mm = numOrNull(getv(name, 'mm', '10'));
        if (mm == null || Number.isNaN(mm)) return toast('Enter a number of mm');
        return go(act, { tool, mm });
      }
      case 'jam': return go('jam', { tool, unload: !!getv(name, 'jam' + tool, true) });
      case 'sensor_fault': return go('set_sensor_fault', { tool, value: !t.sensor_fault });
      case 'stuck_write': return go('set_stuck_write', { tool, value: !t.stuck_write });
      case 'tool_enabled': return go('set_tool_enabled', { tool, value: !t.enabled });
      case 'counter_submit': case 'eau_submit': {
        const eau = act === 'eau_submit';
        const pre = eau ? 'eau.' : 'counter.';
        const tl = Number(getv(name, pre + 'tool', p.tools.length ? p.tools[0].index : 0));
        const v = numOrNull(getv(name, pre + (eau ? 'value' : 'mm'), eau ? '0' : '10'));
        if (v == null || Number.isNaN(v)) return toast('Enter a number');
        return go(eau ? 'set_extruded_at_unload' : 'counter_step_back', eau ? { tool: tl, value: v } : { tool: tl, mm: v });
      }
      default: return undefined;
    }
  } catch (err) {
    toast(err.message);
    return undefined;
  }
}

function wireCards() {
  const host = $('cards');
  host.addEventListener('click', onClick);
  host.addEventListener('input', (e) => {
    const k = e.target.dataset.k;
    if (!k) return;
    const card = e.target.closest('.card');
    pst(card.dataset.p).v[k] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
    const lm = /^load(\d+)\.(name|source)$/.exec(k);
    if (lm) {
      const form = e.target.closest('.form');
      const sub = form && form.querySelector('button[data-act="load_submit"]');
      const srcEl = form && form.querySelector('[data-k="load' + lm[1] + '.source"]');
      const nmEl = form && form.querySelector('[data-k="load' + lm[1] + '.name"]');
      if (sub && srcEl && nmEl) sub.disabled = srcEl.value !== 'keep' && !nmEl.value.trim();
    }
  });
  host.addEventListener('change', (e) => {
    const sel = e.target.dataset.sel;
    if (!sel) return;
    const name = e.target.closest('.card').dataset.p;
    const val = e.target.value;
    e.target.blur();
    doAction(name, sel, { [e.target.dataset.field]: val }, () => renderCard(name));
  });
  host.addEventListener('focusout', (e) => {
    const card = e.target.closest && e.target.closest('.card');
    if (!card) return;
    const name = card.dataset.p;
    setTimeout(() => renderCard(name), 0);
  });
}

// ---- event log ----

function evKey(e) { return [e.t, e.printer, e.kind, e.text].join('|'); }

function addEvent(e, atEnd) {
  const key = evKey(e);
  if (logSeen.has(key)) return;
  logSeen.add(key);
  const row = { e, key };
  if (atEnd) logRows.push(row); else logRows.unshift(row);
  while (logRows.length > MAX_LOG) {
    const old = logRows.pop();
    logSeen.delete(old.key);
    openRows.delete(old.key);
  }
}

function kindOf(k) { return KINDS.includes(k) && k !== 'other' ? k : 'other'; }

function renderLog() {
  if (logPending) return;
  logPending = true;
  requestAnimationFrame(() => {
    logPending = false;
    const pf = logFilter.printer;
    const out = [];
    for (const { e, key } of logRows) {
      if (pf && e.printer !== pf) continue;
      if (!logFilter.kinds.has(kindOf(e.kind))) continue;
      if (!logFilter.polls && e.kind === 'request' && String(e.text).startsWith('GET')) continue;
      const body = e.data && e.data.body;
      const open = openRows.has(key);
      out.push('<tr><td class="t">' + fmtTime(e.t, true) + '</td><td>' + esc(e.printer || '-') + '</td><td>' + esc(e.kind) +
        '</td><td class="txt">' + esc(e.text) +
        (body != null ? ' <button type="button" class="disc" data-key="' + esc(key) + '">' + (open ? 'hide body' : 'show body') + '</button>' : '') +
        (body != null && open ? '<pre>' + esc(body) + '</pre>' : '') + '</td></tr>');
    }
    $('log-body').innerHTML = out.join('');
  });
}

function renderLogControls() {
  const sel = $('log-printer');
  const names = S ? S.printers.map((p) => p.name) : [];
  const sig = names.join('\n');
  if (sel.dataset.sig === sig) return;
  sel.dataset.sig = sig;
  sel.innerHTML = '<option value="">all</option>' + names.map((n) => '<option>' + esc(n) + '</option>').join('');
  sel.value = names.includes(logFilter.printer) ? logFilter.printer : '';
}

function wireLog() {
  $('log-kinds').innerHTML = KINDS.map((k) =>
    '<label><input type="checkbox" data-kind="' + k + '" checked>' + k + '</label>').join('');
  $('log-kinds').addEventListener('change', (e) => {
    const k = e.target.dataset.kind;
    if (!k) return;
    if (e.target.checked) logFilter.kinds.add(k); else logFilter.kinds.delete(k);
    renderLog();
  });
  $('log-polls').addEventListener('change', (e) => { logFilter.polls = e.target.checked; renderLog(); });
  $('log-printer').addEventListener('change', (e) => { logFilter.printer = e.target.value; renderLog(); });
  $('log-clear').onclick = () => {
    logRows.length = 0;
    logSeen.clear();
    openRows.clear();
    renderLog();
  };
  $('log-body').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-key]');
    if (!b) return;
    const k = b.dataset.key;
    if (openRows.has(k)) openRows.delete(k); else openRows.add(k);
    renderLog();
  });
}

// ---- data flow ----

function applyState(s) {
  const events = S && S.events;
  S = s;
  if (events && !s.events) S.events = events;
  renderSettings();
  renderLogControls();
  renderCards();
}

async function loadInitial() {
  try {
    const r = await fetch('/api/state');
    if (!r.ok) return;
    const s = await r.json();
    const evs = s.events || [];
    // Oldest first, so unshift leaves the newest on top.
    for (const e of evs) addEvent(e, false);
    applyState(s);
    renderLog();
  } catch (e) { /* the SSE stream reports the outage */ }
}

function connect() {
  const es = new EventSource('/api/events');
  es.onopen = () => { $('banner').hidden = true; loadInitial(); };
  es.onerror = () => {
    $('banner').hidden = false;
    if (es.readyState === 2) { es.close(); setTimeout(connect, 2000); }
  };
  es.addEventListener('state', (ev) => {
    try { applyState(JSON.parse(ev.data)); } catch (e) { console.error('bad state event', e); }
  });
  es.addEventListener('log', (ev) => {
    try { addEvent(JSON.parse(ev.data), false); renderLog(); } catch (e) { console.error('bad log event', e); }
  });
}

buildSettings();
wireSnapshots();
wireCards();
wireLog();
renderLog();
connect();
