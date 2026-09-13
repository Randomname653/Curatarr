// ── HISTORY & TASTE ───────────────────────────────────────────────────────────
// Sync and recompute are called from the History toolbar (status goes into
// the stats block) and from Settings → Maintenance (status goes into that
// section's own line, statusId) — never into another view's element.
import { CAT_LABELS, _errHtml, _errMsg, esc, setStatus, toast } from './ui.js';
import { _swrInvalidate, _swrRun, api } from './api.js';
import { loadEnrichStatus } from './kb.js';
export async function recomputeTaste(statusId = null) {
  const status = statusId ? document.getElementById(statusId) : null;
  const el = document.getElementById('history-stats');
  const orig = el ? el.innerHTML : '';
  if (status) setStatus(status, 'Recomputing taste vectors…', 'busy');
  else if (el) el.innerHTML += '<p class="fs-12 t-amber mt-8">Recomputing taste vectors…</p>';
  try {
    await api('/api/history/recompute-taste','POST');
    if (status) setStatus(status, 'Taste vectors are being recomputed — the numbers refresh in a few seconds.', 'ok');
    toast('Taste vector recomputation started', 'success');
    _swrInvalidate('history-stats'); // so the delayed reload below fetches fresh instead of flashing the pre-recompute numbers
    setTimeout(() => {
      loadHistoryStatus();
      // Also refresh enrichment status if on that tab
      if (document.getElementById('enrich-view')?.classList.contains('active')) loadEnrichStatus();
    }, 8000);
  } catch(e) {
    if (status) setStatus(status, _errMsg(e), 'err');
    else if (el) el.innerHTML = orig + _errHtml(e);
  }
}

export async function syncHistory(force = false, statusId = null) {
  const status = statusId ? document.getElementById(statusId) : null;
  const el = document.getElementById('history-stats');
  const label = force ? 'Force syncing…' : 'Syncing Plex history…';
  if (status) setStatus(status, label, 'busy');
  else if (el) el.innerHTML = `<p class="loading" role="status" aria-live="polite">${label}</p>`;
  try {
    const r = await api('/api/history/sync?force=' + (force ? 'true' : 'false'), 'POST');
    if (status) setStatus(status, r.message, 'ok');
    else if (el) el.innerHTML = `<div class="fs-13 t-success">${esc(r.message)}</div>`;
    toast(r.message, 'success');
    _swrInvalidate('history-stats'); // so the delayed reload below fetches fresh instead of flashing the pre-sync numbers
    setTimeout(loadHistoryStatus, 5000);
  } catch (e) {
    if (status) setStatus(status, _errMsg(e), 'err');
    else if (el) el.innerHTML = _errHtml(e, `syncHistory(${force})`);
  }
}


export function _renderHistoryStats(s) {
  const tv = s.taste_vector;
  const coverageOk = tv.covers_all || !tv.computed;
  document.getElementById('history-stats').innerHTML=`
    <div class="stat-row">
      <div class="stat-box"><div class="num">${s.watch_history_entries.toLocaleString()}</div><div class="lbl">Entries synced</div></div>
      <div class="stat-box">
        <div class="num" style="color:${coverageOk?'var(--amber)':'var(--danger)'}">${tv.watch_count?.toLocaleString()||0}</div>
        <div class="lbl">In taste profile ${coverageOk?'':'<span style="color:var(--amber)">— outdated</span>'}</div>
      </div>
      <div class="stat-box"><div class="num" style="font-size:16px">${tv.computed?'Ready':'Pending'}</div><div class="lbl">Profile status</div></div>
    </div>
    ${tv.note?`<div style="font-size:12px;color:var(--danger);margin-bottom:12px;padding:8px 12px;background:var(--danger-dim);border-radius:var(--radius)">${esc(tv.note)}</div>`:''}`;
}

// Only the stats tile above goes through the SWR cache -- the taste-tabs /
// recent-history portion below already has its own tab-switching mechanism
// (showTasteTab) that independently re-fetches both endpoints on click, so
// folding it into the same cache key would make that click bypass a
// generic cache built around the wrong assumption (one payload per key).
// It stays exactly as it was: re-runs on every visit to this view.
export async function loadHistoryStatus() {
  try {
    const s = await _swrRun('history-stats', () => api('/api/history/status'), _renderHistoryStats);
    const tv=s.taste_vector;
    const byType=tv.by_type||{};
    const types=Object.keys(byType);

    if (types.length) {
      document.getElementById('taste-tabs').innerHTML = types.map((t,i)=>
        `<button class="cat-tab ${i===0?'active':''}" onclick="showTasteTab('${t}',this)">${CAT_LABELS[t]||t}</button>`
      ).join('');
      showTasteTabData(byType, tv.summary||'', types[0]);

      const h=await api(`/api/history/recent?limit=100&category=${types[0]}`);
      renderRecent(h.entries, types[0]);
    }
  } catch(e){ document.getElementById('history-stats').innerHTML=_errHtml(e, 'loadHistoryStatus()'); }
}

