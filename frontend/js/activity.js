// ── TASK MONITOR ──────────────────────────────────────────────────────────────
// Moved state.taskEventSource to state
// Moved state.taskStreamRetries to state
import { state } from './state.js';
import { CAT_LABELS, EL, EVENT, SVG_CHECK, SVG_WARN, SVG_X, _errHtml, _errMsg, _fmtAbs, _fmtRel, act, btnBusy, btnDone, emptyHtml, esc, escAttr, hydrateStyles, loc, pulseHeldBy, setBadge, setPulse, toast } from './ui.js';
import { _swrInvalidate, _swrRun, api, sessionEnded } from './api.js';

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
  if (el) el.innerHTML = `<p class="t-danger loading" role="status" aria-live="polite">
    ${SVG_WARN} Task stream disconnected (${esc(why)}) — retrying, or
    <a class="t-amber" href="#" ${act('reloadPage', EVENT)} aria-label="Reload page to reconnect">reload page</a>.
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
    if (el && /Connecting|disconnected/.test(el.textContent)) el.innerHTML = '';
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
const OUTCOME = {
  running:   {label: 'Running',   badge: 'amber'},
  queued:    {label: 'Queued',    badge: 'muted'},
  completed: {label: 'Completed', badge: 'success'},
  warning:   {label: 'Warnings',  badge: 'amber'},
  partial:   {label: 'Partial',   badge: 'amber'},
  failed:    {label: 'Failed',    badge: 'danger'},
  cancelled: {label: 'Cancelled', badge: 'muted'},
  skipped:   {label: 'Skipped',   badge: 'muted'},
};
const AREA_LABELS = {sync: 'Sync', enrichment: 'Enrichment', taste: 'Taste / vectors', maintenance: 'Maintenance', system: 'System'};
const FADE_MS = 2600;
const SVG_CLOCK = '<svg class="icon-inline" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15 14"/></svg>';

const _isAdmin = () => !!state.currentUser?.is_admin;
const _outcomeOf = t => t.outcome || ({running: 'running', pending: 'queued', done: 'completed', error: 'failed', skipped: 'cancelled'})[t.status] || 'completed';
const _isLive = t => LIVE.has(_outcomeOf(t));

function _store(key, val) {
  try { if (val === undefined) return localStorage.getItem(key); localStorage.setItem(key, val); } catch { /* private window */ }
  return null;
}

export function fmtTime(s) {
  s = Math.max(0, Math.round(Number(s) || 0));
  if (!s) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m ${s%60}s`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

