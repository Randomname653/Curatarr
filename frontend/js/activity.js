// ── TASK MONITOR ──────────────────────────────────────────────────────────────
// Moved state.taskEventSource to state
// Moved state.taskStreamRetries to state
import { state } from './state.js';
import { CAT_LABELS, SVG_WARN, _errHtml, _errMsg, _fmtAbs, _fmtRel, btnBusy, btnDone, emptyHtml, esc, escAttr, setBadge, toast } from './ui.js';
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
        <a href="#" onclick="location.reload();return false;" aria-label="Reload page to reconnect" style="color:var(--amber)">reload page</a>
        to reconnect.
      </p>`;
      state.taskStreamRetries = 0; // reset so reload works
      setTimeout(startTaskStream, 30000); // retry after 30s
    } else {
      setTimeout(startTaskStream, 3000 * state.taskStreamRetries);
    }
  };
}

export function updateTaskBadge(tasks) {
  setBadge('tasks-badge', tasks.filter(t => t.status === 'running' || t.status === 'pending').length);
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

export function renderTasks(tasks) {
  const el = document.getElementById('tasks-list');
  if (!el) return;
  if (!tasks?.length) {
    el.innerHTML = emptyHtml('No tasks yet — syncs, enrichment runs and imports appear here while they run.');
    return;
  }
  // Row anatomy: name + status badge (+ percent or spinner) with Cancel at
  // the row end while it can still be cancelled; the numbers on one meta
  // line; progress and the last log line on the row itself.
  el.innerHTML = tasks.map(t => {
    const label = STATUS_LABELS[t.status] || t.status;
    const live = t.status === 'running' || t.status === 'pending';
    const meta = [
      t.elapsed_s > 0 ? fmtTime(Math.round(t.elapsed_s)) : '',
      t.processed && t.total ? `${t.processed.toLocaleString()} / ${t.total.toLocaleString()}` : '',
      t.rate > 0 ? `${t.rate.toFixed(1)}/s` : '',
      t.eta_s ? `<span class="t-amber">ETA ${fmtTime(t.eta_s)}</span>` : '',
    ].filter(Boolean).join(' · ');
    const recentLog = t.logs?.slice(-1)[0]?.msg || '';
    return `<div class="panel-item${t.status === 'running' ? ' live' : ''}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(t.name)}<span class="badge ${STATUS_BADGE[t.status] || 'muted'} badge-sm">${esc(label)}</span>
          ${t.status === 'running' ? (t.total > 0 ? `<span class="t-amber b">${t.progress}%</span>` : '<span class="spinner" title="No item count for this job — watch the log line and the elapsed time"></span>') : ''}</div>
        ${live ? `<div class="panel-actions"><button type="button" class="btn btn-danger btn-sm" onclick="cancelTask('${esc(t.id)}',this)">Cancel</button></div>` : ''}
      </div>
      ${meta ? `<div class="panel-item-meta mt-4">${meta}</div>` : ''}
      ${t.status === 'error' && t.error ? `<div class="panel-item-sub t-danger">${esc(t.error)}</div>` : ''}
      ${t.status === 'running' && t.total > 0 ? `<div class="progress-bar"><div class="progress-fill" style="width:${t.progress}%"></div></div>` : ''}
      ${recentLog ? `<div class="panel-item-foot fs-11 t3 mono">${esc(recentLog)}</div>` : ''}
    </div>`;
  }).join('');
}

export function updateEnrichLiveSection(tasks) {
  const el = document.getElementById('enrich-live-section');
  if (!el) return;
  const active = tasks.filter(t => t.category && t.category.startsWith('enrich-') && (t.status === 'running' || t.status === 'pending'));
  el.hidden = !active.length;
  if (!active.length) return;
  el.innerHTML = `<section class="section">
    <div class="section-head"><h3>Enrichment running <span class="badge amber badge-sm">${active.length} categor${active.length === 1 ? 'y' : 'ies'}</span></h3><span class="section-hint">in parallel, one slot each</span></div>
    <div class="section-body">${active.map(t => {
      const cat = t.category.replace('enrich-', '');
      const pct = t.progress || 0;
      const eta = t.eta_s ? ` · ETA ${fmtTime(Math.round(t.eta_s))}` : '';
      const rate = t.rate > 0 ? ` · ${t.rate.toFixed(1)}/s` : '';
      const current = t.logs && t.logs.length ? t.logs[t.logs.length - 1].msg : '';
      return `<div class="mb-8">
        <div class="row fs-12"><span class="b">${esc(CAT_LABELS[cat] || cat)}</span><span class="t3 row-end">${(t.processed || 0).toLocaleString()} / ${(t.total || 0).toLocaleString()}${rate}${eta}</span></div>
        <div class="progress-bar" style="margin-top:4px"><div class="progress-fill" style="width:${pct}%"></div></div>
        ${current ? `<div class="fs-10 t3 mono mt-4" style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escAttr(current)}">${esc(current)}</div>` : ''}
      </div>`;
    }).join('')}</div></section>`;
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
    el.innerHTML = _errHtml(e, 'loadTaskHistory()');
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
