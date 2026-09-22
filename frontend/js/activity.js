// ── TASK MONITOR ──────────────────────────────────────────────────────────────
// Moved state.taskEventSource to state
// Moved state.taskStreamRetries to state
import { state } from './state.js';
import { CAT_LABELS, EL, EVENT, SVG_WARN, _errHtml, _errMsg, _fmtAbs, _fmtRel, act, btnBusy, btnDone, emptyHtml, esc, escAttr, pulseHeldBy, setBadge, setPulse, toast } from './ui.js';
import { _swrInvalidate, _swrRun, api } from './api.js';

export async function startTaskStream() {
  if (state.taskEventSource) return;
  const tok = state.token || localStorage.getItem('curatarr_token');
  if (!tok) { setTimeout(startTaskStream, 2000); return; }

  // Fetch a short-lived ticket so the JWT never appears in the URL
  let ticket;
  try {
    const r = await fetch('/api/tasks/ticket', { headers: { 'Authorization': `Bearer ${tok}` } });
    if (!r.ok) { setTimeout(startTaskStream, 3000); return; }
    ticket = (await r.json()).ticket;
  } catch { setTimeout(startTaskStream, 3000); return; }

  state.taskEventSource = new EventSource(`/api/tasks/stream?ticket=${encodeURIComponent(ticket)}`);

  state.taskEventSource.onopen = () => {
    state.taskStreamRetries = 0;
    const el = document.getElementById('tasks-list');
    if (el?.textContent.includes('Connecting')) el.innerHTML = '';
  };

  state.taskEventSource.onmessage = (e) => {
    try {
      const data = JSON.parse(e.data);
      // Handle auth failure — clear state.token and reload
      if (data.error === 'auth_failed') {
        state.taskEventSource?.close();
        state.taskEventSource = null;
        localStorage.removeItem('curatarr_token');
        state.token = '';
        location.reload();
        return;
      }
      const tasks = Array.isArray(data) ? data : [];
      renderTasks(tasks);
      updateTaskBadge(tasks);
      updateEnrichLiveSection(tasks);
    } catch {}
  };

  state.taskEventSource.onerror = (evt) => {
    state.taskEventSource?.close();
    state.taskEventSource = null;
    state.taskStreamRetries++;

    // After 3 retries, likely an auth issue — show message and stop retrying
    if (state.taskStreamRetries >= 3) {
      const el = document.getElementById('tasks-list');
      if (el) el.innerHTML = `<p class="loading" role="status" aria-live="polite" style="color:var(--danger)">
        ${SVG_WARN} Task stream disconnected —
        <a href="#" ${act('reloadPage', EVENT)} aria-label="Reload page to reconnect" style="color:var(--amber)">reload page</a>
        to reconnect.
      </p>`;
      state.taskStreamRetries = 0; // reset so reload works
      setTimeout(startTaskStream, 30000); // retry after 30s
    } else {
      setTimeout(startTaskStream, 3000 * state.taskStreamRetries);
    }
  };
}