function _num(n) { return (Number(n) || 0).toLocaleString(loc()); }

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
  const top = tasks.filter(t => !t.parent_id && (t.status === 'running' || t.status === 'pending'));
  setBadge('tasks-badge', top.length);
  setBadge('act-count-queue', top.length);
  const running = tasks.some(t => (t.status === 'running' || t.status === 'pending') && String(t.id || '').startsWith('del-analysis-'));
  setPulse('sb-deletions-pulse', 'stream', running);
  if (_delAnalysisSeen && !running && !pulseHeldBy('sb-deletions-pulse', 'local')
      && document.getElementById('deletions-view')?.classList.contains('active')) {
    toast('Deletion analysis finished in another tab.', 'info', {actions: [
      {label: 'Reload proposals', primary: true, onClick: () => import('./deletions.js').then(m => m.reloadDeletions())},
      {label: 'Later'},
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

function _metaLine(t) {
  const o = _outcomeOf(t);
  if (o !== 'running') return '';
  return [
    t.total > 0 ? `${_num(t.processed)} / ${_num(t.total)}` : '',
    _rate(t.rate),
    t.eta_s ? `<span class="t-amber">ETA ${fmtTime(t.eta_s)}</span>` : '',
  ].filter(Boolean).join(' · ');
}

// A slow job read "0.0/s": below one item per ten seconds the rate is per minute.
function _rate(r) {
  if (!(r > 0)) return '';
  return r >= 0.1 ? `${r.toFixed(1)}/s` : `${(r * 60).toFixed(1)}/min`;
}

// Sub-steps of a grouped run (the per-category taste vectors of one
// recompute) as one compact badge row instead of rows of their own.
function _subStepsHtml(t, kids) {
  const seen = new Set();
  const pills = kids.map(k => {
    const label = (k.tags && k.tags[0]) || k.name;
    seen.add(label);
    const o = _outcomeOf(k);
    const extra = o === 'running' && k.total > 0 ? ` ${k.progress}%` : '';
    return `<span class="act-sub s-${o}" title="${escAttr(`${k.name}: ${OUTCOME[o]?.label || o}`)}">${o === 'running' ? '<span class="act-spin sm" aria-hidden="true"></span>' : _icon(o)}${esc(CAT_LABELS[label] || label)}${extra}</span>`;
  });
  if (_isLive(t) && kids.length) {          // a grouped run: the steps still to come
    for (const tag of t.tags || []) {
      if (!seen.has(tag)) pills.push(`<span class="act-sub s-queued" title="Waiting">${SVG_CLOCK}${esc(CAT_LABELS[tag] || tag)}</span>`);
    }
  }
  return pills.join('');
}

function _stepLine(t) {
  const o = _outcomeOf(t);
  if (!LIVE.has(o)) return t.error || t.summary || '';
  if (o === 'queued') return 'Waiting for a free slot';
  return t.logs?.slice(-1)[0]?.msg || '';
}

function _queueRowHtml(t, kids) {
  const o = _outcomeOf(t);
  const live = LIVE.has(o);
  const meta = _metaLine(t);
  const step = _stepLine(t);
  const subs = _subStepsHtml(t, kids);
  const pct = live ? (t.progress || 0) : 100;
  const showBar = o === 'running' || (!live && o !== 'cancelled' && o !== 'skipped' && o !== 'failed');
  const pctLabel = o === 'running' && !t.indeterminate ? `<span class="t-amber b fs-12">${t.progress || 0}%</span>` : '';
  return `<div class="act-row act-${o}${live ? '' : ' act-fade'}" role="listitem" data-run-id="${escAttr(String(t.run_id || t.id))}" data-started="${escAttr(t.started_at || '')}">
      <div class="act-ico js-ico">${_icon(o)}</div>
      <div class="act-main">
        <div class="act-title js-title"><span class="b">${esc(t.name)}</span>${kids.length ? '' : _tagPills(t.tags)}${_outcomeBadge(o)}${pctLabel}</div>
        <div class="act-step ellipsis js-step"${step ? '' : ' hidden'} title="${escAttr(step)}">${esc(step)}</div>
        ${_barHtml(pct, !!t.indeterminate, !showBar)}
        <div class="act-meta js-meta"${meta ? '' : ' hidden'}>${meta}</div>
        <div class="act-subs js-subs"${subs ? '' : ' hidden'}>${subs}</div>
      </div>
      <div class="act-side">
        <span class="act-elapsed mono js-elapsed">${fmtTime(t.elapsed_s)}</span>
        <span class="js-actions"${live ? '' : ' hidden'}><button type="button" class="btn btn-secondary btn-sm" ${act('cancelTask', t.id, EL)} aria-label="${escAttr(`Cancel ${t.name}`)}">Cancel</button></span>
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
  const ids = new Set(tasks.map(t => t.id));
  const kidsOf = new Map();
  for (const t of tasks) {
    if (t.parent_id && ids.has(t.parent_id)) {
      if (!kidsOf.has(t.parent_id)) kidsOf.set(t.parent_id, []);
      kidsOf.get(t.parent_id).push(t);
    }
  }
  let finishedNow = false;
  const visible = [];
  for (const t of tasks) {
    if (t.parent_id && ids.has(t.parent_id)) continue;
    const key = String(t.run_id || t.id);
    if (_isLive(t)) { _finishedSeen.delete(key); visible.push(t); continue; }
    if (!_finishedSeen.has(key)) { _finishedSeen.set(key, now); finishedNow = true; }
    if (now - _finishedSeen.get(key) < FADE_MS) visible.push(t);
  }
  if (_finishedSeen.size > 200) _finishedSeen.clear();
  if (finishedNow) _onRunFinished();
  // Re-render when the next fading row is due to leave.
  clearTimeout(_fadeTimer);
  if (visible.some(t => !_isLive(t))) _fadeTimer = setTimeout(() => renderTasks(_lastLive), 400);

  const liveCount = visible.filter(_isLive).length;
  const hint = document.getElementById('act-queue-hint');
  if (hint) hint.textContent = liveCount ? `${liveCount} active` : 'idle';

  if (!visible.length) {
    if (!el.querySelector(':scope > .empty')) {
      el.innerHTML = emptyHtml('No active tasks running — all queues idle.', null, null, {good: true});
    }
    _syncElapsedTicker();
    return;
  }
  let list = el.querySelector(':scope > .act-list');
  if (!list) {
    el.innerHTML = '<div class="act-list" role="list" aria-label="Active tasks"></div>';
    list = el.firstElementChild;
  }
  const rows = new Map([...list.children].map(r => [r.dataset.runId, r]));
  const scratch = document.createElement('div');
  let prev = null;
  for (const t of visible) {
    const key = String(t.run_id || t.id);
    scratch.innerHTML = _queueRowHtml(t, kidsOf.get(t.id) || []);
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
  btnBusy(btn, 'Cancelling…');
  try {
    await api(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, 'POST');
    _swrInvalidate('tasks'); // next visit reflects the cancellation instead of a stale pre-cancel snapshot
    toast('Cancel requested — the task stops at its next checkpoint', 'info');
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
    done: ['success', 'OK'], partial: ['amber', 'Partial — continues next tick'],
    error: ['danger', 'Failed'], waiting: ['muted', 'Waiting'], skipped: ['muted', 'Skipped'],
  };
  const [cls, label] = map[j.last_result] || ['muted', j.last_result];
  const summary = j.last_summary && !(j.last_result === 'partial' && /^Partial/.test(j.last_summary)) ? j.last_summary : '';
  return `<span class="badge ${cls} badge-sm">${esc(label)}</span>${summary ? ` <span class="t3 fs-12 act-sum" title="${escAttr(summary)}">${esc(summary)}</span>` : ''}`;
}

function _nextCell(j) {
  if (j.state === 'running') return '<span class="badge amber badge-sm"><span class="act-spin sm" aria-hidden="true"></span>Running</span>';
  if (j.state === 'queued') return `<span class="badge muted badge-sm">${SVG_CLOCK} Queued</span>`;
  if (j.blocked) return `<span class="t-amber fs-12">${esc(j.blocked)}</span>`;
  if (j.due) {
    const when = j.next_run ? _fmtRel(j.next_run) : '';
    return `<span class="t-amber" title="${escAttr(j.next_run ? _fmtAbs(j.next_run) : '')}">due now</span>${when && when !== 'now' ? ` <span class="t3 fs-12">· next tick ${esc(when)}</span>` : ''}`;
  }
  if (!j.next_run) return `<span class="t3">${esc(j.next_note || '—')}</span>`;
  return `<span title="${escAttr(_fmtAbs(j.next_run))}">${esc(_fmtRel(j.next_run) || 'now')}</span>`;
}

function _schedRowHtml(j) {
  const last = j.last_run
    ? `<span title="${escAttr(_fmtAbs(j.last_run))}">${esc(_fmtRel(j.last_run) || 'now')}</span>`
    : '<span class="t3">never</span>';
  const dur = j.last_duration_s != null && j.last_duration_s > 0 ? fmtTime(j.last_duration_s) : (j.last_duration_s === 0 ? '<1s' : '—');
  const busy = j.state !== 'idle';
  return `<tr data-job="${escAttr(j.job_id)}">
      <td class="cell-primary"><span class="b">${esc(j.name)}</span>${j.uses_model ? ' <span class="badge theme badge-sm" title="Runs a model — waits while a game holds the GPU">model</span>' : ''}</td>
      <td class="t2 nowrap">${esc(j.cadence)}</td>
      <td class="nowrap">${last}</td>
      <td class="t2 nowrap">${dur}</td>
      <td class="act-result">${_resultCell(j)}</td>
      <td class="nowrap">${_nextCell(j)}</td>
      <td class="t-right"><button type="button" class="btn btn-secondary btn-sm" ${act('runScheduledJob', j.job_id, EL)}${j.can_run ? '' : ' disabled'} aria-label="${escAttr(`Run ${j.name} now`)}" title="${escAttr(busy ? 'Already in the queue' : j.can_run ? 'Run now, outside its schedule' : 'Not available')}">${busy ? 'In queue' : 'Run now'}</button></td>
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
  if (!jobs.length) { el.innerHTML = emptyHtml('No background jobs are registered — the scheduler is not running.'); return; }
  const c = d.custodian || {};
  const busy = Object.entries(c.queues || {}).filter(([, q]) => q.busy).map(([n, q]) => `${n} queue on ${q.current || '…'}`);
  const head = `<p class="t3 fs-12 mb-8">Maintenance work runs on the data custodian's tick (${esc(c.tick || '')}${c.next_tick ? `, next <span title="${escAttr(_fmtAbs(c.next_tick))}">${esc(_fmtRel(c.next_tick))}</span>` : ''})${busy.length ? ` — ${esc(busy.join(', '))}` : ''}.</p>`;
  el.innerHTML = head + d.groups.map(g => `<section class="section">
      <div class="section-head"><h3>${esc(g.label)} <span class="badge muted badge-sm">${g.jobs.length}</span></h3></div>
      <div class="section-body"><div class="tbl-wrap"><table class="tbl tbl-cards act-sched"><thead><tr><th>Task</th><th>Interval</th><th>Last execution</th><th>Last duration</th><th>Last result</th><th>Next execution</th><th class="t-right"><span class="sr-only">Actions</span></th></tr></thead>
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
  btnBusy(btn, 'Starting…');
  try {
    const r = await api(`/api/tasks/scheduled/${encodeURIComponent(jobId)}/run`, 'POST');
    toast(r.state === 'queued' ? 'Queued — it runs right after the task its queue is on' : 'Started — follow it in the Queue', 'info');
    btnDone(btn, 'In queue', {keepDisabled: true});
  } catch (e) {
    toast(e.status === 409 ? 'Already running or queued' : _errMsg(e), e.status === 409 ? 'info' : 'danger');
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
    const tip = `${s.name}: ${OUTCOME[o]?.label || o}${s.elapsed_s ? ` · ${fmtTime(s.elapsed_s)}` : ''}${s.summary ? ` — ${s.summary}` : ''}`;
    return `<span class="act-sub s-${o}" title="${escAttr(tip)}">${_icon(o)}${esc(CAT_LABELS[label] || label)}</span>`;
  }).join('')}</div>`;
}

function _historyRowHtml(r) {
  const o = r.outcome || 'completed';
  const when = r.finished_at || r.started_at;
  const count = r.total > 0 && r.processed < r.total ? `${_num(r.processed)} of ${_num(r.total)} items`
    : r.total > 0 ? `${_num(r.total)} items` : '';
  const summary = r.error || r.summary || '';
  const logs = (r.logs || []).filter(l => l.msg && l.msg !== summary);
  const details = logs.length ? `<details class="act-log mt-4"><summary class="t3 fs-11">Log</summary><div class="mono fs-11 t3">${logs.map(l => `<div class="${l.level === 'error' ? 't-danger' : l.level === 'warn' ? 't-amber' : ''}">${esc(l.msg)}</div>`).join('')}</div></details>` : '';
  return `<tr class="${r.noop ? 'act-noop' : ''}">
      <td class="cell-primary"><div class="act-title"><span class="act-hico">${_icon(o)}</span><span class="b">${esc(r.name)}</span>${r.steps?.length ? '' : _tagPills(r.tags)}</div>${summary ? `<div class="fs-12 t2 act-sum" title="${escAttr(summary)}">${esc(summary)}</div>` : ''}${_stepsHtml(r)}${details}</td>
      <td class="t2 nowrap">${esc(AREA_LABELS[r.area] || r.area || '')}</td>
      <td class="nowrap">${when ? `<span title="${escAttr(_fmtAbs(when))}">${esc(_fmtRel(when) || 'now')}</span>` : '—'}</td>
      <td class="t2 nowrap t-right">${r.elapsed_s >= 1 ? fmtTime(r.elapsed_s) : '<1s'}</td>
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
    ? `${shown.length} of ${runs.length} runs${hidden ? ` · ${hidden} empty hidden` : ''}` : '';
  if (!runs.length) { el.innerHTML = emptyHtml('No finished runs yet — syncs, enrichment and maintenance land here once they end.'); return; }
  if (!shown.length) {
    el.innerHTML = emptyHtml(hidden ? `Only empty runs match — ${hidden} hidden.` : 'No runs match these filters.',
      hidden ? 'Show empty runs' : null, hidden ? act('showNoopRuns') : null);
    return;
  }
  el.innerHTML = `<div class="tbl-wrap"><table class="tbl tbl-cards act-hist"><thead><tr><th>Task</th><th>Area</th><th>Finished</th><th class="t-right">Duration</th><th>Result</th></tr></thead>
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

function _enrichRowHtml(t) {
  const cat = t.category.replace('enrich-', '');
  const pct = t.progress || 0;
  const eta = t.eta_s ? ` · ETA ${fmtTime(Math.round(t.eta_s))}` : '';
  const rate = t.rate > 0 ? ` · ${t.rate.toFixed(1)}/s` : '';
  const current = t.logs && t.logs.length ? t.logs[t.logs.length - 1].msg : '';
  return `<div class="mb-8" data-enrich-cat="${escAttr(t.category)}">
        <div class="row fs-12 js-count-line"><span class="b">${esc(CAT_LABELS[cat] || cat)}</span><span class="t3 row-end">${(t.processed || 0).toLocaleString(loc())} / ${(t.total || 0).toLocaleString(loc())}${rate}${eta}</span></div>
        <div class="mt-4 progress-bar"><div class="progress-fill" data-style="width:${pct}%"></div></div>
        <div class="ellipsis fs-10 t3 mono mt-4 js-current"${current ? '' : ' hidden'} title="${escAttr(current)}">${esc(current)}</div>
      </div>`;
}

export function updateEnrichLiveSection(tasks) {
  const el = document.getElementById('enrich-live-section');
  if (!el) return;
  const active = tasks.filter(t => t.category && t.category.startsWith('enrich-') && (t.status === 'running' || t.status === 'pending'));
  el.hidden = !active.length;
  if (!active.length) { el.dataset.cats = ''; return; }
  const cats = active.map(t => t.category).join('\u0001');
  if (el.dataset.cats !== cats) {            // a category started or finished: one rebuild
    el.innerHTML = `<section class="section">
    <div class="section-head"><h3>Enrichment running <span class="badge amber badge-sm">${active.length} categor${active.length === 1 ? 'y' : 'ies'}</span></h3><span class="section-hint">in parallel, one slot each</span></div>
    <div class="section-body">${active.map(_enrichRowHtml).join('')}</div></section>`;
    el.dataset.cats = cats;
    return;
  }
  const rows = new Map([...el.querySelectorAll('[data-enrich-cat]')].map(r => [r.dataset.enrichCat, r]));
  const scratch = document.createElement('div');
  for (const t of active) {
    const row = rows.get(t.category);
    if (!row) continue;
    scratch.innerHTML = _enrichRowHtml(t);
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
