// ── TASK MONITOR ──────────────────────────────────────────────────────────────
// Moved state.taskEventSource to state
// Moved state.taskStreamRetries to state
import { state } from './state.js';
import { CAT_LABELS, EL, EVENT, SVG_CHECK, SVG_WARN, SVG_X, _errHtml, _errMsg, _fmtAbs, _fmtRel, act, btnBusy, btnDone, emptyHtml, esc, escAttr, hydrateStyles, loc, pulseHeldBy, setBadge, setPulse, toast } from './ui.js';
import { _swrInvalidate, _swrRun, api, sessionEnded } from './api.js';
import { t, th } from './i18n.js';

// One task stream per tab. The guard used to sit BEFORE the ticket fetch, so
// a retry timer and showApp() (or the Plex login poll) could both pass it
// and open two EventSources — the one overwritten in state was never closed,
// rendering every update twice and holding a server subscription. Now: one
// opening promise, one retry timer, a re-check after the await, and an
// onerror that only clears the state slot if it still holds its own stream.
let _taskStreamOpening = null;
let _taskStreamRetry = null;

function _retryTaskStream(ms) {
  clearTimeout(_taskStreamRetry);
  _taskStreamRetry = setTimeout(startTaskStream, ms);
}

// The ticket fetch retried at a fixed 3 s forever, silently, through a server
// restart or an outage. It backs off now (3 s doubling to 30 s) and, from the
// third miss, says the stream is down instead of "Connecting…".
function _ticketFailed(why) {
  const n = ++state.taskStreamRetries;
  if (n >= 3) _showStreamDown(why);
  _retryTaskStream(Math.min(30000, 3000 * 2 ** (n - 1)));
}

function _showStreamDown(why) {
  const el = document.getElementById('tasks-list');
  if (el) el.innerHTML = `<p class="t-danger loading js-stream-down" role="status" aria-live="polite">
    ${SVG_WARN} ${th('activity.stream_down', {why})}
    <a class="t-amber" href="#" ${act('reloadPage', EVENT)} aria-label="${th('activity.reload_aria')}">${th('activity.reload_page')}</a>.
  </p>`;
}

export function stopTaskStream() {
  clearTimeout(_taskStreamRetry);
  _taskStreamRetry = null;
  try { state.taskEventSource?.close(); } catch {}
  state.taskEventSource = null;
}

export function startTaskStream() {
  if (state.taskEventSource || _taskStreamOpening) return _taskStreamOpening;
  _taskStreamOpening = _openTaskStream().finally(() => { _taskStreamOpening = null; });
  return _taskStreamOpening;
}

async function _openTaskStream() {
  const tok = state.token || localStorage.getItem('curatarr_token');
  // Signed out: nothing to stream. showApp() starts it again after sign-in.
  if (!tok) return;

  // Fetch a short-lived ticket so the JWT never appears in the URL
  let r;
  try {
    r = await fetch('/api/tasks/ticket', { headers: { 'Authorization': `Bearer ${tok}` } });
  } catch { _ticketFailed('network'); return; }
  // An ended session can never get a ticket. This used to return silently
  // ("the next api() call surfaces the sign-out" - nothing did), leaving
  // Activity on "Connecting…" or on frozen Running rows with live Cancel
  // buttons. It ends the session the way api() does.
  if (r.status === 401) { sessionEnded(await r.text().catch(() => '')); return; }
  if (!r.ok) { _ticketFailed(`HTTP ${r.status}`); return; }
  let ticket;
  try { ticket = (await r.json()).ticket; } catch { _ticketFailed('bad ticket'); return; }
  if (state.taskEventSource) return;        // another opener won while we awaited

  const es = new EventSource(`/api/tasks/stream?ticket=${encodeURIComponent(ticket)}`);
  state.taskEventSource = es;

  es.onopen = () => {
    state.taskStreamRetries = 0;
    const el = document.getElementById('tasks-list');
    if (el && (el.querySelector('.js-stream-down') || /Connecting|disconnected/.test(el.textContent))) el.innerHTML = '';
  };

  es.onmessage = (e) => {
    let data;
    try { data = JSON.parse(e.data); } catch (err) {
      console.warn('[activity] unparseable task event', err);   // the next event repaints
      return;
    }
    const tasks = Array.isArray(data) ? data : [];
    // Only the parse is expected to fail; a render bug must be seen, and one
    // broken renderer must not stop the other two.
    for (const render of [renderTasks, updateTaskBadge, updateEnrichLiveSection]) {
      try { render(tasks); } catch (err) { console.error(`[activity] ${render.name} failed`, err); }
    }
  };

  // A ticket is single-use: the browser's own reconnect would replay the
  // spent one and get a 401, so every error closes this stream and opens a
  // fresh one with a new ticket.
  es.onerror = () => {
    es.close();
    if (state.taskEventSource !== es) return;   // already replaced or stopped
    state.taskEventSource = null;
    state.taskStreamRetries++;

    // After 3 retries, show it and back off
    if (state.taskStreamRetries >= 3) {
      _showStreamDown('connection lost');
      state.taskStreamRetries = 0; // reset so reload works
      _retryTaskStream(30000);     // retry after 30s
    } else {
      _retryTaskStream(3000 * state.taskStreamRetries);
    }
  };
}

