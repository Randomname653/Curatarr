// ── RECOMMENDATIONS ───────────────────────────────────────────────────────────
// Moved state.currentRecsCategory to state
// Moved state._recsPollTimer to state
// Moved state._recsPollKillswitch to state
import { state } from './state.js';
import { _errHtml, _fmtAbs, _fmtRel, _posterImg, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, toast } from './ui.js';
import { api } from './api.js';

export function _stopRecsPoll() {
  if (state._recsPollTimer) { clearInterval(state._recsPollTimer); state._recsPollTimer = null; }
  if (state._recsPollKillswitch) { clearTimeout(state._recsPollKillswitch); state._recsPollKillswitch = null; }
}

// Lane presentation metadata for the two recommendation sections.
const _LANE_META = {
  library:   { title: 'From your library', sub: 'you own it · still unwatched' },
  discovery: { title: 'Discover new',      sub: 'not in your library · worth acquiring' },
};

export function _recCard(rec) {
  const genreTags = (rec.genres||'').split(',').map(g=>g.trim()).filter(Boolean).slice(0,6)
    .map(g=>`<span class="badge muted badge-sm">${esc(g)}</span>`).join('');
  const isLib = rec.lane === 'library';
  const laneBadge = `<span class="badge muted badge-sm" title="${isLib ? 'In your library — still unwatched' : 'Not in your library — worth acquiring'}">${isLib ? 'lib' : 'new'}</span>`;
  return `
  <div class="card mb-12">
    <div class="poster-card">
      <div class="glow-interactive selectable${rec.category==='music'?' is-music':''}" role="button" tabindex="0" title="Discuss this recommendation" aria-label="Discuss ${escAttr(rec.title)}" onclick="onDiscussRec(this.closest('.card').querySelector('[data-discuss]'))" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">${_posterImg(rec.poster_url, 174, rec.category==='music'?174:261, rec.category==='music')}</div>
      <div class="grow">
        <div class="panel-item-head">
          <div class="panel-item-title" style="font-size:16px">${esc(rec.title)}${laneBadge}<span class="badge amber badge-sm" title="How well it fits your taste">${Math.round((rec.confidence||0.7)*100)}%</span></div>
          <div class="panel-actions">
            <button type="button" class="btn btn-secondary btn-sm" data-discuss onclick="onDiscussRec(this)" data-title="${escAttr(rec.title)}" data-reason="${escAttr(rec.reason||rec.pitch||'')}" data-category="${escAttr(rec.category)}">Discuss</button>
            ${rec.lane === 'discovery' && state.currentUser?.is_admin
              ? `<button type="button" class="btn btn-primary btn-sm" onclick="onAddRecToArr(this)" data-title="${escAttr(rec.title)}" data-category="${escAttr(rec.category)}" data-year="${escAttr(rec.year||'')}">+ Add</button>`
              : ''}
          </div>
        </div>
        <div class="fs-12 t3 mt-4">${esc(rec.category_label||'')}</div>
        ${genreTags ? `<div class="row mt-8" style="gap:5px">${genreTags}</div>` : ''}
        ${rec.synopsis ? `<div class="fs-12 t3 mt-8" style="line-height:1.55;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${esc(rec.synopsis)}</div>` : ''}
        <div class="t2 mt-8" style="font-size:13.5px;line-height:1.65">${esc(rec.reason||rec.pitch||'')}</div>
      </div>
    </div>
  </div>`;
}

// Feature B: one-click acquire from a Discovery pitch. Admin-only button
// (POST /api/library/add 403s for everyone else as defense-in-depth) —
// resolves the pitch title against the arr's own lookup, confirms, then
// adds with the saved defaults + curatarr tag.
export async function onAddRecToArr(btn) {
  const title = btn.dataset.title;
  const year = parseInt(btn.dataset.year, 10) || null;
  const svc = {movie:'radarr', show:'sonarr', anime:'sonarr', music:'lidarr'}[btn.dataset.category];
  if (!svc) return;
  btnBusy(btn, 'Searching…');
  try {
    const r = await api(`/api/library/search/${svc}?q=${encodeURIComponent(title)}`);
    const matches = r.matches || [];
    const tl = title.toLowerCase();
    const m = (year && matches.find(x => (x.title||'').toLowerCase() === tl && x.year === year))
           || matches.find(x => (x.title||'').toLowerCase() === tl)
           || matches[0];
    if (!m) { toast(`No match for "${title}" in ${svc} — try Add New in the ${svc} view`, 'amber'); btnDone(btn); return; }
    if (m.already_added) { btnDone(btn, 'In library', {keepDisabled: true}); return; }
    const res = await confirmDialog({title: `Add to ${svc}`, confirmLabel: 'Add',
      body: `<p>Add <b>${esc(m.title)}</b>${m.year ? ` (${m.year})` : ''} to ${esc(svc)}?</p>`});
    if (!res.ok) { btnDone(btn); return; }
    btnBusy(btn, 'Adding…');
    await api('/api/library/add', 'POST', {
      service: svc, title: m.title, year: m.year,
      tvdb_id: m.tvdb_id, tmdb_id: m.tmdb_id, mbid: m.mbid,
    });
    btnDone(btn, 'Added', {keepDisabled: true});
    toast(`Added "${m.title}" to ${svc}`, 'success');
  } catch (e) {
    let detail = e.message || 'failed';
    try { detail = JSON.parse(detail).detail || detail; } catch {}
    toast(detail, 'danger', {ms: 8000});
    btnDone(btn);
  }
}

// Semantic library search — the same ChromaDB retrieval as the chat's hidden
// RAG, surfaced directly: ranked OWNED titles with watched/size badges.
export async function searchLibrary() {
  const q = (document.getElementById('lib-search')?.value || '').trim();
  if (q.length < 2) return;
  _stopRecsPoll();
  const el = document.getElementById('recs-content');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Searching + curating your library…</p>';
  try {
    const cat = state.currentRecsCategory ? `&category=${ state.currentRecsCategory }` : '';
    const r = await api(`/api/library/semantic-search?q=${encodeURIComponent(q)}${cat}&limit=12`);
    const hits = r.results || [];
    const back = `<div class="mb-12"><button type="button" class="btn btn-secondary btn-sm" onclick="loadRecs(state.currentRecsCategory)">← Back to recommendations</button></div>`;
    if (!hits.length) {
      el.innerHTML = back + emptyHtml(`No semantic matches for "${esc(q)}" — coverage follows the enrichment index.`);
      return;
    }
    const modeTag = r.mode === 'evidence' ? ' · curated' : ' · similarity only';
    const anchorTag = r.anchor ? ` · similar to ${esc(r.anchor)}` : '';
    const cov = r.coverage || null;
    const covBanner = (cov && cov.constraints >= 3 && cov.best_met <= 1)
      ? `<div class="banner"><div class="banner-text">No library title carries this full profile (best match: ${cov.best_met}/${cov.constraints} criteria) — showing partial matches.</div></div>`
      : '';
    el.innerHTML = back +
      `<div class="fs-13 t2 mb-12">Semantic matches in your library for <b class="t-amber">${esc(q)}</b>${state.currentRecsCategory ? ` · ${esc(state.currentRecsCategory)}` : ''}${anchorTag}${modeTag}</div>` +
      covBanner +
      hits.map(h => `
      <div class="card mb-8">
        <div class="panel-item-head">
          <div class="panel-item-title" style="font-size:14px">${esc(h.title)}</div>
          <div class="row">
            <span class="badge ${/unwatched|not/i.test(h.watch_tag || '') ? 'muted' : 'amber'}">${esc(h.watch_tag || '')}</span>
            ${h.size_tag ? `<span class="badge muted">${esc(h.size_tag)}</span>` : ''}
          </div>
        </div>
        ${h.genres ? `<div class="fs-11 t3 mt-4">${esc(h.genres)}${h.themes ? ` · ${esc(h.themes)}` : ''}</div>` : ''}
        ${h.fit_note ? `<div class="fs-11 t3 mt-4" style="font-style:italic">${esc(h.fit_note)}</div>` : ''}
        ${h.doc ? `<div class="fs-12 t2 mt-8" style="line-height:1.5;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">${esc(h.doc.slice(0, 220))}</div>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = _errHtml(e);
  }
}

export function _recSection(lane, recs) {
  if (!recs || !recs.length) return '';
  const m = _LANE_META[lane] || { title:lane, sub:'' };
  return `<div class="list-head"><span>${esc(m.title)}</span><span class="fs-12 t3" style="font-weight:400">· ${esc(m.sub)} · ${recs.length}</span></div>` + recs.map(_recCard).join('');
}

export async function loadRecs(category=null, btn=null, refresh=false) {
  state.currentRecsCategory = category;
  // Whatever was running for the previous tab/category dies first — otherwise
  // every category change leaks another timer that keeps fetching forever.
  _stopRecsPoll();

  document.querySelectorAll('#recs-view .cat-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  const el = document.getElementById('recs-content');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  // Snapshot the category so a stale tab change can't clobber the active view.
  const requestedCategory = category;
  // Both lanes are always cached; state.currentRecsLane just filters the view.
  const laneFilter = state.currentRecsLane || 'all';
  try {
    const url = '/api/recommendations/?' + (category ? `category=${category}&` : '') + `limit=8&refresh=${refresh}&source=cache` + _laneParam();
    const r = await api(url);
    // If the user clicked another tab while we were fetching, abandon — the
    // newer call already cleared the poll timer and is rendering now.
    if (state.currentRecsCategory !== requestedCategory) return;
    if (!r.recommendations?.length) {
      // No cache yet — kick off a background build of BOTH lanes and poll.
      el.innerHTML = emptyHtml('Generating personalised recommendations — your library picks and fresh discoveries, ~2-4 min the first time. The tab updates on its own.');
      api('/api/recommendations/refresh-cache', 'POST').catch(()=>{});
      _pollRecsUntilFresh(category, btn, null);
      return;
    }
    const cacheNote = r.cached_at
      ? `<div class="fs-11 t3 mb-8"><span title="${escAttr(_fmtAbs(r.cached_at))}">Cached ${_fmtRel(r.cached_at)}</span> · <button type="button" class="link-num t-amber" onclick="regenerateRecs()">Regenerate now</button></div>`
      : '';
    const library   = r.recommendations.filter(x => x.lane === 'library');
    const discovery = r.recommendations.filter(x => x.lane !== 'library');
    let body = '';
    if (laneFilter === 'all' || laneFilter === 'library')   body += _recSection('library', library);
    if (laneFilter === 'all' || laneFilter === 'discovery') body += _recSection('discovery', discovery);
    if (!body) body = emptyHtml('Nothing in this lane yet — regenerate, or switch lane above.', 'Regenerate', 'regenerateRecs()');
    el.innerHTML = cacheNote + body;
  } catch(e) { el.innerHTML = _errHtml(e); }
}


export function setRecLane(lane, btn) {
  state.currentRecsLane = lane;
  for (const [id, val] of [['lane-all-btn','all'], ['lane-lib-btn','library'], ['lane-disc-btn','discovery']]) {
    const b = document.getElementById(id);
    if (b) b.classList.toggle('active', lane === val);
  }
  loadRecs(state.currentRecsCategory);
}

// Force a full regeneration of BOTH lanes (the scheduler job), then reload once
// the new cache lands — detected via a changed cached_at timestamp (the cache
// is never empty after the first run, so we can't just poll for presence).
// One poll loop for "the cache is being (re)built": every 15 s ask for the
// cached lanes and reload once something new is there — a moved stamp when
// regenerating, anything at all on a first build. Killed after 5 min and
// whenever the category changes (loadRecs stops it first).
export function _pollRecsUntilFresh(cat, btn, prevStamp) {
  _stopRecsPoll();
  state._recsPollTimer = setInterval(async () => {
    if (state.currentRecsCategory !== cat) { _stopRecsPoll(); return; }
    try {
      const r2 = await api('/api/recommendations/?limit=8' + (cat ? `&category=${cat}` : '') + '&source=cache' + _laneParam());
      const fresh = prevStamp ? (r2.cached_at && r2.cached_at !== prevStamp) : !!r2.recommendations?.length;
      if (fresh) { _stopRecsPoll(); loadRecs(cat, btn, false); }
    } catch {}
  }, 15000);
  state._recsPollKillswitch = setTimeout(_stopRecsPoll, 300000);
}

export function _laneParam() {
  return (state.currentRecsLane === 'library' || state.currentRecsLane === 'discovery') ? `&lane=${ state.currentRecsLane }` : '';
}

export async function regenerateRecs() {
  _stopRecsPoll();
  const cat = state.currentRecsCategory;
  let prevStamp = null;
  try {
    const cur = await api('/api/recommendations/?source=cache' + (cat ? `&category=${cat}` : ''));
    prevStamp = cur.cached_at || null;
  } catch {}
  document.getElementById('recs-content').innerHTML =
    emptyHtml('Regenerating both lanes — library picks and fresh discoveries across every category, ~2-4 min. The view updates on its own.');
  api('/api/recommendations/refresh-cache', 'POST').catch(()=>{});
  _pollRecsUntilFresh(cat, null, prevStamp || 'none');
}
