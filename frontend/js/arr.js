// ── Pass 16c: Library Manager (Sonarr/Radarr/Lidarr pages) ───────────────
//
// Each arr page has the same shape:
//   1. Status check via /api/library/status
//   2. If not configured → setup banner with "Configure now" button that
//      jumps to Settings → Library
//   3. Otherwise → tab-bar (per-arr layout) + tab content placeholder
//      (real tabs filled in Pass 16d-g)
//
// Tab definitions per arr — adjust here when new tabs land.
import { api } from './api.js';
import { skeletonHtml, EL, _errHtml, _errMsg, _fmtRel, _posterImg, act, actOn, btnBusy, btnDone, emptyHtml, esc, escAttr, loc, openModal, proxyImg, setStatus, toast } from './ui.js';
import { serverLabel, state } from './state.js';
import { showView, tabToHash } from './nav.js';
import { t, th } from './i18n.js';
// A tab's words are in the dictionary (arr.tab.*): `key` names them.
const ARR_TABS = {
  sonarr: [
    { id: 'all',    key: 'arr.tab.all_series' },
    { id: 'tv',     key: 'arr.tab.tv' },
    { id: 'anime',  key: 'arr.tab.anime' },
    { id: 'curatarr', key: 'arr.tab.curatarr' },
    { id: 'add',    key: 'arr.tab.add' },
  ],
  radarr: [
    { id: 'all',    key: 'arr.tab.all_movies' },
    { id: 'curatarr', key: 'arr.tab.curatarr' },
    { id: 'add',    key: 'arr.tab.add' },
  ],
  lidarr: [
    { id: 'all',    key: 'arr.tab.all_artists' },
    { id: 'curatarr', key: 'arr.tab.curatarr' },
    { id: 'backlog', key: 'arr.tab.backlog' },
    { id: 'wanted', key: 'arr.tab.wanted' },
    { id: 'add',    key: 'arr.tab.add' },
  ],
};

// Active tab per arr — survives tab switches; a reload restores it from the
// hash (#arr-sonarr/anime, nav.js › registerTabRoute).
const _arrActiveTab = { sonarr: 'all', radarr: 'all', lidarr: 'all' };
export const arrActiveTab = svc => _arrActiveTab[svc];
// The tabs actually shown per arr: Lidarr's list shrinks to Library + Spotify
// Backlog when the page runs on the Plex music index (no arr to add to).
const _arrTabsEff = {};
// 'lidarr' | 'plex' | null — what the Music page runs on (from /api/library/status).
let _arrMusicSource = null;

// `tab`: the one the address names (nav.js); omitted, the last one open.
export async function loadArrPage(svc, tab) {
  if (tab) _arrActiveTab[svc] = tab;
  const tabsEl = document.getElementById(`arr-tabs-${svc}`);
  const contentEl = document.getElementById(`arr-content-${svc}`);
  contentEl.innerHTML = skeletonHtml(t('common.loading'), {kind: 'rows'});
  tabsEl.innerHTML = '';

  let status;
  try {
    status = await api('/api/library/status');
  } catch (e) {
    contentEl.innerHTML = _errHtml(e, act('loadArrPage', svc));
    return;
  }

  const info = (status || {})[svc] || {};
  // Lidarr optional: the Music page runs on the Plex music index without it.
  const viaPlex = svc === 'lidarr' && !info.configured && info.music_source === 'plex';
  _arrMusicSource = svc === 'lidarr' ? (info.music_source || (info.configured ? 'lidarr' : null)) : _arrMusicSource;
  if (svc === 'lidarr') {
    // the header says what the page runs on
    const h = document.querySelector('#arr-lidarr-view .view-header');
    const sub = h && h.querySelector('h1 span'), lead = h && h.querySelector('p');
    if (sub) sub.textContent = viaPlex ? `via ${serverLabel()}` : 'via Lidarr';
    if (lead) lead.textContent = viaPlex
      ? t('arr.lead_plex', {server: serverLabel()})
      : t('arr.lead_lidarr');
  }
  _arrTabsEff[svc] = viaPlex
    ? ARR_TABS[svc].filter(t => ['all', 'backlog', 'wanted'].includes(t.id))
    : ARR_TABS[svc].filter(t => t.id !== 'wanted');
  if (!info.configured && !viaPlex) {
    // Setup banner — admins get a CTA, non-admins get a "ask your admin"
    // message. The Settings → Arr apps pane is admin-only, so showing the
    // Configure button to a non-admin would lead them to a hidden tab
    // (Pass 16l).
    const isAdmin = !!(state.currentUser && state.currentUser.is_admin);
    const titleSvc = svc.charAt(0).toUpperCase() + svc.slice(1);
    contentEl.innerHTML = isAdmin
      ? `
        <div class="arr-setup-banner">
          <h3>${th('arr.not_configured', {svc: titleSvc})}</h3>
          <p>${th('arr.configure_hint')}</p>
          <button class="btn btn-primary" ${act('goToLibrarySettings', svc)}>${th('arr.configure', {svc})}</button>
        </div>
      `
      : `
        <div class="arr-setup-banner">
          <h3>${th('arr.not_configured', {svc: titleSvc})}</h3>
          <p class="t2">${th('arr.ask_admin', {svc}).replace('{where}', `<em>${th('arr.settings_library')}</em>`)}</p>
        </div>
      `;
    return;
  }

  // Render tabs. A tab this page does not have (a stale bookmark, Lidarr
  // switched to the Plex index) opens the first one instead.
  const tabs = _arrTabsEff[svc] || ARR_TABS[svc] || [];
  if (tabs.length && !tabs.some(t => t.id === _arrActiveTab[svc])) _arrActiveTab[svc] = tabs[0].id;
  tabToHash('arr-' + svc, _arrActiveTab[svc]);
  tabsEl.innerHTML = tabs.map(tb =>
    `<button class="arr-tab ${_arrActiveTab[svc] === tb.id ? 'active' : ''}" ${act('setArrTab', svc, tb.id)}>${th(tb.key)}</button>`
  ).join('');
  renderArrTab(svc, _arrActiveTab[svc]);
}