// ── ACTIVITY VIEW (2026-11, Sonarr/Radarr layout) ────────────────────────────
// Three tabs: Queue (what runs or waits now — the SSE stream), Scheduled
// (every background job: cadence, last / next execution, Run now) and
// History (finished runs, filterable). A task that ends stays in the Queue
// for a moment to show its outcome, fades, and moves to the History; the
// Queue never fills up with "Done" rows again.

const LIVE = new Set(['running', 'queued']);
// Outcome codes and areas are identifiers; their words are in the dictionary
// (activity.outcome.*, activity.area.*), read when drawn.
const _BADGE = {running: 'amber', queued: 'muted', completed: 'success', warning: 'amber', partial: 'amber',
  failed: 'danger', cancelled: 'muted', skipped: 'muted'};
const OUTCOME = Object.fromEntries(Object.entries(_BADGE).map(([k, badge]) => [k, Object.defineProperty({badge}, 'label',
  {get: () => t(`activity.outcome.${k}`), enumerable: true})]));
const _AREAS = ['sync', 'enrichment', 'taste', 'maintenance', 'system'];
const AREA_LABELS = Object.fromEntries(_AREAS.map(a => [a, null]));
const _areaLabel = a => (a in AREA_LABELS ? t(`activity.area.${a}`) : a || '');
const FADE_MS = 2600;
const SVG_CLOCK = '<svg class="icon-inline" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';

const _isAdmin = () => !!state.currentUser?.is_admin;
const _outcomeOf = x => x.outcome || ({running: 'running', pending: 'queued', done: 'completed', error: 'failed', skipped: 'cancelled'})[x.status] || 'completed';
const _isLive = x => LIVE.has(_outcomeOf(x));

function _store(key, val) {
  try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch { /* private window */ }
  return null;
}

export function fmtTime(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (!s) return '';
  if (s < 60) return t('activity.fmt_s', {s});
  if (s < 3600) return t('activity.fmt_ms', {m: Math.floor(s/60), s: s%60});
  return t('activity.fmt_hm', {h: Math.floor(s/3600), m: Math.floor((s%3600)/60)});
}

// The server says a job's interval in a closed set of English phrases
// (services/activity fmt_cadence / _trigger_cadence, the custodian's tick);
// they are UI words, so they are said in the UI language here. Anything
// else (a cron spec) is shown as it came.
const _CADENCE = [
  [/^on demand$/, () => t('activity.fmt_on_demand')],
  [/^daily$/, () => t('activity.fmt_daily')],
  [/^weekly$/, () => t('activity.fmt_weekly')],
  [/^hourly$/, () => t('activity.fmt_hourly')],
  [/^once after start$/, () => t('activity.fmt_once')],
  [/^every (\d+) min$/, m => t('activity.fmt_every_min', {n: +m[1], x: m[1]})],
  [/^every (\d+) days$/, m => t('activity.fmt_every_days', {n: +m[1], x: m[1]})],
  [/^every (\d+) s$/, m => t('activity.fmt_every_s', {n: +m[1], x: m[1]})],
  [/^every ([\d.]+) h$/, m => t('activity.fmt_every_h', {n: +m[1], x: Number(m[1]).toLocaleString(loc())})],
  [/^hourly at :(\d\d)$/, m => t('activity.fmt_hourly_at', {mm: m[1]})],
];
export function fmtCadence(text) {
  const s = String(text ?? '');
  const sprint = / \(sprint\)$/.test(s);
  const core = sprint ? s.slice(0, -' (sprint)'.length) : s;
  for (const [re, say] of _CADENCE) {
    const m = core.match(re);
    if (m) return sprint ? t('activity.fmt_sprint', {x: say(m)}) : say(m);
  }
  return s;
}

// The scheduler's notes beside a job (services/activity): a closed set of
// UI phrases, said in the UI language; anything else as it came.
const _NOTES = {'running now': 'activity.note.running_now', 'queued': 'activity.note.queued',
  'due — next custodian tick': 'activity.note.due_next_tick', 'due now': 'activity.note.due_now', 'paused': 'activity.note.paused',
  'waiting for the GPU (a game holds it)': 'activity.note.wait_gpu', 'waiting for the PC agent': 'activity.note.wait_agent',
  'backing off after a failed run': 'activity.note.backoff'};
export const fmtNote = s => (_NOTES[s] ? t(_NOTES[s]) : String(s ?? ''));

function _num(n) { return (Number(n) || 0).toLocaleString(loc()); }
// one decimal in the UI's locale, no grouping (as toFixed(1) wrote it)
const _dec1 = v => v.toLocaleString(loc(), {minimumFractionDigits: 1, maximumFractionDigits: 1, useGrouping: false});

function _tagPills(tags) {
  return (tags || []).map(c => `<span class="act-tag">${esc(CAT_LABELS[c] || c)}</span>`).join('');
}

function _outcomeBadge(o, label) {
  const m = OUTCOME[o] || OUTCOME.completed;
  return `<span class="badge ${m.badge} badge-sm">${esc(label || m.label)}</span>`;
}