// The Deletions entry pulses while a deletion analysis runs ANYWHERE —
// the task id is del-analysis-<user>, whichever tab pressed Analyse. When
// it ends and this tab did not start it, the Deletions view still shows
// the old proposals: offer the reload rather than replacing a list the
// user may be working in.
let _delAnalysisSeen = false;
export function updateTaskBadge(tasks) {
  setBadge('tasks-badge', tasks.filter(t => t.status === 'running' || t.status === 'pending').length);
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

const STATUS_BADGE = { running: 'amber', done: 'success', error: 'danger', pending: 'muted', skipped: 'muted' };
const STATUS_LABELS = {
  running: 'Running', done: 'Done',
  error: 'Error', pending: 'Queued', skipped: 'Cancelled'
};

export function fmtTime(s) {
  if (!s) return '';
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s/60)}m ${s%60}s`;
  return `${Math.floor(s/3600)}h ${Math.floor((s%3600)/60)}m`;
}

// Rows are patched in place while the task set is unchanged, and renders are
// coalesced to one per animation frame. Rebuilding the list on every progress
// event re-ran the card entrance animation on every row several times a
// second: the taste-vector recompute strobed the page (2026-09-13).
let _tasksPending = null;
let _tasksRaf = 0;
export function renderTasks(tasks) {
  _tasksPending = tasks;
  if (_tasksRaf) return;
  _tasksRaf = requestAnimationFrame(() => { _tasksRaf = 0; _renderTasksNow(_tasksPending || []); });
}

// Every part of a row is always present (hidden when empty) so a later tick
// can patch it instead of rebuilding the row.
function _taskRowHtml(t) {
  const label = STATUS_LABELS[t.status] || t.status;
  const live = t.status === 'running' || t.status === 'pending';
  const meta = [
    t.elapsed_s > 0 ? fmtTime(Math.round(t.elapsed_s)) : '',
    t.processed && t.total ? `${t.processed.toLocaleString()} / ${t.total.toLocaleString()}` : '',
    t.rate > 0 ? `${t.rate.toFixed(1)}/s` : '',
    t.eta_s ? `<span class="t-amber">ETA ${fmtTime(t.eta_s)}</span>` : '',
  ].filter(Boolean).join(' · ');
  const recentLog = t.logs?.slice(-1)[0]?.msg || '';
  const err = t.status === 'error' && t.error ? esc(t.error) : '';
  const bar = t.status === 'running' && t.total > 0;
  return `<div class="panel-item${t.status === 'running' ? ' live' : ''}" data-task-id="${escAttr(String(t.id))}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(t.name)}<span class="js-status"><span class="badge ${STATUS_BADGE[t.status] || 'muted'} badge-sm">${esc(label)}</span>
          ${t.status === 'running' ? (t.total > 0 ? `<span class="t-amber b">${t.progress}%</span>` : '<span class="spinner" title="No item count for this job — watch the log line and the elapsed time"></span>') : ''}</span></div>
        <div class="panel-actions js-actions"${live ? '' : ' hidden'}><button type="button" class="btn btn-danger btn-sm" ${act('cancelTask', t.id, EL)}>Cancel</button></div>
      </div>
      <div class="panel-item-meta mt-4 js-meta"${meta ? '' : ' hidden'}>${meta}</div>
      <div class="panel-item-sub t-danger js-error"${err ? '' : ' hidden'}>${err}</div>
      <div class="progress-bar js-progress"${bar ? '' : ' hidden'}><div class="progress-fill" style="width:${bar ? t.progress : 0}%"></div></div>
      <div class="panel-item-foot fs-11 t3 mono js-foot"${recentLog ? '' : ' hidden'}>${esc(recentLog)}</div>
    </div>`;
}

function _renderTasksNow(tasks) {
  const el = document.getElementById('tasks-list');
  if (!el) return;
  if (!tasks?.length) {
    el.dataset.taskIds = '';
    el.innerHTML = emptyHtml('No tasks yet — syncs, enrichment runs and imports appear here while they run.');
    return;
  }
  const ids = tasks.map(t => String(t.id)).join('\u0001');
  if (el.dataset.taskIds !== ids) {          // membership or order changed: one rebuild
    el.innerHTML = tasks.map(_taskRowHtml).join('');
    el.dataset.taskIds = ids;
    return;
  }
  const rows = new Map([...el.querySelectorAll('[data-task-id]')].map(r => [r.dataset.taskId, r]));
  const scratch = document.createElement('div');
  for (const t of tasks) {
    const row = rows.get(String(t.id));
    if (!row) continue;
    scratch.innerHTML = _taskRowHtml(t);
    const fresh = scratch.firstElementChild;
    if (row.className !== fresh.className) row.className = fresh.className;
    for (const sel of ['.js-status', '.js-meta', '.js-error', '.js-foot']) {
      const a = row.querySelector(sel), b = fresh.querySelector(sel);
      if (a.innerHTML !== b.innerHTML) a.innerHTML = b.innerHTML;
      a.hidden = b.hidden;
    }
    const pa = row.querySelector('.js-progress'), pb = fresh.querySelector('.js-progress');
    pa.hidden = pb.hidden;
    pa.firstElementChild.style.width = pb.firstElementChild.style.width;
    // The Cancel button is never replaced: a "Cancelling…" busy state survives the next tick.
    row.querySelector('.js-actions').hidden = fresh.querySelector('.js-actions').hidden;
  }
}

function _enrichRowHtml(t) {
  const cat = t.category.replace('enrich-', '');
  const pct = t.progress || 0;
  const eta = t.eta_s ? ` · ETA ${fmtTime(Math.round(t.eta_s))}` : '';
  const rate = t.rate > 0 ? ` · ${t.rate.toFixed(1)}/s` : '';
  const current = t.logs && t.logs.length ? t.logs[t.logs.length - 1].msg : '';
  return `<div class="mb-8" data-enrich-cat="${escAttr(t.category)}">
        <div class="row fs-12 js-count-line"><span class="b">${esc(CAT_LABELS[cat] || cat)}</span><span class="t3 row-end">${(t.processed || 0).toLocaleString()} / ${(t.total || 0).toLocaleString()}${rate}${eta}</span></div>
        <div class="progress-bar" style="margin-top:4px"><div class="progress-fill" style="width:${pct}%"></div></div>
        <div class="fs-10 t3 mono mt-4 js-current" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"${current ? '' : ' hidden'} title="${escAttr(current)}">${esc(current)}</div>
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