export function setArrTab(svc, tabId) {
  _arrActiveTab[svc] = tabId;
  tabToHash('arr-' + svc, tabId);
  document.querySelectorAll(`#arr-tabs-${svc} .arr-tab`).forEach((b, i) => {
    b.classList.toggle('active', ((_arrTabsEff[svc] || ARR_TABS[svc])[i] || {}).id === tabId);
  });
  renderArrTab(svc, tabId);
}

export function renderArrTab(svc, tabId) {
  // Synopsis Browser tabs (Pass 16d): all, tv, anime, curatarr
  // Add New tab (Pass 16e): add
  // Spotify Backlog tab (Pass 16g, lidarr only): backlog
  if (tabId === 'wanted') { renderWanted(svc); return; }
  if (['all', 'tv', 'anime', 'curatarr'].includes(tabId)) {
    renderSynopsisBrowser(svc, tabId);
  } else if (tabId === 'add') {
    renderAddNew(svc);
  } else if (tabId === 'backlog' && svc === 'lidarr') {
    renderSpotifyBacklog(svc);
  } else {
    const tabKey = (ARR_TABS[svc].find(tb => tb.id === tabId) || {}).key;
    const tabLabel = tabKey ? t(tabKey) : tabId;
    const contentEl = document.getElementById(`arr-content-${svc}`);
    contentEl.innerHTML = `
      <div class="empty-note">
        <p class="m-0 mb-6"><strong>${esc(tabLabel)}</strong></p>
        <p class="m-0 fs-12">${th('arr.skeleton')}</p>
      </div>
    `;
  }
}

// ── Pass 16d: Synopsis Browser ───────────────────────────────────────────
//
// Renders a sortable + filterable list of arr items with their enrichment
// status (synopsis snippet + last-update). Three Re-Enrich actions per
// row: Refresh metadata / Refresh summary / Refresh both — fire-and-
// forget background tasks (curator hint shows the queue status).

const _arrBrowserState = {
  // svc → { sort, needs_enrichment, items, loaded_at }
};