function _icon(o) {
  if (o === 'running') return '<span class="act-spin" aria-hidden="true"></span>';
  if (o === 'queued') return `<span class="t3" aria-hidden="true">${SVG_CLOCK}</span>`;
  if (o === 'failed') return `<span class="t-danger" aria-hidden="true">${SVG_X}</span>`;
  if (o === 'cancelled' || o === 'skipped') return `<span class="t3" aria-hidden="true">${SVG_X}</span>`;
  if (o === 'partial' || o === 'warning') return `<span class="t-amber" aria-hidden="true">${SVG_WARN}</span>`;
  return `<span class="t-success" aria-hidden="true">${SVG_CHECK}</span>`;
}

// One progress bar for the queue: a width when the count is known, a pulse
// when it is not (a job with no item count never sits at "0%" again).
function _barHtml(pct, indeterminate, hidden) {
  return `<div class="progress-bar act-bar js-bar${indeterminate ? ' indeterminate' : ''}"${hidden ? ' hidden' : ''}><div class="progress-fill" data-style="width:${indeterminate ? 100 : Math.max(0, Math.min(100, pct || 0))}%"></div></div>`;
}

// ── Queue ────────────────────────────────────────────────────────────────────

// When this tab first saw a run finished (client clock): the row fades and
// leaves FADE_MS later, whatever the server's grace window says.
const _finishedSeen = new Map();
let _fadeTimer = 0;
let _lastLive = [];

// The Deletions entry pulses while a deletion analysis runs ANYWHERE —
// the task id is del-analysis-<user>, whichever tab pressed Analyse. When
// it ends and this tab did not start it, the Deletions view still shows
// the old proposals: offer the reload rather than replacing a list the
// user may be working in.
let _delAnalysisSeen = false;
export function updateTaskBadge(tasks) {
  const top = tasks.filter(x => !x.parent_id && (x.status === 'running' || x.status === 'pending'));
  setBadge('tasks-badge', top.length);
  setBadge('act-count-queue', top.length);
  const running = tasks.some(x => (x.status === 'running' || x.status === 'pending') && String(x.id || '').startsWith('del-analysis-'));
  setPulse('sb-deletions-pulse', 'stream', running);
  if (_delAnalysisSeen && !running && !pulseHeldBy('sb-deletions-pulse', 'local')
      && document.getElementById('deletions-view')?.classList.contains('active')) {
    toast(t('activity.del_analysis_done'), 'info', {actions: [
      {label: t('activity.reload_proposals'), primary: true, onClick: () => import('./deletions.js').then(m => m.reloadDeletions())},
      {label: t('activity.later')},
    ]});
  }
  _delAnalysisSeen = running;
}

// Rows are patched in place, keyed by run, and renders are coalesced to one
// per animation frame. Rebuilding the list on every progress event re-ran
// the card entrance animation on every row several times a second: the
// taste-vector recompute strobed the page (2026-09-13).
let _tasksPending = null;
let _tasksRaf = 0;
export function renderTasks(tasks) {
  _tasksPending = tasks;
  if (_tasksRaf) return;
  _tasksRaf = requestAnimationFrame(() => { _tasksRaf = 0; _renderTasksNow(_tasksPending || []); });
}

function _metaLine(x) {
  const o = _outcomeOf(x);
  if (o !== 'running') return '';
  return [
    x.total > 0 ? `${_num(x.processed)} / ${_num(x.total)}` : '',
    _rate(x.rate),
    x.eta_s ? `<span class="t-amber">${th('activity.eta', {time: fmtTime(x.eta_s)})}</span>` : '',
  ].filter(Boolean).join(' · ');
}

// A slow job read "0.0/s": below one item per ten seconds the rate is per minute.
function _rate(r) {
  if (!(r > 0)) return '';
  return r >= 0.1 ? t('activity.rate_s', {x: _dec1(r)})
    : t('activity.rate_min', {x: _dec1(r * 60)});
}

// Sub-steps of a grouped run (the per-category taste vectors of one
// recompute) as one compact badge row instead of rows of their own.
function _subStepsHtml(run, kids) {
  const seen = new Set();
  const pills = kids.map(k => {
    const label = (k.tags && k.tags[0]) || k.name;
    seen.add(label);
    const o = _outcomeOf(k);
    const extra = o === 'running' && k.total > 0 ? ` ${th('activity.pct', {pct: k.progress})}` : '';
    return `<span class="act-sub s-${o}" title="${escAttr(`${k.name}: ${OUTCOME[o]?.label || o}`)}">${o === 'running' ? '<span class="act-spin sm" aria-hidden="true"></span>' : _icon(o)}${esc(CAT_LABELS[label] || label)}${extra}</span>`;
  });
  if (_isLive(run) && kids.length) {          // a grouped run: the steps still to come
    for (const tag of run.tags || []) {
      if (!seen.has(tag)) pills.push(`<span class="act-sub s-queued" title="${th('activity.waiting')}">${SVG_CLOCK}${esc(CAT_LABELS[tag] || tag)}</span>`);
    }
  }
  return pills.join('');
}

// A run's name, its error and summary and its log lines are the server's
// (job names are technical identifiers): they pass through as they came.
function _stepLine(run) {
  const o = _outcomeOf(run);
  if (!LIVE.has(o)) return run.error || run.summary || '';
  if (o === 'queued') return t('activity.waiting_slot');
  return run.logs?.slice(-1)[0]?.msg || '';
}