export function showTasteTab(type,btn) {
  document.querySelectorAll('#taste-tabs .cat-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  api('/api/history/status').then(s=>showTasteTabData(s.taste_vector.by_type||{},s.taste_vector.summary||'',type)).catch(()=>{});
  api(`/api/history/recent?limit=100&category=${type}`).then(h=>renderRecent(h.entries,type)).catch(()=>{});
}

export function showTasteTabData(byType, summary, type) {
  const d=byType[type];
  if(!d){document.getElementById('taste-content').innerHTML='<p class="loading" role="status" aria-live="polite">No data for this category yet.</p>';return;}
  const typeUpper=type.toUpperCase();
  const match=summary.match(new RegExp(`\\[${typeUpper}\\]([^\\[]*)`));
  const typeSummary=match?match[1].trim():'';
  const cb = d.completion_breakdown || {};
  const compRow = cb.total
    ? `<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:12px">
        <span><strong style="color:var(--success)">${cb.completed||0}</strong> <span style="font-size:12px;color:var(--text2)">finished</span></span>
        ${cb.in_progress?`<span><strong style="color:var(--amber)">${cb.in_progress}</strong> <span style="font-size:12px;color:var(--text2)">in progress</span></span>`:''}
        ${cb.dropped?`<span><strong style="color:var(--danger)">${cb.dropped}</strong> <span style="font-size:12px;color:var(--text2)">dropped &lt;40%</span></span>`:''}
        ${cb.nearly_done?`<span><strong style="color:var(--info)">${cb.nearly_done}</strong> <span style="font-size:12px;color:var(--text2)">&gt;80% done</span></span>`:''}
      </div>`
    : `<div style="margin-bottom:12px"><span style="font-size:22px;font-weight:700;color:var(--amber)">${d.watch_count?.toLocaleString()||0}</span><span style="font-size:12px;color:var(--text2);margin-left:6px">items</span></div>`;
  document.getElementById('taste-content').innerHTML=`
    <div class="card" style="margin-bottom:16px">
      ${compRow}
      ${d.top_genres?.length?`<div style="margin-bottom:8px"><span style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px">Top genres</span><div style="margin-top:6px;display:flex;gap:6px;flex-wrap:wrap">${d.top_genres.slice(0,8).map(g=>`<span class="badge amber">${esc(g)}</span>`).join('')}</div></div>`:''}
      ${d.top_titles?.length?`<div><span style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px">Most watched</span><div style="margin-top:4px;font-size:12px;color:var(--text2)">${d.top_titles.slice(0,8).map(esc).join(' · ')}</div></div>`:''}
      ${typeSummary?`<div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--border);font-size:12px;color:var(--text2);line-height:1.7">${esc(typeSummary)}</div>`:''}
    </div>`;
}

export function renderRecent(entries, category) {
  if(!entries?.length){document.getElementById('recent-history').innerHTML=`<p class="loading" role="status" aria-live="polite">No ${CAT_LABELS[category]||category} history yet.</p>`;return;}
  const label = CAT_LABELS[category]||category;
  document.getElementById('recent-history').innerHTML=`
    <div style="font-size:11px;color:var(--text3);text-transform:uppercase;letter-spacing:.8px;margin-bottom:8px">Recent ${label}</div>
    <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Title</th><th>Genre</th><th>Date</th></tr></thead><tbody>
    ${entries.slice(0,50).map(e=>{
      let title=e.title;
      if(e.media_type==='music'&&e.series_title) title=`${e.title} — ${e.series_title}`;
      else if((e.media_type==='anime'||e.media_type==='show')&&e.series_title) title=`${e.series_title}: ${e.title}`;
      return `<tr><td>${esc(title)}</td><td><span style="color:var(--text3);font-size:12px">${esc((e.genres||'').split(',')[0])}</span></td><td style="color:var(--text3);font-size:12px;white-space:nowrap">${(e.viewed_at||'').slice(0,10)}</td></tr>`;
    }).join('')}
    </tbody></table></div>`;
}