export async function renderSynopsisBrowser(svc, tabId, opts = {}) {
  const contentEl = document.getElementById(`arr-content-${svc}`);
  contentEl.innerHTML = skeletonHtml(t('common.loading'), {kind: 'rows'});

  const st = _arrBrowserState[svc + ':' + tabId] || {
    sort: 'size_desc',
    needs_enrichment: false,
    search: '',
  };
  _arrBrowserState[svc + ':' + tabId] = st;

  // Map tabId → backend filter params
  const params = new URLSearchParams({ sort: st.sort, limit: '500' });
  if (st.needs_enrichment) params.set('needs_enrichment', 'true');
  if (st.search)          params.set('search', st.search);
  if (tabId === 'tv')        params.set('root_filter', 'tv');
  if (tabId === 'anime')     params.set('root_filter', 'anime');
  if (tabId === 'curatarr')  params.set('curatarr_only', 'true');
  // Pass 16i: explicit refresh bypasses the 15-min server-side cache
  if (opts.forceRefresh)     params.set('refresh', 'true');

  let data;
  try {
    data = await api(`/api/library/items/${encodeURIComponent(svc)}?${params.toString()}`);
  } catch (e) {
    contentEl.innerHTML = `${_errHtml(e)}
      <p class="fs-12 t3 mt-8">${th('arr.first_fetch', {svc})}</p>
      <div class="mt-8"><button type="button" class="btn btn-secondary btn-sm" ${act('renderSynopsisBrowser', svc, tabId, {forceRefresh: true})}>${th('common.retry')}</button></div>`;
    return;
  }

  // Pass 16i: cache badge — 'live' (just fetched), 'cache' (Xm old), 'stale' (arr down)
  const ci = data.cache || {};
  const ageMin = Math.floor((ci.age_s || 0) / 60);
  const ageStr = ageMin === 0 ? t('time.just_now') : t('time.minutes_ago', {n: ageMin});
  const cacheBadge =
      ci.source === 'live'
    ? `<span class="badge muted badge-sm" title="${th('arr.cache_live_title', {svc})}">${th('arr.cache_live')}</span>`
    : ci.source === 'cache'
    ? `<span class="badge muted badge-sm" title="${th('arr.cache_cached_title', {age: ageStr})}">${th('arr.cache_cached', {age: ageStr})}</span>`
    : ci.source === 'plex-index'
    ? `<span class="badge info badge-sm" title="${th('arr.cache_index_title', {server: serverLabel()})}">${th('arr.cache_index', {server: serverLabel()})}</span>`
    : ci.source === 'stale'
    ? `<span class="badge amber badge-sm" title="${th('arr.cache_stale_title', {svc, error: ci.stale_error || t('arr.connection_failed'), age: ageStr})}">${th('arr.cache_stale', {age: ageStr})}</span>`
    : '';

  st.items = data.items || [];
  const SEARCH_IC = '<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';
  const REFRESH_IC = '<svg aria-hidden="true" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12a9 9 0 1 1-2.64-6.36"/><polyline points="21 3 21 9 15 9"/></svg>';
  // Search first (the one control everyone uses), then sort, the filter and
  // a quiet refresh; the count and cache state sit under it.
  const toolbar = `
    <div class="lib-toolbar">
      <label class="lib-search">${SEARCH_IC}
        <input type="search" id="arr-search-${svc}-${tabId}" class="input" aria-label="${th('arr.search_titles')}" value="${escAttr(st.search || '')}"
               placeholder="${th('arr.search_title_ph')}" ${actOn('input', 'onBrowserSearch', svc, tabId, EL)}></label>
      <select id="arr-sort-${svc}-${tabId}" class="input lib-sort" aria-label="${th('arr.sort')}" ${actOn('change', 'onBrowserSort', svc, tabId, EL)}>
        <option value="size_desc"            ${st.sort === 'size_desc' ? 'selected' : ''}>${th('arr.sort_size_desc')}</option>
        <option value="size_asc"             ${st.sort === 'size_asc' ? 'selected' : ''}>${th('arr.sort_size_asc')}</option>
        <option value="added_desc"           ${st.sort === 'added_desc' ? 'selected' : ''}>${th('arr.sort_added_desc')}</option>
        <option value="added_asc"            ${st.sort === 'added_asc' ? 'selected' : ''}>${th('arr.sort_added_asc')}</option>
        <option value="synopsis_updated_asc" ${st.sort === 'synopsis_updated_asc' ? 'selected' : ''}>${th('arr.sort_synopsis')}</option>
        <option value="title_asc"            ${st.sort === 'title_asc' ? 'selected' : ''}>${th('arr.sort_title')}</option>
      </select>
      <label class="chip${st.needs_enrichment ? ' active' : ''}" title="${th('arr.needs_enrichment_title')}">
        <input type="checkbox" ${st.needs_enrichment ? 'checked' : ''} ${actOn('change', 'onBrowserFilter', svc, tabId, EL)}> ${th('arr.needs_enrichment')}</label>
      <button type="button" class="btn-plain lib-refresh" ${act('renderSynopsisBrowser', svc, tabId, {forceRefresh: true})}
              title="${th('arr.refetch_title', {svc})}" aria-label="${th('arr.refresh_from', {svc})}">${REFRESH_IC}</button>
    </div>
    <div class="lib-count fs-12 t3"><b>${data.total}</b> ${th('arr.titles', {n: data.total})} ${cacheBadge}</div>
  `;

  if (!data.items || data.items.length === 0) {
    contentEl.innerHTML = toolbar + emptyHtml(th('arr.no_items'));
    return;
  }

  const cards = data.items.map((it, i) => renderArrItemCard(svc, tabId, it, i)).join('');
  contentEl.innerHTML = toolbar + `<div class="lib-grid${svc === 'lidarr' ? ' is-music' : ''}">${cards}</div>`;
}

function _sizeStr(bytes) {
  const mb = (bytes || 0) / (1024 * 1024);
  return mb > 1024 ? `${(mb / 1024).toFixed(1)} GB` : mb > 0 ? `${mb.toFixed(0)} MB` : '';
}

// Enrichment is the admin's business: a card only carries a small dot when
// something is missing, the words live in the detail sheet.
function _enrichState(item) {
  if (!item.has_enrichment) return {cls: 'amber', label: t('arr.no_synopsis_yet')};
  if ((item.enrichment_source || '').startsWith('raw')) return {cls: 'amber', label: t('arr.raw_metadata')};
  return {cls: 'success', label: t('arr.enriched_via', {source: item.enrichment_source || 'cache'})};
}

function _libPoster(item, cls) {
  const initial = esc((item.title || '?').trim().slice(0, 1).toUpperCase() || '?');
  return item.poster
    ? `<img class="${cls}" src="${esc(proxyImg(item.poster))}" alt="" loading="lazy" decoding="async" ${actOn('error', 'hideOnError', EL)}><span class="${cls} lib-ph" aria-hidden="true">${initial}</span>`
    : `<span class="${cls} lib-ph" aria-hidden="true">${initial}</span>`;
}