function _queueRowHtml(run, kids) {
  const o = _outcomeOf(run);
  const live = LIVE.has(o);
  const meta = _metaLine(run);
  const step = _stepLine(run);
  const subs = _subStepsHtml(run, kids);
  const pct = live ? (run.progress || 0) : 100;
  const showBar = o === 'running' || (!live && o !== 'cancelled' && o !== 'skipped' && o !== 'failed');
  const pctLabel = o === 'running' && !run.indeterminate ? `<span class="t-amber b fs-12">${th('activity.pct', {pct: run.progress || 0})}</span>` : '';
  return `<div class="act-row act-${o}${live ? '' : ' act-fade'}" role="listitem" data-run-id="${escAttr(String(run.run_id || run.id))}" data-started="${escAttr(run.started_at || '')}">
      <div class="act-ico js-ico">${_icon(o)}</div>
      <div class="act-main">
        <div class="act-title js-title"><span class="b">${esc(run.name)}</span>${kids.length ? '' : _tagPills(run.tags)}${_outcomeBadge(o)}${pctLabel}</div>
        <div class="act-step ellipsis js-step"${step ? '' : ' hidden'} title="${escAttr(step)}">${esc(step)}</div>
        ${_barHtml(pct, !!run.indeterminate, !showBar)}
        <div class="act-meta js-meta"${meta ? '' : ' hidden'}>${meta}</div>
        <div class="act-subs js-subs"${subs ? '' : ' hidden'}>${subs}</div>
      </div>
      <div class="act-side">
        <span class="act-elapsed mono js-elapsed">${fmtTime(run.elapsed_s)}</span>
        <span class="js-actions"${live ? '' : ' hidden'}><button type="button" class="btn btn-secondary btn-sm" ${act('cancelTask', run.id, EL)} aria-label="${th('activity.cancel_aria', {name: run.name})}">${th('common.cancel')}</button></span>
      </div>
    </div>`;
}

function _patchRow(row, fresh) {
  if (row.className !== fresh.className) row.className = fresh.className;
  row.dataset.started = fresh.dataset.started;
  for (const sel of ['.js-ico', '.js-title', '.js-step', '.js-meta', '.js-subs', '.js-elapsed']) {
    const a = row.querySelector(sel), b = fresh.querySelector(sel);
    if (a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
    a.hidden = b.hidden;
    if (b.title !== undefined && a.title !== b.title) a.title = b.title;
  }
  const pa = row.querySelector('.js-bar'), pb = fresh.querySelector('.js-bar');
  pa.hidden = pb.hidden;
  pa.classList.toggle('indeterminate', pb.classList.contains('indeterminate'));
  hydrateStyles(pb);
  pa.firstElementChild.style.width = pb.firstElementChild.style.width;
  // The Cancel button is never replaced: a "Cancelling…" busy state survives the next tick.
  row.querySelector('.js-actions').hidden = fresh.querySelector('.js-actions').hidden;
}

function _renderTasksNow(tasks) {
  _lastLive = tasks;
  const el = document.getElementById('tasks-list');
  if (!el) return;
  const now = Date.now();
  const ids = new Set(tasks.map(x => x.id));
  const kidsOf = new Map();
  for (const x of tasks) {
    if (x.parent_id && ids.has(x.parent_id)) {
      if (!kidsOf.has(x.parent_id)) kidsOf.set(x.parent_id, []);
      kidsOf.get(x.parent_id).push(x);
    }
  }
  let finishedNow = false;
  const visible = [];
  for (const x of tasks) {
    if (x.parent_id && ids.has(x.parent_id)) continue;
    const key = String(x.run_id || x.id);
    if (_isLive(x)) { _finishedSeen.delete(key); visible.push(x); continue; }
    if (!_finishedSeen.has(key)) { _finishedSeen.set(key, now); finishedNow = true; }
    if (now - _finishedSeen.get(key) < FADE_MS) visible.push(x);
  }
  if (_finishedSeen.size > 200) _finishedSeen.clear();
  if (finishedNow) _onRunFinished();
  // Re-render when the next fading row is due to leave.
  clearTimeout(_fadeTimer);
  if (visible.some(x => !_isLive(x))) _fadeTimer = setTimeout(() => renderTasks(_lastLive), 400);

  const liveCount = visible.filter(_isLive).length;
  const hint = document.getElementById('act-queue-hint');
  if (hint) hint.textContent = liveCount ? t('activity.n_active', {n: liveCount}) : t('activity.idle');

  if (!visible.length) {
    if (!el.querySelector(':scope > .empty')) {
      el.innerHTML = emptyHtml(th('activity.queue_empty'), null, null, {good: true});
    }
    _syncElapsedTicker();
    return;
  }
  let list = el.querySelector(':scope > .act-list');
  if (!list) {
    el.innerHTML = `<div class="act-list" role="list" aria-label="${th('activity.active_aria')}"></div>`;
    list = el.firstElementChild;
  }
  const rows = new Map([...list.children].map(r => [r.dataset.runId, r]));
  const scratch = document.createElement('div');
  let prev = null;
  for (const x of visible) {
    const key = String(x.run_id || x.id);
    scratch.innerHTML = _queueRowHtml(x, kidsOf.get(x.id) || []);
    const fresh = scratch.firstElementChild;
    let row = rows.get(key);
    if (row) { _patchRow(row, fresh); rows.delete(key); }
    else row = fresh;
    const want = prev ? prev.nextSibling : list.firstChild;
    if (row !== want) list.insertBefore(row, want);
    prev = row;
  }
  for (const r of rows.values()) r.remove();
  _syncElapsedTicker();
}

// Running rows count their elapsed time up every second, between the
// server's progress events (a job that reports rarely looked frozen).
let _elapsedTimer = 0;
function _syncElapsedTicker() {
  const need = !!document.querySelector('#tasks-list .act-running');
  if (need && !_elapsedTimer) {
    _elapsedTimer = setInterval(() => {
      const rows = document.querySelectorAll('#tasks-list .act-running');
      if (!rows.length) { clearInterval(_elapsedTimer); _elapsedTimer = 0; return; }
      for (const r of rows) {
        const t0 = Date.parse(r.dataset.started || '');
        if (!isNaN(t0)) r.querySelector('.js-elapsed').textContent = fmtTime((Date.now() - t0) / 1000);
      }
    }, 1000);
  } else if (!need && _elapsedTimer) {
    clearInterval(_elapsedTimer); _elapsedTimer = 0;
  }
}

// A run just finished: the History and the Scheduled tab have news.
let _refreshTimer = 0;
function _onRunFinished() {
  clearTimeout(_refreshTimer);
  _refreshTimer = setTimeout(() => {
    if (!document.getElementById('tasks-view')?.classList.contains('active')) {
      _swrInvalidate('tasks');
      return;
    }
    loadHistory(true);
    if (_isAdmin()) loadScheduled(true);
  }, 1500);
}

export async function cancelTask(taskId, btn) {
  btnBusy(btn, t('activity.cancelling'));
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, 'POST');
    _swrInvalidate('tasks'); // next visit reflects the cancellation instead of a stale pre-cancel snapshot
    toast(t('activity.cancel_requested'), 'info');
  } catch (e) {
    toast(_errMsg(e), 'danger');
    btnDone(btn);
  }
}