export function _renderTaskHistory(r) {
  const el = document.getElementById('tasks-history');
  if (!el) return;
  const runs = r.last_runs || {};
  const entries = Object.entries(runs);
  if (!entries.length) {
    el.innerHTML = emptyHtml('No completed tasks yet this session.');
    return;
  }
  entries.sort((a,b) => (b[1].at||'').localeCompare(a[1].at||''));
  // The task table: relative time in the cell, the absolute one in its tooltip.
  el.innerHTML = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Task</th><th>Status</th><th>Last run</th><th class="t-right">Duration</th></tr></thead><tbody>
    ${entries.map(([cat, run]) => `<tr>
      <td><div class="b">${esc(run.name || cat)}</div>${run.message ? `<div class="fs-11 t3" style="max-width:520px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escAttr(run.message)}">${esc(run.message)}</div>` : ''}</td>
      <td><span class="badge ${STATUS_BADGE[run.status] || 'muted'} badge-sm">${esc(run.status || '')}</span></td>
      <td class="t3" title="${escAttr(_fmtAbs(run.at))}">${run.at ? _fmtRel(run.at) : '—'}</td>
      <td class="t-right t3">${run.elapsed_s > 0 ? fmtTime(Math.round(run.elapsed_s)) : ''}</td>
    </tr>`).join('')}
  </tbody></table></div>`;
}

export async function loadTaskHistory() {
  const el = document.getElementById('tasks-history');
  if (!el) return;
  try {
    await _swrRun('tasks', () => api('/api/tasks/history'), _renderTaskHistory);
  } catch(e) {
    el.innerHTML = _errHtml(e, act('loadTaskHistory'));
  }
}

export async function cancelTask(taskId, btn) {
  btnBusy(btn, 'Cancelling…');
  try {
    await api(`/api/tasks/${taskId}/cancel`, 'POST');
    _swrInvalidate('tasks'); // next visit reflects the cancellation instead of a stale pre-cancel snapshot
    toast('Cancel requested — the task stops at its next checkpoint', 'info');
  } catch (e) {
    toast(_errMsg(e), 'danger');
    btnDone(btn);
  }
}

// The "reload page" link of the stream-error notice: an <a href="#"> has to
// swallow its default before reloading.
export function reloadPage(event) { event.preventDefault(); location.reload(); }