export function renderArrItemCard(svc, tabId, item, idx) {
  const meta = [item.year, _sizeStr(item.size_on_disk)].filter(Boolean).join(' · ');
  const en = _enrichState(item);
  return `
    <button type="button" class="lib-card" ${act('openLibraryItem', svc, tabId, idx)}>
      <span class="lib-poster">${_libPoster(item, 'lib-img')}
        ${en.cls === 'amber' ? `<span class="lib-flag" title="${esc(en.label)}"><span class="sr-only">${esc(en.label)}</span></span>` : ''}</span>
      <span class="lib-card-t">${esc(item.title || t('arr.untitled'))}</span>
      ${meta ? `<span class="lib-card-m">${esc(meta)}</span>` : ''}
    </button>`;
}

// One title in full: poster, facts, synopsis - and, for admins, the
// Re-enrich actions that used to sit on every row.
export function openLibraryItem(svc, tabId, idx) {
  const item = (_arrBrowserState[svc + ':' + tabId]?.items || [])[idx];
  if (!item) return;
  const en = _enrichState(item);
  const isAdmin = !!state.currentUser?.is_admin;
  const facts = [
    item.year,
    _sizeStr(item.size_on_disk),
    item.album_count ? t('arr.albums', {n: item.album_count}) : '',
    item.added ? t('arr.added_on', {date: new Date(item.added).toLocaleDateString(loc())}) : '',
    item.status,
  ].filter(Boolean).map(f => esc(String(f))).join(' · ');
  openModal({
    title: item.title || t('arr.untitled'),
    size: 'narrow',
    body: `
      <div class="lib-detail${svc === 'lidarr' ? ' is-music' : ''}">
        <span class="lib-detail-poster">${_libPoster(item, 'lib-img')}</span>
        <div class="lib-detail-facts">
          ${facts ? `<div class="fs-13 t2">${facts}</div>` : ''}
          <span class="badge ${en.cls} badge-sm">${esc(en.label)}</span>
          ${item.root_folder ? `<code class="fs-11 t3 lib-root">${esc(item.root_folder)}</code>` : ''}
        </div>
      </div>
      <p class="lib-detail-syn">${item.synopsis ? esc(item.synopsis) : `<span class="italic t3">${th('arr.no_synopsis_on_file')}</span>`}</p>
      ${isAdmin ? `
        <div class="lib-detail-h">${th('arr.reenrich')}</div>
        <div class="row">
          <button type="button" class="btn btn-secondary btn-sm" ${act('reEnrich', svc, item.id, 'metadata')} title="${th('arr.reenrich_metadata_title')}">${th('arr.reenrich_metadata')}</button>
          <button type="button" class="btn btn-secondary btn-sm" ${act('reEnrich', svc, item.id, 'summary')} title="${th('arr.reenrich_summary_title')}">${th('arr.reenrich_summary')}</button>
          <button type="button" class="btn btn-secondary btn-sm" ${act('reEnrich', svc, item.id, 'both')}>${th('arr.reenrich_both')}</button>
        </div>` : ''}`,
  });
}

export function setBrowserSort(svc, tabId, sort) {
  const st = _arrBrowserState[svc + ':' + tabId] || {};
  st.sort = sort;
  _arrBrowserState[svc + ':' + tabId] = st;
  renderSynopsisBrowser(svc, tabId);
}

export function setBrowserFilter(svc, tabId, needsEnrichment) {
  const st = _arrBrowserState[svc + ':' + tabId] || {};
  st.needs_enrichment = needsEnrichment;
  _arrBrowserState[svc + ':' + tabId] = st;
  renderSynopsisBrowser(svc, tabId);
}

// Debounced title search: re-render destroys the input, so wait for a typing
// pause, then restore focus + caret so the user can keep typing seamlessly.
let _browserSearchTimer = null;
export function setBrowserSearch(svc, tabId, value) {
  const st = _arrBrowserState[svc + ':' + tabId] || {};
  st.search = value;
  _arrBrowserState[svc + ':' + tabId] = st;
  clearTimeout(_browserSearchTimer);
  _browserSearchTimer = setTimeout(async () => {
    await renderSynopsisBrowser(svc, tabId);
    const inp = document.getElementById(`arr-search-${svc}-${tabId}`);
    if (inp) { inp.focus(); inp.setSelectionRange(inp.value.length, inp.value.length); }
  }, 400);
}