// ── tabs ─────────────────────────────────────────────────────────────────────

const TABS = ['queue', 'scheduled', 'history'];
let _tab = null;
let _schedTimer = 0;

function _selectTab(name) {
  if (name === 'scheduled' && !_isAdmin()) name = 'queue';
  if (!TABS.includes(name)) name = 'queue';
  _tab = name;
  for (const n of TABS) {
    const tab = document.getElementById(`act-tab-${n}`);
    const panel = document.getElementById(`act-panel-${n}`);
    if (tab) { tab.classList.toggle('active', n === name); tab.setAttribute('aria-selected', String(n === name)); tab.tabIndex = n === name ? 0 : -1; }
    if (panel) panel.hidden = n !== name;
  }
  _store('curatarr_activity_tab', name);
  clearInterval(_schedTimer);
  _schedTimer = 0;
  if (name === 'scheduled') {
    loadScheduled();
    // Relative times and next runs move: refresh while the tab is open.
    _schedTimer = setInterval(() => {
      if (_tab !== 'scheduled' || !document.getElementById('tasks-view')?.classList.contains('active')) {
        clearInterval(_schedTimer); _schedTimer = 0; return;
      }
      loadScheduled(true);
    }, 30000);
  }
  if (name === 'history') loadHistory();
}

export function showActivityTab(name) { _selectTab(name); }

// Arrow keys move along the tab row (the WAI tabs pattern).
export function activityTabKey(event) {
  const i = TABS.indexOf(_tab || 'queue');
  const order = _isAdmin() ? TABS : TABS.filter(n => n !== 'scheduled');
  let j = order.indexOf(TABS[i]);
  if (event.key === 'ArrowRight') j = (j + 1) % order.length;
  else if (event.key === 'ArrowLeft') j = (j - 1 + order.length) % order.length;
  else return;
  event.preventDefault();
  _selectTab(order[j]);
  document.getElementById(`act-tab-${order[j]}`)?.focus();
}

// The view's entry (nav.js): pick the tab, load what it shows. Named for
// the old "Last run per type" table it replaces — nav and the retry button
// still call it.
export async function loadTaskHistory() {
  const sched = document.getElementById('act-tab-scheduled');
  if (sched) sched.hidden = !_isAdmin();
  _applyFilterUi();
  _selectTab(_tab || _store('curatarr_activity_tab') || 'queue');
  // The History badge (failures of the last day) wants the data even on another tab.
  if (_tab !== 'history') loadHistory(true);
  if (_tab !== 'scheduled' && _isAdmin()) loadScheduled(true);
}

// ── Scheduled ────────────────────────────────────────────────────────────────

let _schedData = null;

function _resultCell(j) {
  if (!j.last_result) return '<span class="t3">—</span>';
  const map = {
    done: ['success', 'activity.result.done'], partial: ['amber', 'activity.result.partial'],
    error: ['danger', 'activity.result.error'], waiting: ['muted', 'activity.result.waiting'], skipped: ['muted', 'activity.result.skipped'],
  };
  const [cls, key] = map[j.last_result] || ['muted', null];
  const label = key ? t(key) : j.last_result;
  // the summary is the server's (English, like the job's name); its "Partial…" lead is the badge already
  const summary = j.last_summary && !(j.last_result === 'partial' && /^Partial/.test(j.last_summary)) ? j.last_summary : '';
  return `<span class="badge ${cls} badge-sm">${esc(label)}</span>${summary ? ` <span class="t3 fs-12 act-sum" title="${escAttr(summary)}">${esc(summary)}</span>` : ''}`;
}

