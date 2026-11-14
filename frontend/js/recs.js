// ── RECOMMENDATIONS ───────────────────────────────────────────────────────────
// Moved state.currentRecsCategory to state
// Moved state._recsPollTimer to state
// Moved state._recsPollKillswitch to state
import { serverLabel, state } from './state.js';
import { skeletonHtml, EL, EVENT, _errHtml, _fmtAbs, _fmtRel, _posterImg, act, actOn, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, loc, toast, loadingHtml } from './ui.js';
import { onDiscussRec } from './chat.js';
import { api } from './api.js';
import { _whyButton } from './deletions.js';
import { tabToHash } from './nav.js';
import { t, th } from './i18n.js';

export function _stopRecsPoll() {
  if (state._recsPollTimer) { clearInterval(state._recsPollTimer); state._recsPollTimer = null; }
  if (state._recsPollKillswitch) { clearTimeout(state._recsPollKillswitch); state._recsPollKillswitch = null; }
}

// Lane presentation metadata for the two recommendation sections.
// (Read when drawn, in the UI's language: i18n keys recs.lane.<lane>.*)
const _LANES = ['library', 'discovery', 'because', 'rediscover'];
const _laneMeta = lane => (_LANES.includes(lane)
  ? { title: t(`recs.lane.${lane}.title`), sub: t(`recs.lane.${lane}.sub`) } : { title: lane, sub: '' });

const _catNoun = c => (['movie', 'show', 'anime'].includes(c) ? t(`common.noun.${c}`) : c);

// "A year ago": what the member finished around today's date last year
// (services/on_this_day). Only on days that have one; each title can be
// hidden for good. No curator text, no push.
async function _yearAgoRow(category) {
  let items = [];
  try { items = (await api('/api/recommendations/on-this-day')).items || []; } catch { return ''; }
  if (category) items = items.filter(x => x.category === category);
  if (!items.length) return '';
  const rows = items.map(x => `<div class="panel-item">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(x.title)} <span class="t3 fs-12">· ${esc(_catNoun(x.category))}${x.episodes > 1 ? ` · ${th('recs.episodes', {n: x.episodes})}` : ''} · <span title="${escAttr(_fmtAbs(x.viewed_at))}">${esc(new Date(x.viewed_at).toLocaleDateString(loc(), { day: 'numeric', month: 'short', year: 'numeric' }))}</span></span></div>
        <button type="button" class="btn btn-ghost btn-sm" title="${th('recs.year_ago.hide_title')}" ${act('hideYearAgo', x.title, EL)}>${th('recs.year_ago.hide')}</button>
      </div></div>`).join('');
  return `<div class="list-head"><span>${th('recs.year_ago.head')}</span><span class="fw-400 fs-12 t3">· ${th('recs.year_ago.sub')} · ${items.length}</span></div>${rows}`;
}

export async function hideYearAgo(title, btn) {
  try {
    await api('/api/recommendations/on-this-day/hide', 'POST', { title });
    toast(t('recs.year_ago.hidden'));
    loadRecs(state.currentRecsCategory);
  } catch (e) { toast(t('recs.year_ago.not_hidden', {error: e.message || e}), 'error'); }
}

// "Because you watched A, B and C": one section per category, headed by its
// three titles (score_factors.anchors, services/because_recs).
function _becauseSections(recs) {
  const groups = new Map();
  for (const r of recs) {
    const a = r.score_factors?.anchors || [];
    const k = `${r.category}|${a.join('|')}`;
    if (!groups.has(k)) groups.set(k, { anchors: a, recs: [] });
    groups.get(k).recs.push(r);
  }
  const names = a => a.length > 1 ? `${a.slice(0, -1).join(', ')} ${t('common.and')} ${a[a.length - 1]}` : (a[0] || '');
  return [...groups.values()].map(g =>
    `<div class="list-head"><span>${th('recs.because_head', {names: names(g.anchors)})}</span><span class="fw-400 fs-12 t3">· ${esc(_laneMeta('because').sub)} · ${g.recs.length}</span></div>`
    + g.recs.map((r, i) => _recCard(r, `because-${r.category}-${i}`)).join('')).join('');
}

// "Why?" on a rec card: the signals it was picked on
// (engine.recommendation_score_factors). The button, its toggle and Escape are
// the Deletions card's; only the panel's words differ, because these are facts
// the pick was made on, not the points of a score. A rec cached before the
// breakdown existed has none and shows no button.
export function _recWhy(rec, key) {
  const f = rec.score_factors?.factors || [];
  if (!f.length) return {btn: '', panel: ''};
  const p = {id: `rec-${key}`, score_factors: {factors: f}};
  return {btn: _whyButton(p), panel: `<div class="why-panel mt-8" id="why-${p.id}" tabindex="-1" hidden>
    <div class="fs-12 t2 mb-6">${th('recs.why.head')}</div>
    <ul class="why-list">${f.map(x => `<li>${esc(x.label)}</li>`).join('')}</ul>
    <div class="fs-11 t3 mt-6">${th('recs.why.foot')}</div>
  </div>`};
}