export async function reEnrich(svc, arrId, mode) {
  const what = ['metadata', 'summary', 'both'].includes(mode) ? t(`arr.reenrich_what.${mode}`) : mode;
  try {
    await api('/api/library/reenrich', 'POST', { service: svc, arr_id: arrId, mode });
    toast(t('arr.reenrich_queued', {what}), 'success');
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
}

// ── Pass 16e: Add New (live debounced search + add with curatarr tag) ────

// Per-service search debouncer + last query (so we don't fire concurrent
// requests when the user is still typing).
const _addNewState = {
  // svc → { timer, lastQuery, controller, results }
};

export function renderAddNew(svc) {
  const contentEl = document.getElementById(`arr-content-${svc}`);
  const placeholder = svc === 'lidarr' ? t('arr.add_ph_artist')
    : svc === 'radarr' ? t('arr.add_ph_movie')
    : t('arr.add_ph_show');
  contentEl.innerHTML = `
    <div class="toolbar">
      <input type="search" id="add-search-${svc}" class="pad-9-12 fs-13 input grow" aria-label="${th('arr.search_title_aria')}" placeholder="${esc(placeholder)}" ${actOn('input', 'debouncedAddSearch', svc)}>
      <span id="add-search-status-${svc}" class="minw-80 status"></span>
    </div>
    <div id="add-search-results-${svc}"></div>
    <p class="fs-11 t3 mt-8">
      ${th('arr.add_hint').replace('{link}', `<button type="button" class="link-num t-amber" ${act('goToLibrarySettings', svc)}>${th('arr.settings_library')}</button>`).replace('{tag}', '<code>curatarr</code>')}
    </p>
  `;
  // Focus the input
  setTimeout(() => document.getElementById(`add-search-${svc}`)?.focus(), 50);
}

export function debouncedAddSearch(svc) {
  const st = _addNewState[svc] || (_addNewState[svc] = {});
  if (st.timer) clearTimeout(st.timer);
  st.timer = setTimeout(() => addSearchExec(svc), 350);
}

export async function addSearchExec(svc) {
  const inp = document.getElementById(`add-search-${svc}`);
  const out = document.getElementById(`add-search-results-${svc}`);
  const stat = document.getElementById(`add-search-status-${svc}`);
  if (!inp || !out) return;
  const q = (inp.value || '').trim();
  if (q.length < 2) {
    out.innerHTML = '';
    stat.textContent = q.length ? t('arr.min_chars') : '';
    return;
  }

  // Cancel any in-flight request for this service
  const st = _addNewState[svc];
  if (st.controller) {
    try { st.controller.abort(); } catch {}
  }
  st.controller = new AbortController();

  setStatus(stat, t('arr.searching'), 'busy');
  try {
    const headers = {};
    if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
    const r = await fetch(`/api/library/search/${encodeURIComponent(svc)}?q=${encodeURIComponent(q)}`, {
      headers, signal: st.controller.signal,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: r.statusText}));
      setStatus(stat, err.detail || t('common.failed'), 'err');
      out.innerHTML = '';
      return;
    }
    const data = await r.json();
    st.results = data.matches || [];
    setStatus(stat, t('arr.hits', {n: data.matches.length}), null);
    if (data.matches.length === 0) {
      out.innerHTML = emptyHtml(th('arr.no_matches'));
      return;
    }
    out.innerHTML = data.matches.map((m, idx) => renderAddCard(svc, m, idx)).join('');
  } catch (e) {
    if (e.name === 'AbortError') return;   // newer request superseded us
    setStatus(stat, e.message || t('common.failed'), 'err');
  }
}

export function renderAddCard(svc, m, idx) {
  const overview = m.overview ? esc(m.overview) : '';
  const action = m.already_added
    ? `<span class="badge muted badge-sm">${th('arr.already_in_library')}</span>`
    : `<button type="button" class="btn btn-primary btn-sm" ${act('addArrItem', svc, idx, EL)}>${th('arr.add_to', {svc})}</button>`;
  const isMusic = svc === 'lidarr';
  const poster = `<div class="glow-interactive${isMusic?' is-music':''}">${_posterImg(m.poster, 174, isMusic?174:261, isMusic)}</div>`;
  return `
    <div class="card mb-12">
      <div class="poster-card">
        ${poster}
        <div class="grow">
          <div class="panel-item-head">
            <div class="fs-16 panel-item-title">${esc(m.title)}${m.year ? `<span class="t3 fs-12">(${m.year})</span>` : ''}${m.status ? `<span class="badge muted badge-sm">${esc(m.status)}</span>` : ''}</div>
            <div class="panel-actions">${action}</div>
          </div>
          ${overview ? `<div class="note-text t2 mt-8">${overview}</div>` : ''}
        </div>
      </div>
    </div>
  `;
}