function _nextCell(j) {
  if (j.state === 'running') return `<span class="badge amber badge-sm"><span class="act-spin sm" aria-hidden="true"></span>${th('activity.outcome.running')}</span>`;
  if (j.state === 'queued') return `<span class="badge muted badge-sm">${SVG_CLOCK} ${th('activity.outcome.queued')}</span>`;
  if (j.blocked) return `<span class="t-amber fs-12">${esc(fmtNote(j.blocked))}</span>`;
  if (j.due) {
    const when = j.next_run ? _fmtRel(j.next_run) : '';
    return `<span class="t-amber" title="${escAttr(j.next_run ? _fmtAbs(j.next_run) : '')}">${th('activity.due_now')}</span>${when && when !== t('time.now') ? ` <span class="t3 fs-12">· ${th('activity.next_tick', {when})}</span>` : ''}`;
  }
  if (!j.next_run) return `<span class="t3">${esc(fmtNote(j.next_note) || '—')}</span>`;
  return `<span title="${escAttr(_fmtAbs(j.next_run))}">${esc(_fmtRel(j.next_run) || t('time.now'))}</span>`;
}

function _schedRowHtml(j) {
  const last = j.last_run
    ? `<span title="${escAttr(_fmtAbs(j.last_run))}">${esc(_fmtRel(j.last_run) || t('time.now'))}</span>`
    : `<span class="t3">${th('activity.never')}</span>`;
  const dur = j.last_duration_s != null && j.last_duration_s > 0 ? fmtTime(j.last_duration_s) : (j.last_duration_s === 0 ? th('activity.under_1s') : '—');
  const busy = j.state !== 'idle';
  return `<tr data-job="${escAttr(j.job_id)}">
      <td class="cell-primary"><span class="b">${esc(j.name)}</span>${j.uses_model ? ` <span class="badge theme badge-sm" title="${th('activity.model_title')}">${th('activity.model')}</span>` : ''}</td>
      <td class="t2 nowrap">${esc(fmtCadence(j.cadence))}</td>
      <td class="nowrap">${last}</td>
      <td class="t2 nowrap">${dur}</td>
      <td class="act-result">${_resultCell(j)}</td>
      <td class="nowrap">${_nextCell(j)}</td>
      <td class="t-right"><button type="button" class="btn btn-secondary btn-sm" ${act('runScheduledJob', j.job_id, EL)}${j.can_run ? '' : ' disabled'} aria-label="${th('activity.run_aria', {name: j.name})}" title="${th(busy ? 'activity.in_queue_title' : j.can_run ? 'activity.run_now_title' : 'activity.not_available')}">${th(busy ? 'activity.in_queue' : 'activity.run_now')}</button></td>
    </tr>`;
}

function _renderScheduled(d) {
  _schedData = d;
  const el = document.getElementById('tasks-scheduled');
  if (!el) return;
  const jobs = (d.groups || []).flatMap(g => g.jobs);
  // The badge: jobs due or running now (a job due on every tick says nothing).
  const due = jobs.filter(j => j.state !== 'idle' || (j.due && !(j.cadence_h != null && j.cadence_h < 0.55))).length;
  setBadge('act-count-scheduled', due);
  if (!jobs.length) { el.innerHTML = emptyHtml(th('activity.sched_empty')); return; }
  const c = d.custodian || {};
  // queue names and job names are identifiers: as they came
  const busy = Object.entries(c.queues || {}).filter(([, q]) => q.busy).map(([n, q]) => t('activity.queue_on', {name: n, job: q.current || '…'}));
  const next = c.next_tick ? th('activity.tick_next', {when: '\u0000'}).replace('\u0000', `<span title="${escAttr(_fmtAbs(c.next_tick))}">${esc(_fmtRel(c.next_tick))}</span>`) : '';
  const head = `<p class="t3 fs-12 mb-8">${th('activity.tick_head', {tick: fmtCadence(c.tick || '')})}${next ? `, ${next}` : ''})${busy.length ? ` — ${esc(busy.join(', '))}` : ''}.</p>`;
  // a group's name by its area id (the server's label where the id is new)
  const groupLabel = g => (_AREAS.includes(g.area) ? t(`activity.group.${g.area}`) : g.label);
  el.innerHTML = head + d.groups.map(g => `<section class="section">
      <div class="section-head"><h3>${esc(groupLabel(g))} <span class="badge muted badge-sm">${g.jobs.length}</span></h3></div>
      <div class="section-body"><div class="tbl-wrap"><table class="tbl tbl-cards act-sched"><thead><tr><th>${th('activity.col.task')}</th><th>${th('activity.col.interval')}</th><th>${th('activity.col.last_run')}</th><th>${th('activity.col.last_duration')}</th><th>${th('activity.col.last_result')}</th><th>${th('activity.col.next_run')}</th><th class="t-right"><span class="sr-only">${th('activity.col.actions')}</span></th></tr></thead>
      <tbody>${g.jobs.map(_schedRowHtml).join('')}</tbody></table></div></div></section>`).join('');
}

export async function loadScheduled(quiet) {
  const el = document.getElementById('tasks-scheduled');
  if (!el || !_isAdmin()) return;
  try {
    await _swrRun('tasks-scheduled', () => api('/api/tasks/scheduled'), _renderScheduled);
  } catch (e) {
    if (!quiet || !_schedData) el.innerHTML = _errHtml(e, act('loadScheduled'));
  }
}

