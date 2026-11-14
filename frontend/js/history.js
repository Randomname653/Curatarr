// ── HISTORY & TASTE ───────────────────────────────────────────────────────────
// Sync and recompute are called from the History toolbar (status goes into
// the stats block) and from Settings → Maintenance (status goes into that
// section's own line, statusId) — never into another view's element.
import { CAT_LABELS, EL, _errHtml, _errMsg, act, countTo, emptyHtml, esc, loc, menuHtml, setStatus, skeletonHtml, toast, loadingHtml } from './ui.js';
import { _swrInvalidate, _swrRun, api } from './api.js';
import { loadEnrichStatus } from './kb.js';
import { serverLabel, state } from './state.js';
import { t, th, uiLang } from './i18n.js';
export async function recomputeTaste(statusId = null) {
  const status = statusId ? document.getElementById(statusId) : null;
  const el = document.getElementById('history-stats');
  const orig = el ? el.innerHTML : '';
  if (status) setStatus(status, t('history.recomputing'), 'busy');
  else if (el) el.innerHTML += `<p class="fs-12 t-amber mt-8">${th('history.recomputing')}</p>`;
  try {
    await api('/api/history/recompute-taste','POST');
    if (status) setStatus(status, t('history.updating_long'), 'ok');
    toast(t('history.updating'), 'success');
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
  const label = t('history.syncing', {server: serverLabel()});
  if (status) setStatus(status, label, 'busy');
  else if (el) el.innerHTML = loadingHtml(label);
  try {
    const r = await api('/api/history/sync?force=' + (force ? 'true' : 'false'), 'POST');
    if (status) setStatus(status, r.message, 'ok');
    else if (el) el.innerHTML = `<div class="fs-13 t-success">${esc(r.message)}</div>`;
    toast(r.message, 'success');
    _swrInvalidate('history-stats'); // so the delayed reload below fetches fresh instead of flashing the pre-sync numbers
    setTimeout(loadHistoryStatus, 5000);
  } catch (e) {
    if (status) setStatus(status, _errMsg(e), 'err');
    else if (el) el.innerHTML = _errHtml(e, act('syncHistory', force));
  }
}


export function _renderHistoryStats(s) {
  const tv = s.taste_vector;
  const coverageOk = tv.covers_all || !tv.computed;
  // The synced count counts up from what was on screen (0 the first time).
  const shown = document.querySelector('#history-stats [data-count]')?.textContent || '0';
  document.getElementById('history-stats').innerHTML=`
    <div class="stat-row">
      <div class="stat-box"><div class="num" data-count>${esc(shown)}</div><div class="lbl">${th('history.entries_synced')}</div></div>
      <div class="stat-box">
        <div class="num ${coverageOk ? 't-amber' : 't-danger'}">${tv.watch_count?.toLocaleString(loc())||0}</div>
        <div class="lbl">${th('history.in_profile')} ${coverageOk?'':`<span class="t-amber">${th('history.outdated')}</span>`}</div>
      </div>
      <div class="stat-box"><div class="fs-16 num">${th(tv.computed ? 'history.ready' : 'history.pending')}</div><div class="lbl">${th('history.profile_status')}</div></div>
    </div>
    ${tv.note?`<div class="error-box">${esc(tv.note)}</div>`:''}`;
  countTo(document.querySelector('#history-stats [data-count]'), s.watch_history_entries);
}

// Only the stats tile above goes through the SWR cache -- the taste-tabs /
// recent-history portion below already has its own tab-switching mechanism
// (showTasteTab) that independently re-fetches both endpoints on click, so
// folding it into the same cache key would make that click bypass a
// generic cache built around the wrong assumption (one payload per key).
// It stays exactly as it was: re-runs on every visit to this view.
// Which witnesses a Jellyfin install's history comes from (design §4.5):
// live sessions always, the Webhook plugin when it posted this week, the
// Playback Reporting backfill when one was found. Plex installs show nothing.
export async function loadHistorySources() {
  const el = document.getElementById('history-sources');
  if (!el || state.currentUser?.server !== 'jellyfin') return;
  try {
    const s = await api('/api/jellyfin/history-sources');
    const have = [t('history.src_live')];
    if (s.webhook) have.push(t('history.src_webhook'));
    if (s.playback_reporting) have.push(t('history.src_backfill'));
    el.textContent = t('history.sources', {list: have.join(' + ')})
      + (s.webhook ? '' : ` ${t('history.add_webhook')}`);
    el.hidden = false;
  } catch (e) { console.warn('[history] sources not read - the line stays hidden', e); }
}

// The owner's rarer actions behind one "More": the toolbar carried five
// buttons, two rows on a phone (UI audit 2026-11-14). Sync now stays visible.
function _renderHistoryMore() {
  const el = document.getElementById('history-more');
  if (!el || !state.currentUser?.is_admin) return;
  // Re-rendered only when its words change: the sync poll calls this every
  // 5 s and a rewrite closed an open menu.
  const sig = uiLang() + '|' + serverLabel();
  if (el.dataset.sig === sig) return;
  el.dataset.sig = sig;
  el.innerHTML = menuHtml([
    {label: t('history.force_sync'), action: act('syncHistory', true), title: t('history.force_title')},
    {label: t('history.recompute'), action: act('recomputeTaste')},
    {sep: true},
    {label: t('auth.libraries_btn', {server: serverLabel()}), action: act('showLibrariesForce')},
  ], t('common.more'), {size: 'md', align: 'start'});
}

export async function loadHistoryStatus() {
  _renderHistoryMore();
  const stats = document.getElementById('history-stats');
  if (stats && !stats.innerHTML) stats.innerHTML = skeletonHtml(t('common.loading'), {kind: 'rows', n: 3});
  loadHistorySources();
  try {
    const s = await _swrRun('history-stats', () => api('/api/history/status'), _renderHistoryStats);
    const tv=s.taste_vector;
    const byType=tv.by_type||{};
    const types=Object.keys(byType);

    if (types.length) {
      document.getElementById('taste-tabs').innerHTML = types.map((ty,i)=>
        `<button class="cat-tab ${i===0?'active':''}" ${act('showTasteTab', ty, EL)}>${esc(CAT_LABELS[ty]||ty)}</button>`
      ).join('');
      showTasteTabData(byType, tv.summary||'', types[0]);

      // Its own error slot: a failed recent list used to replace the stats
      // tile above (which had loaded fine) with "Couldn't load".
      try {
        const h=await api(`/api/history/recent?limit=100&category=${types[0]}`);
        renderRecent(h.entries, types[0]);
      } catch (e) { _tasteErr('recent-history', e, act('showTasteTab', types[0], null)); }
    } else {
      // A page with the stats tile and nothing under it was a dead end. No
      // plays yet: Sync now fills it. Plays but no taste profile yet (a
      // fresh install learns its library first): Recompute builds it.
      document.getElementById('taste-tabs').innerHTML = '';
      document.getElementById('taste-content').innerHTML = (s.watch_history_entries || 0) === 0
        ? emptyHtml(th('history.empty', {server: serverLabel()}), t('history.sync_now'), act('syncHistory', false), {mark: true})
        : emptyHtml(th('history.no_taste'), t('history.recompute'), act('recomputeTaste'));
    }
  } catch(e){ document.getElementById('history-stats').innerHTML=_errHtml(e, act('loadHistoryStatus')); }
}

export function showTasteTab(type,btn) {
  document.querySelectorAll('#taste-tabs .cat-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  // A failed load used to leave the previous tab's numbers under the new
  // tab's heading. Each half says it failed, with a Retry of this tab.
  const retry = act('showTasteTab', type, null);
  api('/api/history/status').then(s=>showTasteTabData(s.taste_vector.by_type||{},s.taste_vector.summary||'',type))
    .catch(e => _tasteErr('taste-content', e, retry));
  api(`/api/history/recent?limit=100&category=${encodeURIComponent(type)}`).then(h=>renderRecent(h.entries,type))
    .catch(e => _tasteErr('recent-history', e, retry));
}
function _tasteErr(id, e, retry) {
  const el = document.getElementById(id);
  if (el) el.innerHTML = _errHtml(e, retry);
}

export function showTasteTabData(byType, summary, type) {
  const d=byType[type];
  if(!d){document.getElementById('taste-content').innerHTML=emptyHtml(th('history.no_data'));return;}
  const typeUpper=type.toUpperCase();
  const match=summary.match(new RegExp(`\\[${typeUpper}\\]([^\\[]*)`));
  const typeSummary=match?match[1].trim():'';
  const cb = d.completion_breakdown || {};
  const compRow = cb.total
    ? `<div class="flex gap-16 flex-wrap mb-12">
        <span><strong class="t-success">${cb.completed||0}</strong> <span class="fs-12 t2">${th('history.finished')}</span></span>
        ${cb.in_progress?`<span><strong class="t-amber">${cb.in_progress}</strong> <span class="fs-12 t2">${th('history.in_progress')}</span></span>`:''}
        ${cb.dropped?`<span><strong class="t-danger">${cb.dropped}</strong> <span class="fs-12 t2">${th('history.dropped')}</span></span>`:''}
        ${cb.nearly_done?`<span><strong class="t-info">${cb.nearly_done}</strong> <span class="fs-12 t2">${th('history.nearly_done')}</span></span>`:''}
      </div>`
    : `<div class="mb-12"><span class="fs-22 fw-700 t-amber">${d.watch_count?.toLocaleString(loc())||0}</span><span class="fs-12 t2 ml-6">${th('history.items')}</span></div>`;
  document.getElementById('taste-content').innerHTML=`
    <div class="mb-16 card">
      ${compRow}
      ${d.top_genres?.length?`<div class="mb-8"><span class="eyebrow">${th('history.top_genres')}</span><div class="mt-6 flex gap-6 flex-wrap">${d.top_genres.slice(0,8).map(g=>`<span class="badge amber">${esc(g)}</span>`).join('')}</div></div>`:''}
      ${d.top_titles?.length?`<div><span class="eyebrow">${th('history.most_watched')}</span><div class="mt-4 fs-12 t2">${d.top_titles.slice(0,8).map(esc).join(' · ')}</div></div>`:''}
      ${typeSummary?`<div class="footnote">${esc(typeSummary)}</div>`:''}
    </div>`;
}

export function renderRecent(entries, category) {
  if(!entries?.length){document.getElementById('recent-history').innerHTML=emptyHtml(th('history.none_yet', {category: CAT_LABELS[category]||category}));return;}
  const label = CAT_LABELS[category]||category;
  document.getElementById('recent-history').innerHTML=`
    <div class="eyebrow mb-8">${th('history.recent', {category: label})}</div>
    <div class="tbl-wrap"><table class="tbl tbl-cards"><thead><tr><th>${th('history.col_title')}</th><th>${th('history.col_genre')}</th><th>${th('history.col_date')}</th></tr></thead><tbody>
    ${entries.slice(0,50).map(e=>{
      let title=e.title;
      if(e.media_type==='music'&&e.series_title) title=`${e.title} — ${e.series_title}`;
      else if((e.media_type==='anime'||e.media_type==='show')&&e.series_title) title=`${e.series_title}: ${e.title}`;
      return `<tr><td class="cell-primary">${esc(title)}</td><td><span class="t3 fs-12">${esc((e.genres||'').split(',')[0])}</span></td><td class="t3 fs-12 nowrap">${(e.viewed_at||'').slice(0,10)}</td></tr>`;
    }).join('')}
    </tbody></table></div>`;
}