export async function addArrItem(svc, idx, btn) {
  const st = _addNewState[svc] || {};
  const m = (st.results || [])[idx];
  if (!m) return;
  btnBusy(btn, t('common.adding'));
  try {
    await api('/api/library/add', 'POST', {
      service: svc, title: m.title, year: m.year,
      tvdb_id: m.tvdb_id, tmdb_id: m.tmdb_id, mbid: m.mbid,
    });
    btnDone(btn, t('arr.added_searching'), {keepDisabled: true});
    toast(t('arr.added_toast', {title: m.title, svc}), 'success');
    m.already_added = true;   // so a second click cannot add it twice
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}

export function goToLibrarySettings(svc) {
  // Settings, Arr apps pane (data-pane "library").
  showView('settings', null, {pane: 'library', focus: true});
}

// ── Pass 16g: Spotify Backlog (top spotify-only artists → Lidarr) ────────
//
// Lists artists you stream on Spotify but don't own in your Plex library,
// with one-click "Add to Lidarr" using the pre-resolved MBID from
// Phase 1.4 of the music pipeline.
//
// "only_resolved" hides artists where the MBID lookup is still pending
// — useful once Phase 1.4 has caught up so every visible card is
// instantly addable. With a fresh import you typically leave it OFF
// to see the full backlog.

// Moved _backlogState to state

export async function renderSpotifyBacklog(svc) {
  const contentEl = document.getElementById(`arr-content-${svc}`);
  contentEl.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('arr.loading_backlog')}</p>`;

  // Pass 30: not_added_only is the second independent filter — works
  // in combination with only_resolved.
  const st = state._backlogState[svc] || {
    only_resolved: false,
    not_added_only: false,
    limit: 100,
  };
  state._backlogState[svc] = st;

  let data;
  try {
    const params = new URLSearchParams({ limit: String(st.limit) });
    if (st.only_resolved)  params.set('only_resolved',  'true');
    if (st.not_added_only) params.set('not_added_only', 'true');
    data = await api(`/api/library/spotify-backlog?${params.toString()}`);
  } catch (e) {
    contentEl.innerHTML = _errHtml(e, act('renderSpotifyBacklog', svc));
    return;
  }
  st.data = data;

  const stats = data.stats || {};
  const total    = stats.total_unique_artists  || 0;
  const resolved = stats.mbid_resolved_artists || 0;
  const pending  = stats.mbid_pending_artists  || 0;
  const pct = total > 0 ? Math.round((resolved / total) * 100) : 0;
  // Pass 16o: stats also tell us whether the Lidarr cache cross-check ran.
  const inLidarrVisible = stats.in_lidarr_visible || 0;
  const lidarrCacheLoaded = !!stats.lidarr_cache_loaded;
  // Without Lidarr the music server's index is the library, and a wish is
  // what the button makes - sent on to SoulSync when it is set up.
  const viaPlex = _arrMusicSource === 'plex';
  const libChecked = viaPlex ? !!stats.library_checked : lidarrCacheLoaded;
  const libName = viaPlex ? t('arr.the_library') : 'Lidarr';
  const hint = !viaPlex
    ? th('arr.backlog_hint_lidarr').replace('{tag}', '<code>curatarr</code>')
    : data.soulsync_ready && state.currentUser?.is_admin
      ? th('arr.backlog_hint_soulsync')
      : th('arr.backlog_hint_wanted');

  const header = `
    <section class="section">
      <div class="section-head"><h3>${th('arr.tab.backlog')}</h3>
        <span class="section-hint">${hint}</span>
        <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('renderSpotifyBacklog', svc)}>${th('arr.refresh')}</button></div>
      </div>
      <div class="section-body">
        <div class="gap-18 row fs-12">
          <span><b>${total}</b> <span class="t3">${th('arr.unique_artists', {n: total})}</span></span>
          <span><b class="t-success">${resolved}</b> <span class="t3">${th('arr.mbid_resolved', {pct})}</span></span>
          <span><b class="t-amber">${pending}</b> <span class="t3">${th('arr.pending_lookup')}</span></span>
          ${libChecked ? `<span><b class="t-success">${inLidarrVisible}</b> <span class="t3">${th('arr.already_in_page', {lib: libName})}</span></span>` : ''}
          <div class="row row-end">
            <label class="chip${st.only_resolved ? ' active' : ''}" title="${th('arr.resolved_only_title')}">
              <input type="checkbox" ${st.only_resolved ? 'checked' : ''} ${actOn('change', 'onBacklogOnlyResolved', svc, EL)}> ${th('arr.resolved_only')}</label>
            <label class="chip${st.not_added_only ? ' active' : ''}${libChecked ? '' : ' dim'}"
                   title="${libChecked ? th('arr.not_added_title', {lib: libName}) : th(viaPlex ? 'arr.index_unreadable_filter' : 'arr.lidarr_cache_filter')}">
              <input type="checkbox" ${st.not_added_only ? 'checked' : ''} ${actOn('change', 'onBacklogNotAddedOnly', svc, EL)} ${libChecked ? '' : 'disabled'}> ${th('arr.not_in_lib_yet', {lib: libName})}</label>
          </div>
        </div>
        ${pending > 0 && !st.only_resolved ? `<p class="italic fs-11 t3 mt-8">${th('arr.pending_note')}</p>` : ''}
        ${libChecked ? '' : viaPlex
          ? `<p class="italic fs-11 t-amber mt-8">${th('arr.index_unreadable')}</p>`
          : `<p class="italic fs-11 t-amber mt-8" title="${th('arr.lidarr_cache_title')}">${th('arr.lidarr_cache_note')}</p>`}
      </div>
    </section>
  `;

  const artists = data.artists || [];
  if (artists.length === 0) {
    const reason = st.not_added_only
      ? th('arr.empty_not_added', {lib: libName})
      : st.only_resolved
        ? th('arr.empty_resolved')
        : th('arr.empty_backlog');
    contentEl.innerHTML = header + emptyHtml(reason);
    return;
  }

  contentEl.innerHTML = header + artists.map((a, idx) => renderBacklogCard(svc, a, idx)).join('');
}

// An artist the owner deleted can still be streamed on Spotify; adding it
// again is allowed, but never by accident.
// Drawn in the UI language, and again when it changes.
const _deletedBadge = () => `<span class="badge amber badge-sm" title="${th('arr.deleted_title')}">${th('arr.deleted')}</span>`;
let DELETED_BADGE = _deletedBadge();
globalThis.addEventListener?.('i18n:changed', () => { DELETED_BADGE = _deletedBadge(); });

export function renderBacklogCard(svc, a, idx) {
  const tracks = (a.top_tracks || []).slice(0, 3);
  const tracksHTML = tracks.length
    ? `<div class="mt-4 panel-item-sub"><span class="t2">${th('arr.top_tracks')}</span> ${tracks.map(tr => `<span class="mr-10">${esc(tr.title)} <span class="t3">×${tr.plays}</span></span>`).join('')}</div>`
    : '';
  const mbidBadge = a.mbid_resolved
    ? `<span class="badge success badge-sm" title="${th('arr.mbid_title')}">MBID</span>`
    : `<span class="badge amber badge-sm" title="${th('arr.pending_title')}">${th('arr.pending')}</span>`;

  // Pass 16o: cross-check against the cached Lidarr library so we don't
  // surface a clickable "Add" button for an artist already in Lidarr.
  // Falls back to the previous behaviour if the cache hasn't loaded yet.
  let addBtn;
  const viaPlex = _arrMusicSource === 'plex';
  if (a.in_library || a.in_lidarr) {
    addBtn = viaPlex
      ? `<span class="badge success badge-sm" title="${th('arr.in_server_library_title', {server: serverLabel()})}">${th('arr.in_library')}</span>`
      : `<span class="badge success badge-sm" title="${th('arr.in_lidarr_title')}">${th('arr.in_lidarr')}</span>`;
  } else if (viaPlex) {
    addBtn = `<button type="button" class="btn btn-primary btn-sm" ${act('wishArtist', svc, idx, EL)}>${th('arr.wish')}</button>`;
  } else if (a.mbid_resolved) {
    addBtn = `<button type="button" class="btn btn-primary btn-sm" ${act('addBacklogArtist', svc, idx, EL)}>${th('arr.add_to', {svc: 'Lidarr'})}</button>`;
  } else {
    addBtn = `<button type="button" class="btn btn-secondary btn-sm" disabled title="${th('arr.mbid_not_resolved')}">${th('arr.mbid_pending')}</button>`;
  }

  return `
    <div class="panel-item">
      <div class="panel-item-head">
        <div class="fs-14 panel-item-title">${esc(a.artist_name || t('arr.unknown'))}<span class="fs-11 t3">${th('arr.plays', {n: a.play_count})}</span>${mbidBadge}${a.deleted ? DELETED_BADGE : ''}</div>
        <div class="panel-actions">${addBtn}</div>
      </div>
      ${tracksHTML}
    </div>
  `;
}

export function setBacklogOnlyResolved(svc, value) {
  const st = state._backlogState[svc] || {};
  st.only_resolved = !!value;
  state._backlogState[svc] = st;
  renderSpotifyBacklog(svc);
}

// Pass 30: second independent filter — hide artists already in Lidarr.
// Combines with only_resolved. Both can be on at once.
export function setBacklogNotAddedOnly(svc, value) {
  const st = state._backlogState[svc] || {};
  st.not_added_only = !!value;
  state._backlogState[svc] = st;
  renderSpotifyBacklog(svc);
}

export async function addBacklogArtist(svc, idx, btn) {
  const st = state._backlogState[svc] || {};
  const a = ((st.data || {}).artists || [])[idx];
  if (!a || !a.mbid) return;
  btnBusy(btn, t('common.adding'));
  try {
    await api('/api/library/add', 'POST', { service: 'lidarr', title: a.artist_name, mbid: a.mbid });
    btnDone(btn, t('common.added'), {keepDisabled: true});
    toast(t('arr.added_to_lidarr', {title: a.artist_name}), 'success');
    // No auto-refresh — the user may want to chain several adds in one go.
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}

// Synopsis-browser controls: the inline handlers read this.value / this.checked;
// the dispatcher hands over the element instead, so these small readers do it.
export function onBrowserSort(svc, tabId, el) { setBrowserSort(svc, tabId, el.value); }
export function onBrowserFilter(svc, tabId, el) { setBrowserFilter(svc, tabId, el.checked); }
export function onBrowserSearch(svc, tabId, el) { setBrowserSearch(svc, tabId, el.value); }
// Spotify-backlog filters, same reason.
export function onBacklogOnlyResolved(svc, el) { setBacklogOnlyResolved(svc, el.checked); }
export function onBacklogNotAddedOnly(svc, el) { setBacklogNotAddedOnly(svc, el.checked); }


// ── Wanted (Lidarr optional): wishes the owner fulfils in SoulSync ──────────
export async function wishArtist(svc, idx, btn) {
  const st = state._backlogState[svc] || {};
  const a = ((st.data || {}).artists || [])[idx];
  if (!a) return;
  btnBusy(btn, t('arr.wishing'));
  try {
    const w = await api('/api/library/wish', 'POST', { title: a.artist_name, mbid: a.mbid || null, source: 'backlog' });
    if (w.in_library) { btnDone(btn, t('arr.in_library_btn'), {keepDisabled: true}); return; }
    // The owner's wish goes straight on to SoulSync's wishlist (2026-11-01).
    if (st.data?.soulsync_ready && state.currentUser?.is_admin) {
      btnBusy(btn, t('arr.sending'));
      const r = await sendWishToSoulSync(w.id, a.artist_name);
      btnDone(btn, r?.ok ? t('arr.on_soulsync') : t('arr.wished'), {keepDisabled: true});
      return;
    }
    btnDone(btn, t('arr.wished'), {keepDisabled: true});
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}

// One wish onto SoulSync's wishlist: the tracks the wisher streamed by that
// artist (routers/library.send_wish_to_soulsync). Says what SoulSync did.
async function sendWishToSoulSync(id, title) {
  try {
    const r = await api(`/api/library/wish/${encodeURIComponent(id)}/soulsync`, 'POST');
    if (r.ok) {
      const parts = [t('arr.ss_new', {n: r.added}), r.already ? t('arr.ss_already', {n: r.already}) : '',
                     r.refused ? t('arr.ss_refused', {n: r.refused}) : ''].filter(Boolean);
      toast(t('arr.ss_on_wishlist', {title, list: parts.join(', ')}), r.added || r.already ? 'success' : 'amber');
    } else {
      toast(t('arr.ss_not_sent', {title, error: r.error || t('arr.ss_nothing_sent')}), 'amber', {ms: 8000});
    }
    return r;
  } catch (e) {
    toast(t('arr.ss_not_sent', {title, error: _errMsg(e)}), 'amber', {ms: 8000});
    return null;
  }
}

export async function sendWantedToSoulSync(id, btn) {
  const title = btn?.closest('.panel-item')?.dataset.title || t('arr.the_wish');
  btnBusy(btn, t('arr.sending'));
  await sendWishToSoulSync(id, title);
  renderWanted('lidarr');
}

function _soulsyncBadge(s) {
  if (!s) return '';
  const when = s.at ? _fmtRel(s.at) : '';
  const tip = t('arr.ss_tip', {when, added: s.added || 0, already: s.already || 0, refused: s.refused || 0});
  return `<span class="badge muted badge-sm" title="${escAttr(tip)}">${th('arr.ss_tracks', {n: (s.added || 0) + (s.already || 0)})}</span>`;
}

export async function renderWanted(svc) {
  const contentEl = document.getElementById(`arr-content-${svc}`);
  contentEl.innerHTML = skeletonHtml(t('common.loading'), {kind: 'rows'});
  let data;
  try {
    data = await api('/api/library/wishes');
  } catch (e) {
    contentEl.innerHTML = _errHtml(e, act('renderWanted', svc));
    return;
  }
  const rows = data.wishes || [];
  const canSend = !!data.soulsync_ready && !!state.currentUser?.is_admin;
  const body = rows.length ? rows.map(w => `
    <div class="panel-item" data-title="${escAttr(w.title)}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(w.title)} ${w.in_library ? `<span class="badge success badge-sm" title="${th('arr.has_artist_now', {server: serverLabel()})}">${th('arr.in_library')}</span>` : ''}${w.deleted ? DELETED_BADGE : ''}<span class="badge muted badge-sm">${esc(w.source)}</span>${w.mbid ? `<span class="badge muted badge-sm" title="${th('arr.mbid_known')}">MBID</span>` : ''}${_soulsyncBadge(w.soulsync)}</div>
        <div class="panel-actions">${canSend && !w.in_library ? `<button type="button" class="btn btn-secondary btn-sm" ${act('sendWantedToSoulSync', w.id, EL)} title="${th('arr.send_title')}">${th(w.soulsync ? 'arr.send_again' : 'arr.send_to_soulsync')}</button>` : ''}${w.can_remove ? `<button type="button" class="btn btn-secondary btn-sm" ${act('removeWish', w.id, EL)}>${th(w.own ? 'arr.take_back' : 'arr.remove')}</button>` : ''}</div>
      </div>
      <div class="panel-item-meta">${th('arr.wished_by', {who: w.own ? t('arr.you') : (w.wished_by || t('arr.someone'))})} ${w.created_at ? _fmtRel(w.created_at) : ''}${w.note ? ` · ${esc(w.note)}` : ''}</div>
    </div>`).join('')
    : emptyHtml(th('arr.wanted_empty'));
  contentEl.innerHTML = `<section class="section">
    <div class="section-head">
      <h3>${th('arr.tab.wanted')} <span class="badge muted badge-sm">${rows.length}</span></h3>
      <span class="section-hint">${th('arr.wanted_hint_pre')} ${th(canSend ? 'arr.wanted_hint_send' : 'arr.wanted_hint_owner')}${th('arr.wanted_hint_post', {server: serverLabel()})}</span>
      <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('renderWanted', svc)}>${th('arr.refresh')}</button></div>
    </div>
    <div class="section-body">${body}</div></section>`;
}

export async function removeWish(id, btn) {
  btnBusy(btn, t('arr.removing'));
  try {
    await api(`/api/library/wish/${encodeURIComponent(id)}`, 'DELETE');
    btn.closest('.panel-item')?.remove();
    toast(t('arr.wish_removed'), 'info');
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}