export async function runScheduledJob(jobId, btn) {
  btnBusy(btn, t('activity.starting'));
  try {
    const r = await api(`/api/tasks/scheduled/${encodeURIComponent(jobId)}/run`, 'POST');
    toast(t(r.state === 'queued' ? 'activity.queued_toast' : 'activity.started_toast'), 'info');
    btnDone(btn, t('activity.in_queue'), {keepDisabled: true});
  } catch (e) {
    toast(e.status === 409 ? t('activity.already_running') : _errMsg(e), e.status === 409 ? 'info' : 'danger');
    btnDone(btn);
  }
  _swrInvalidate('tasks-scheduled');
  setTimeout(() => loadScheduled(true), 1200);
}

// ── History ──────────────────────────────────────────────────────────────────

const STATUS_FILTERS = {
  all: () => true,
  completed: r => r.outcome === 'completed',
  warn: r => r.outcome === 'partial' || r.outcome === 'warning',
  failed: r => r.outcome === 'failed' || r.outcome === 'cancelled',
};
const _filter = {status: 'all', area: 'all', hideNoop: true};
try {
  const saved = JSON.parse(_store('curatarr_activity_filter') || 'null');
  if (saved && typeof saved === 'object') {
    if (saved.status in STATUS_FILTERS) _filter.status = saved.status;
    if (saved.area === 'all' || saved.area in AREA_LABELS) _filter.area = saved.area;
    if (typeof saved.hideNoop === 'boolean') _filter.hideNoop = saved.hideNoop;
  }
} catch { /* a bad saved filter is no filter */ }

let _runs = null;

function _applyFilterUi() {
  for (const b of document.querySelectorAll('#act-panel-history [data-filter-kind]')) {
    const on = _filter[b.dataset.filterKind] === b.dataset.filterValue;
    b.classList.toggle('active', on);
    b.setAttribute('aria-pressed', String(on));
  }
  const cb = document.getElementById('act-hide-noop');
  if (cb) cb.checked = _filter.hideNoop;
}

export function setHistoryFilter(kind, value) {
  if (kind === 'status' && value in STATUS_FILTERS) _filter.status = value;
  if (kind === 'area' && (value === 'all' || value in AREA_LABELS)) _filter.area = value;
  _store('curatarr_activity_filter', JSON.stringify(_filter));
  _applyFilterUi();
  if (_runs) _renderHistory(_runs);
}

export function toggleHideNoop(cb) {
  _filter.hideNoop = !!cb.checked;
  _store('curatarr_activity_filter', JSON.stringify(_filter));
  if (_runs) _renderHistory(_runs);
}

function _stepsHtml(r) {
  if (!r.steps?.length) return '';
  return `<div class="act-subs mt-4">${r.steps.map(s => {
    const label = (s.tags && s.tags[0]) || s.name;
    const o = s.outcome || 'completed';
    const tip = `${s.name}: ${OUTCOME[o]?.label || o}${s.elapsed_s ? ` · ${fmtTime(s.elapsed_s)}` : ''}${s.summary ? ` — ${s.summary}` : ''}`;   // name and summary: the server's
    return `<span class="act-sub s-${o}" title="${escAttr(tip)}">${_icon(o)}${esc(CAT_LABELS[label] || label)}</span>`;
  }).join('')}</div>`;
}

function _historyRowHtml(r) {
  const o = r.outcome || 'completed';
  const when = r.finished_at || r.started_at;
  const count = r.total > 0 && r.processed < r.total ? th('activity.items_of', {n: r.total, done: _num(r.processed), total: _num(r.total)})
    : r.total > 0 ? th('activity.items', {n: r.total, total: _num(r.total)}) : '';
  const summary = r.error || r.summary || '';
  const logs = (r.logs || []).filter(l => l.msg && l.msg !== summary);
  const details = logs.length ? `<details class="act-log mt-4"><summary class="t3 fs-11">${th('activity.log')}</summary><div class="mono fs-11 t3">${logs.map(l => `<div class="${l.level === 'error' ? 't-danger' : l.level === 'warn' ? 't-amber' : ''}">${esc(l.msg)}</div>`).join('')}</div></details>` : '';
  return `<tr class="${r.noop ? 'act-noop' : ''}">
      <td class="cell-primary"><div class="act-title"><span class="act-hico">${_icon(o)}</span><span class="b">${esc(r.name)}</span>${r.steps?.length ? '' : _tagPills(r.tags)}</div>${summary ? `<div class="fs-12 t2 act-sum" title="${escAttr(summary)}">${esc(summary)}</div>` : ''}${_stepsHtml(r)}${details}</td>
      <td class="t2 nowrap">${esc(_areaLabel(r.area))}</td>
      <td class="nowrap">${when ? `<span title="${escAttr(_fmtAbs(when))}">${esc(_fmtRel(when) || t('time.now'))}</span>` : '—'}</td>
      <td class="t2 nowrap t-right">${r.elapsed_s >= 1 ? fmtTime(r.elapsed_s) : th('activity.under_1s')}</td>
      <td class="nowrap">${_outcomeBadge(o)}${count ? ` <span class="t3 fs-12">${count}</span>` : ''}</td>
    </tr>`;
}