export function _recCard(rec, key) {
  const why = _recWhy(rec, key);
  const genreTags = (rec.genres||'').split(',').map(g=>g.trim()).filter(Boolean).slice(0,6)
    .map(g=>`<span class="badge muted badge-sm">${esc(g)}</span>`).join('');
  const isLib = ['library', 'because', 'rediscover'].includes(rec.lane);
  const laneBadge = `<span class="badge muted badge-sm" title="${th(isLib ? 'recs.lane_library_title' : 'recs.lane_discovery_title')}">${th(isLib ? 'recs.badge.lib' : 'recs.badge.new')}</span>`
    + (rec.lifted_by ? `<span class="badge amber badge-sm" title="${th('recs.moved_up_title', {title: rec.lifted_by})}">${th('recs.moved_up')}</span>` : '');
  return `
  <div class="card mb-12">
    <div class="poster-card">
      <div class="glow-interactive selectable${rec.category==='music'?' is-music':''}" role="button" tabindex="0" title="${th('recs.discuss_title')}" aria-label="${th('actions.discuss_title', {title: rec.title})}" ${act('discussRecCard', EL)} ${actOn('keydown', 'keyActivate', EVENT, EL)}>${_posterImg(rec.poster_url, 174, rec.category==='music'?174:261, rec.category==='music')}</div>
      <div class="grow">
        <div class="panel-item-head">
          <div class="fs-16 panel-item-title">${esc(rec.title)}${laneBadge}${rec.confidence != null && rec.lane !== 'rediscover' ? `<span class="badge amber badge-sm" title="${th('recs.match_title')}">${th('recs.match', {pct: Math.round(rec.confidence*100)})}</span>` : ''}${why.btn}</div>
          <div class="panel-actions">
            ${isLib && rec.link ? `<a class="btn btn-primary btn-sm" href="${escAttr(rec.link)}" target="_blank" rel="noopener">${th('actions.open_in', {server: serverLabel()})}</a>` : ''}
            ${isLib && rec.category !== 'music' ? `<button type="button" class="btn btn-secondary btn-sm" ${act('titleWatchlist', rec.title, rec.category, EL)}>${th('actions.watchlist')}</button>` : ''}
            <button type="button" class="btn btn-secondary btn-sm" data-discuss ${act('onDiscussRec', EL)} data-title="${escAttr(rec.title)}" data-reason="${escAttr(rec.reason||rec.pitch||'')}" data-category="${escAttr(rec.category)}">${th('actions.discuss')}</button>
            ${rec.lane === 'discovery' && state.currentUser?.is_admin
              ? `<button type="button" class="btn btn-primary btn-sm" ${act('onAddRecToArr', EL)} data-title="${escAttr(rec.title)}" data-category="${escAttr(rec.category)}" data-year="${escAttr(rec.year||'')}">${th('recs.add')}</button>`
              : rec.lane === 'discovery' && rec.category === 'music' && _membersWishViaPlex
                ? `<button type="button" class="btn btn-primary btn-sm" ${act('onWishRec', EL)} data-title="${escAttr(rec.title)}" title="${th('recs.wish_title')}">${th('recs.wish')}</button>`
                : ''}
          </div>
        </div>
        ${why.panel}
        <div class="fs-12 t3 mt-4">${esc(rec.category_label||'')}</div>
        ${genreTags ? `<div class="gap-5 row mt-8">${genreTags}</div>` : ''}
        ${rec.synopsis ? `<div class="clamp-3 fs-12 t3 mt-8">${esc(rec.synopsis)}</div>` : ''}
        <div class="quote-text t2 mt-8">${esc(rec.reason||rec.pitch||'')}</div>
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
  btnBusy(btn, t('common.searching'));
  try {
    if (svc === 'lidarr') {
      // Lidarr optional: without it a wanted artist is a wish the owner fulfils in SoulSync
      const lid = ((await api('/api/library/status')) || {}).lidarr || {};
      if (!lid.configured && lid.music_source === 'plex') {
        const w = await api('/api/library/wish', 'POST', {title, source: 'rec'});
        btnDone(btn, t(w.in_library ? 'recs.lane_library' : 'recs.wished'), {keepDisabled: true});
        toast(t(w.in_library ? 'recs.already_your_music' : 'recs.on_wanted_list', {title, server: serverLabel()}), w.in_library ? 'info' : 'success');
        return;
      }
    }
    const r = await api(`/api/library/search/${encodeURIComponent(svc)}?q=${encodeURIComponent(title)}`);
    const matches = r.matches || [];
    const tl = title.toLowerCase();
    const m = (year && matches.find(x => (x.title||'').toLowerCase() === tl && x.year === year))
           || matches.find(x => (x.title||'').toLowerCase() === tl)
           || matches[0];
    if (!m) { toast(t('recs.no_match', {title, svc}), 'amber'); btnDone(btn); return; }
    if (m.already_added) { btnDone(btn, t('recs.lane_library'), {keepDisabled: true}); return; }
    const res = await confirmDialog({title: t('recs.add_to', {svc}), confirmLabel: t('common.add'),
      body: `<p>${th('recs.add_question_pre')} <b>${esc(m.title)}</b>${m.year ? ` (${m.year})` : ''} ${th('recs.add_question_post', {svc})}</p>`});
    if (!res.ok) { btnDone(btn); return; }
    btnBusy(btn, t('common.adding'));
    await api('/api/library/add', 'POST', {
      service: svc, title: m.title, year: m.year,
      tvdb_id: m.tvdb_id, tmdb_id: m.tmdb_id, mbid: m.mbid,
    });
    btnDone(btn, t('common.added'), {keepDisabled: true});
    toast(t('recs.added_toast', {title: m.title, svc}), 'success');
  } catch (e) {
    let detail = e.message || t('common.failed');
    try { detail = JSON.parse(detail).detail || detail; } catch {}
    toast(detail, 'danger', {ms: 8000});
    btnDone(btn);
  }
}

// Household members can wish for music (owner decision 2026-10-20) — only
// where wishes exist at all: no Lidarr, music through Plex. Read once per
// Recommendations load; the owner's own "+ Add" covers every other case.
let _membersWishViaPlex = false;
async function _refreshWishMode() {
  if (state.currentUser?.is_admin) { _membersWishViaPlex = false; return; }
  try {
    const lid = ((await api('/api/library/status')) || {}).lidarr || {};
    _membersWishViaPlex = !lid.configured && lid.music_source === 'plex';
  } catch { _membersWishViaPlex = false; }
}

export async function onWishRec(btn) {
  const title = btn.dataset.title;
  btnBusy(btn, t('recs.wishing'));
  try {
    const w = await api('/api/library/wish', 'POST', {title, source: 'rec'});
    btnDone(btn, t(w.in_library ? 'recs.lane_library' : 'recs.wished'), {keepDisabled: true});
    toast(t(w.in_library ? 'recs.already_the_music' : 'recs.on_household_list', {title, server: serverLabel()}),
          w.in_library ? 'info' : 'success');
  } catch (e) {
    let detail = e.message || t('common.failed');
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
  el.innerHTML = loadingHtml(t('recs.searching_library'));
  try {
    const cat = state.currentRecsCategory ? `&category=${ state.currentRecsCategory }` : '';
    const r = await api(`/api/library/semantic-search?q=${encodeURIComponent(q)}${cat}&limit=12`);
    const hits = r.results || [];
    const back = `<div class="mb-12"><button type="button" class="btn btn-secondary btn-sm" ${act('reloadRecs')}>${th('recs.back')}</button></div>`;
    if (!hits.length) {
      el.innerHTML = back + emptyHtml(th('recs.no_semantic', {q}));
      return;
    }
    const modeTag = ` · ${th(r.mode === 'evidence' ? 'recs.mode_curated' : 'recs.mode_similarity')}`;
    const anchorTag = r.anchor ? ` · ${th('recs.similar_to', {anchor: r.anchor})}` : '';
    const cov = r.coverage || null;
    const covBanner = (cov && cov.constraints >= 3 && cov.best_met <= 1)
      ? `<div class="banner"><div class="banner-text">${th('recs.partial', {met: cov.best_met, n: cov.constraints})}</div></div>`
      : '';
    el.innerHTML = back +
      `<div class="fs-13 t2 mb-12">${th('recs.semantic_head')} <b class="t-amber">${esc(q)}</b>${state.currentRecsCategory ? ` · ${esc(state.currentRecsCategory)}` : ''}${anchorTag}${modeTag}</div>` +
      covBanner +
      hits.map(h => `
      <div class="card mb-8">
        <div class="panel-item-head">
          <div class="fs-14 panel-item-title">${esc(h.title)}</div>
          <div class="row">
            <span class="badge ${/unwatched|not/i.test(h.watch_tag || '') ? 'muted' : 'amber'}">${esc(h.watch_tag || '')}</span>
            ${h.size_tag ? `<span class="badge muted">${esc(h.size_tag)}</span>` : ''}
          </div>
        </div>
        ${h.genres ? `<div class="fs-11 t3 mt-4">${esc(h.genres)}${h.themes ? ` · ${esc(h.themes)}` : ''}</div>` : ''}
        ${h.fit_note ? `<div class="italic fs-11 t3 mt-4">${esc(h.fit_note)}</div>` : ''}
        ${h.doc ? `<div class="clamp-2 fs-12 t2 mt-8">${esc(h.doc.slice(0, 220))}</div>` : ''}
      </div>`).join('');
  } catch (e) {
    el.innerHTML = _errHtml(e);
  }
}

export function _recSection(lane, recs) {
  if (!recs || !recs.length) return '';
  const m = _laneMeta(lane);
  return `<div class="list-head"><span>${esc(m.title)}</span><span class="fw-400 fs-12 t3">· ${esc(m.sub)} · ${recs.length}</span></div>` + recs.map((r, i) => _recCard(r, `${lane}-${i}`)).join('');
}

export async function loadRecs(category=null, btn=null, refresh=false) {
  state.currentRecsCategory = category;
  // Whatever was running for the previous tab/category dies first — otherwise
  // every category change leaks another timer that keeps fetching forever.
  _stopRecsPoll();

  document.querySelectorAll('#recs-view .cat-tab').forEach(b=>b.classList.remove('active'));
  if(btn) btn.classList.add('active');
  const el = document.getElementById('recs-content');
  el.innerHTML = skeletonHtml(t('common.loading'));
  // Snapshot the category so a stale tab change can't clobber the active view.
  const requestedCategory = category;
  // Both lanes are always cached; state.currentRecsLane just filters the view.
  const laneFilter = state.currentRecsLane || 'all';
  try {
    const url = '/api/recommendations/?' + (category ? `category=${category}&` : '') + `limit=8&refresh=${refresh}&source=cache` + _laneParam();
    const [r] = await Promise.all([api(url), _refreshWishMode()]);
    // If the user clicked another tab or lane while we were fetching, abandon —
    // the newer call already cleared the poll timer and is rendering now.
    if (state.currentRecsCategory !== requestedCategory || (state.currentRecsLane || 'all') !== laneFilter) return;
    if (!r.recommendations?.length) {
      // No cache yet — kick off a background build of BOTH lanes and poll.
      el.innerHTML = emptyHtml(th('recs.generating'));
      _startRefresh(category);
      _pollRecsUntilFresh(category, btn, null);
      return;
    }
    const cacheNote = r.cached_at
      ? `<div class="fs-11 t3 mb-8"><span title="${escAttr(_fmtAbs(r.cached_at))}">${th('recs.cached', {when: _fmtRel(r.cached_at)})}</span></div>`
      : '';
    const library   = r.recommendations.filter(x => x.lane === 'library');
    const because   = r.recommendations.filter(x => x.lane === 'because');
    const rediscover = r.recommendations.filter(x => x.lane === 'rediscover');
    const discovery = r.recommendations.filter(x => !['library', 'because', 'rediscover'].includes(x.lane));
    let body = laneFilter === 'all' ? await _yearAgoRow(category) : '';
    if (laneFilter === 'all' || laneFilter === 'because')   body += _becauseSections(because);
    if (laneFilter === 'all' || laneFilter === 'rediscover') body += _recSection('rediscover', rediscover);
    if (laneFilter === 'all' || laneFilter === 'library')   body += _recSection('library', library);
    if (laneFilter === 'all' || laneFilter === 'discovery') body += _recSection('discovery', discovery);
    if (!body) body = emptyHtml(th('recs.empty_lane'), t('recs.regenerate'), act('regenerateRecs'));
    el.innerHTML = cacheNote + body;
  } catch(e) { el.innerHTML = _errHtml(e); }
}


export function setRecLane(lane, btn) {
  state.currentRecsLane = lane;
  tabToHash('recs', lane);    // #recs/library survives a reload
  for (const [id, val] of [['lane-all-btn','all'], ['lane-because-btn','because'], ['lane-rediscover-btn','rediscover'], ['lane-lib-btn','library'], ['lane-disc-btn','discovery']]) {
    const b = document.getElementById(id);
    if (b) { b.classList.toggle('active', lane === val); b.setAttribute('aria-pressed', String(lane === val)); }
  }
  loadRecs(state.currentRecsCategory);
}

// The lane as the in-view tab of #recs (nav.js registerTabRoute). showView has
// just loaded the view on the current lane, so only a different lane reloads.
export const REC_LANES = ['all', 'because', 'rediscover', 'library', 'discovery'];
export function openRecLane(lane) {
  if ((state.currentRecsLane || 'all') !== lane) setRecLane(lane);
}

// Force a full regeneration of BOTH lanes (the scheduler job), then reload once
// the new cache lands — detected via a changed cached_at timestamp (the cache
// is never empty after the first run, so we can't just poll for presence).
// One poll loop for "the cache is being (re)built": every 15 s ask for the
// cached lanes and reload once something new is there — a moved stamp when
// regenerating, anything at all on a first build. Killed after 5 min and
// whenever the category changes (loadRecs stops it first).
// prevStamp: a string = the stamp to move past; null = first build (wait for
// anything); undefined = unknown (the stamp read failed) — the first answer
// becomes the baseline instead of counting as fresh, which is what the old
// 'none' sentinel did with the OLD cache.
// The poll used to swallow every error, and the 5-minute kill switch left
// "updates on its own" on screen for good; both now say what happened.
const RECS_POLL_MAX_FAILS = 5;
export function _recsFresh(baseline, r) {
  return baseline ? !!(r.cached_at && r.cached_at !== baseline) : !!r.recommendations?.length;
}
export function _pollRecsUntilFresh(cat, btn, prevStamp) {
  _stopRecsPoll();
  let baseline = prevStamp, fails = 0;
  const stillHere = () => state.currentRecsCategory === cat && document.getElementById('recs-content');
  state._recsPollTimer = setInterval(async () => {
    if (state.currentRecsCategory !== cat) { _stopRecsPoll(); return; }
    let r2;
    try {
      r2 = await api('/api/recommendations/?limit=8' + (cat ? `&category=${cat}` : '') + '&source=cache' + _laneParam());
      fails = 0;
    } catch (e) {
      if (++fails >= RECS_POLL_MAX_FAILS) {
        _stopRecsPoll();
        const el = stillHere();
        if (el) el.innerHTML = _errHtml(e, act('reloadRecs'));
      }
      return;
    }
    if (baseline === undefined) { baseline = r2.cached_at || null; if (baseline) return; }
    if (_recsFresh(baseline, r2)) { _stopRecsPoll(); loadRecs(cat, btn, false); }
  }, 15000);
  state._recsPollKillswitch = setTimeout(() => {
    _stopRecsPoll();
    const el = stillHere();
    if (el) el.innerHTML = emptyHtml(th('recs.still_generating'), t('common.reload'), act('reloadRecs'));
  }, 300000);
}

// Start the background build. 409 = one is already running for you, which
// the poll picks up like any other; anything else (429 rate limit, 500)
// means nothing is being built, so the "updates on its own" note goes.
async function _startRefresh(cat) {
  try {
    await api('/api/recommendations/refresh-cache', 'POST');
  } catch (e) {
    if (e.status === 409) return;
    if (state.currentRecsCategory !== cat) return;
    _stopRecsPoll();
    const el = document.getElementById('recs-content');
    if (el) el.innerHTML = _errHtml(e, act('regenerateRecs'));
  }
}

export function _laneParam() {
  return ['library', 'discovery', 'because', 'rediscover'].includes(state.currentRecsLane) ? `&lane=${ state.currentRecsLane }` : '';
}

export async function regenerateRecs() {
  // Minutes of GPU work: asked first, it started on a stray click.
  const ask = await confirmDialog({title: t('recs.regen_title'), body: `<p>${th('recs.regen_body')}</p>`,
                                   confirmLabel: t('recs.regenerate')});
  if (!ask?.ok) return;
  _stopRecsPoll();
  const cat = state.currentRecsCategory;
  let prevStamp;   // undefined: unknown — the poll takes its first answer as the baseline
  try {
    const cur = await api('/api/recommendations/?source=cache' + (cat ? `&category=${cat}` : ''));
    prevStamp = cur.cached_at || null;
  } catch { /* unknown stays undefined, never a sentinel the old stamp would "beat" */ }
  document.getElementById('recs-content').innerHTML =
    emptyHtml(th('recs.regenerating'));
  _pollRecsUntilFresh(cat, null, prevStamp);
  _startRefresh(cat);
}

// Same reason as reloadDeletions: a handler string cannot read module state.
export function reloadRecs() {
  return loadRecs(state.currentRecsCategory);
}

// The poster of a recommendation card discusses the card, like its Discuss button.
export function discussRecCard(el) { onDiscussRec(el.closest('.card').querySelector('[data-discuss]')); }