function _renderHistory(d) {
  _runs = d;
  const el = document.getElementById('tasks-history');
  if (!el) return;
  const runs = d.runs || [];
  const day = Date.now() - 86400e3;
  setBadge('act-count-history', runs.filter(r => r.outcome === 'failed' && Date.parse(r.finished_at || '') > day).length);
  const byStatus = runs.filter(STATUS_FILTERS[_filter.status] || STATUS_FILTERS.all)
    .filter(r => _filter.area === 'all' || r.area === _filter.area);
  const shown = _filter.hideNoop ? byStatus.filter(r => !r.noop || r.outcome === 'failed') : byStatus;
  const hidden = byStatus.length - shown.length;
  const count = document.getElementById('act-history-count');
  if (count) count.textContent = runs.length
    ? `${t('activity.runs_shown', {n: runs.length, shown: shown.length, total: runs.length})}${hidden ? ` · ${t('activity.empty_hidden', {n: hidden})}` : ''}` : '';
  if (!runs.length) { el.innerHTML = emptyHtml(th('activity.history_empty')); return; }
  if (!shown.length) {
    el.innerHTML = emptyHtml(hidden ? th('activity.only_empty', {n: hidden}) : th('activity.no_match'),
      hidden ? t('activity.show_empty') : null, hidden ? act('showNoopRuns') : null);
    return;
  }
  el.innerHTML = `<div class="tbl-wrap"><table class="tbl tbl-cards act-hist"><thead><tr><th>${th('activity.col.task')}</th><th>${th('activity.col.area')}</th><th>${th('activity.col.finished')}</th><th class="t-right">${th('activity.col.duration')}</th><th>${th('activity.col.result')}</th></tr></thead>
    <tbody>${shown.map(_historyRowHtml).join('')}</tbody></table></div>`;
}

export function showNoopRuns() {
  _filter.hideNoop = false;
  _store('curatarr_activity_filter', JSON.stringify(_filter));
  _applyFilterUi();
  if (_runs) _renderHistory(_runs);
}

export async function loadHistory(quiet) {
  const el = document.getElementById('tasks-history');
  if (!el) return;
  try {
    await _swrRun('tasks', () => api('/api/tasks/history'), _renderHistory);
  } catch (e) {
    if (!quiet || !_runs) el.innerHTML = _errHtml(e, act('loadTaskHistory'));
  }
}

function _enrichRowHtml(run) {
  const cat = run.category.replace('enrich-', '');
  const pct = run.progress || 0;
  const eta = run.eta_s ? ` · ${esc(t('activity.eta', {time: fmtTime(Math.round(run.eta_s))}))}` : '';
  const rate = run.rate > 0 ? ` · ${esc(t('activity.rate_s', {x: _dec1(run.rate)}))}` : '';
  const current = run.logs && run.logs.length ? run.logs[run.logs.length - 1].msg : '';
  return `<div class="mb-8" data-enrich-cat="${escAttr(run.category)}">
        <div class="row fs-12 js-count-line"><span class="b">${esc(CAT_LABELS[cat] || cat)}</span><span class="t3 row-end">${(run.processed || 0).toLocaleString(loc())} / ${(run.total || 0).toLocaleString(loc())}${rate}${eta}</span></div>
        <div class="mt-4 progress-bar"><div class="progress-fill" data-style="width:${pct}%"></div></div>
        <div class="ellipsis fs-10 t3 mono mt-4 js-current"${current ? '' : ' hidden'} title="${escAttr(current)}">${esc(current)}</div>
      </div>`;
}

export function updateEnrichLiveSection(tasks) {
  const el = document.getElementById('enrich-live-section');
  if (!el) return;
  const active = tasks.filter(x => x.category && x.category.startsWith('enrich-') && (x.status === 'running' || x.status === 'pending'));
  el.hidden = !active.length;
  if (!active.length) { el.dataset.cats = ''; return; }
  const cats = active.map(x => x.category).join('\u0001');
  if (el.dataset.cats !== cats) {            // a category started or finished: one rebuild
    el.innerHTML = `<section class="section">
    <div class="section-head"><h3>${th('activity.enrich_running')} <span class="badge amber badge-sm">${th('activity.categories', {n: active.length})}</span></h3><span class="section-hint">${th('activity.enrich_parallel')}</span></div>
    <div class="section-body">${active.map(_enrichRowHtml).join('')}</div></section>`;
    el.dataset.cats = cats;
    return;
  }
  const rows = new Map([...el.querySelectorAll('[data-enrich-cat]')].map(r => [r.dataset.enrichCat, r]));
  const scratch = document.createElement('div');
  for (const x of active) {
    const row = rows.get(x.category);
    if (!row) continue;
    scratch.innerHTML = _enrichRowHtml(x);
    const fresh = scratch.firstElementChild;
    for (const sel of ['.js-count-line', '.js-current']) {
      const a = row.querySelector(sel), b = fresh.querySelector(sel);
      if (a.outerHTML !== b.outerHTML) a.replaceWith(b);
    }
    row.querySelector('.progress-fill').style.width = fresh.querySelector('.progress-fill').style.width;
  }
}

// The "reload page" link of the stream-error notice: an <a href="#"> has to
// swallow its default before reloading.
export function reloadPage(event) { event.preventDefault(); location.reload(); }
