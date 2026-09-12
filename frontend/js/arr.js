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
import { _errHtml, _errMsg, _posterImg, btnBusy, btnDone, emptyHtml, esc, escAttr, menuHtml, setStatus, toast } from './ui.js';
import { state } from './state.js';
import { showView } from './nav.js';
import { openSettingsPane } from './settings.js';
const ARR_TABS = {
  sonarr: [
    { id: 'all',    label: 'All Series' },
    { id: 'tv',     label: 'TV' },
    { id: 'anime',  label: 'Anime' },
    { id: 'curatarr', label: 'Curatarr-Added' },
    { id: 'add',    label: '+ Add New' },
  ],
  radarr: [
    { id: 'all',    label: 'All Movies' },
    { id: 'curatarr', label: 'Curatarr-Added' },
    { id: 'add',    label: '+ Add New' },
  ],
  lidarr: [
    { id: 'all',    label: 'All Artists' },
    { id: 'curatarr', label: 'Curatarr-Added' },
    { id: 'backlog', label: 'Spotify Backlog' },
    { id: 'add',    label: '+ Add New' },
  ],
};

// Active tab per arr — survives tab switches but resets on page reload.
const _arrActiveTab = { sonarr: 'all', radarr: 'all', lidarr: 'all' };

export async function loadArrPage(svc) {
  const tabsEl = document.getElementById(`arr-tabs-${svc}`);
  const contentEl = document.getElementById(`arr-content-${svc}`);
  contentEl.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  tabsEl.innerHTML = '';

  let status;
  try {
    status = await api('/api/library/status');
  } catch (e) {
    contentEl.innerHTML = _errHtml(e, `loadArrPage('${svc}')`);
    return;
  }

  const info = (status || {})[svc] || {};
  if (!info.configured) {
    // Setup banner — admins get a CTA, non-admins get a "ask your admin"
    // message. The Settings → Library pane is admin-only, so showing the
    // Configure button to a non-admin would lead them to a hidden tab
    // (Pass 16l).
    const isAdmin = !!(state.currentUser && state.currentUser.is_admin);
    const titleSvc = svc.charAt(0).toUpperCase() + svc.slice(1);
    contentEl.innerHTML = isAdmin
      ? `
        <div class="arr-setup-banner">
          <h3>${esc(titleSvc)} not configured</h3>
          <p>Add the URL + API key in Settings → Library to start using this view.</p>
          <button class="btn btn-primary" onclick="goToLibrarySettings('${svc}')">Configure ${svc}</button>
        </div>
      `
      : `
        <div class="arr-setup-banner">
          <h3>${esc(titleSvc)} not configured</h3>
          <p style="color:var(--text2)">This integration hasn't been set up yet. Ask your admin to add ${esc(svc)} in <em>Settings → Library</em>.</p>
        </div>
      `;
    return;
  }

  // Render tabs
  const tabs = ARR_TABS[svc] || [];
  tabsEl.innerHTML = tabs.map(t =>
    `<button class="arr-tab ${_arrActiveTab[svc] === t.id ? 'active' : ''}" onclick="setArrTab('${svc}','${t.id}')">${esc(t.label)}</button>`
  ).join('');
  renderArrTab(svc, _arrActiveTab[svc]);
}

export function setArrTab(svc, tabId) {
  _arrActiveTab[svc] = tabId;
  document.querySelectorAll(`#arr-tabs-${svc} .arr-tab`).forEach((b, i) => {
    b.classList.toggle('active', (ARR_TABS[svc][i] || {}).id === tabId);
  });
  renderArrTab(svc, tabId);
}

export function renderArrTab(svc, tabId) {
  // Synopsis Browser tabs (Pass 16d): all, tv, anime, curatarr
  // Add New tab (Pass 16e): add
  // Spotify Backlog tab (Pass 16g, lidarr only): backlog
  if (['all', 'tv', 'anime', 'curatarr'].includes(tabId)) {
    renderSynopsisBrowser(svc, tabId);
  } else if (tabId === 'add') {
    renderAddNew(svc);
  } else if (tabId === 'backlog' && svc === 'lidarr') {
    renderSpotifyBacklog(svc);
  } else {
    const tabLabel = (ARR_TABS[svc].find(t => t.id === tabId) || {}).label || tabId;
    const contentEl = document.getElementById(`arr-content-${svc}`);
    contentEl.innerHTML = `
      <div style="padding:24px;text-align:center;color:var(--text3);font-size:13px">
        <p style="margin:0 0 6px"><strong>${esc(tabLabel)}</strong></p>
        <p style="margin:0;font-size:12px">Coming in upcoming pass — skeleton only.</p>
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
  contentEl.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';

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
    data = await api(`/api/library/items/${svc}?${params.toString()}`);
  } catch (e) {
    contentEl.innerHTML = `${_errHtml(e)}
      <p class="fs-12 t3 mt-8">First-time fetch needs ${esc(svc)} to be reachable. If you've already opened this view once before, retry — cached data is served while the arr recovers.</p>
      <div class="mt-8"><button type="button" class="btn btn-secondary btn-sm" onclick="renderSynopsisBrowser('${svc}','${tabId}',{forceRefresh:true})">Retry</button></div>`;
    return;
  }

  // Pass 16i: cache badge — 'live' (just fetched), 'cache' (Xm old), 'stale' (arr down)
  const ci = data.cache || {};
  const ageMin = Math.floor((ci.age_s || 0) / 60);
  const ageStr = ageMin === 0 ? 'just now' : `${ageMin}m ago`;
  const cacheBadge =
      ci.source === 'live'
    ? `<span class="badge muted badge-sm" title="Fetched from ${esc(svc)} just now">live</span>`
    : ci.source === 'cache'
    ? `<span class="badge muted badge-sm" title="Served from the 15-min server cache (${ageStr})">cached ${ageStr}</span>`
    : ci.source === 'stale'
    ? `<span class="badge amber badge-sm" title="${esc(svc)} unreachable: ${esc(ci.stale_error || 'connection failed')} — showing cache from ${ageStr}">stale ${ageStr}</span>`
    : '';

  // Toolbar: the count left, the view controls right.
  const toolbar = `
    <div class="toolbar">
      <span class="fs-12 t2"><b>${data.total}</b> items</span>
      ${cacheBadge}
      <div class="toolbar-right">
        <label for="arr-sort-${svc}-${tabId}" class="fs-12 t3">Sort</label>
        <select id="arr-sort-${svc}-${tabId}" class="input" onchange="setBrowserSort('${svc}','${tabId}', this.value)">
          <option value="size_desc"            ${st.sort === 'size_desc' ? 'selected' : ''}>Size ↓ (biggest first)</option>
          <option value="size_asc"             ${st.sort === 'size_asc' ? 'selected' : ''}>Size ↑ (smallest first)</option>
          <option value="added_desc"           ${st.sort === 'added_desc' ? 'selected' : ''}>Recently added</option>
          <option value="added_asc"            ${st.sort === 'added_asc' ? 'selected' : ''}>Oldest added</option>
          <option value="synopsis_updated_asc" ${st.sort === 'synopsis_updated_asc' ? 'selected' : ''}>Synopsis: oldest first</option>
          <option value="title_asc"            ${st.sort === 'title_asc' ? 'selected' : ''}>Title A→Z</option>
        </select>
        <label class="chip${st.needs_enrichment ? ' active' : ''}" title="Only items without a synopsis or with raw metadata still awaiting the polish">
          <input type="checkbox" ${st.needs_enrichment ? 'checked' : ''} onchange="setBrowserFilter('${svc}','${tabId}', this.checked)"> Needs enrichment</label>
        <input type="text" id="arr-search-${svc}-${tabId}" class="input" aria-label="Search items" value="${escAttr(st.search || '')}"
               placeholder="Search title…" oninput="setBrowserSearch('${svc}','${tabId}', this.value)" style="width:200px">
        <button type="button" class="btn btn-secondary btn-sm" onclick="renderSynopsisBrowser('${svc}','${tabId}',{forceRefresh:true})" title="Bypass the 15-min cache and re-fetch from ${esc(svc)}">Refresh</button>
      </div>
    </div>
  `;

  if (!data.items || data.items.length === 0) {
    contentEl.innerHTML = toolbar + emptyHtml('No items match.');
    return;
  }

  // Item rows
  const rows = data.items.map(it => renderArrItemRow(svc, it)).join('');
  contentEl.innerHTML = toolbar + `<div class="arr-item-list">${rows}</div>`;
}

export function renderArrItemRow(svc, item) {
  const sizeMB = (item.size_on_disk || 0) / (1024 * 1024);
  const sizeStr = sizeMB > 1024
    ? `${(sizeMB / 1024).toFixed(1)} GB`
    : sizeMB > 0
      ? `${sizeMB.toFixed(0)} MB`
      : '—';
  const addedStr = item.added
    ? new Date(item.added).toLocaleDateString()
    : '—';
  const enrichedBadge = item.has_enrichment
    ? ((item.enrichment_source || '').startsWith('raw')
        ? '<span class="badge amber badge-sm">raw metadata · awaiting polish</span>'
        : `<span class="badge success badge-sm">enriched · ${esc(item.enrichment_source || 'cache')}</span>`)
    : '<span class="badge amber badge-sm">no synopsis</span>';

  // One row, one Re-enrich menu (metadata / summary / both) at the row end.
  return `
    <div class="panel-item">
      <div class="panel-item-head">
        <div class="panel-item-title" style="font-size:14px">${esc(item.title || '(untitled)')}${item.year ? `<span class="t3 fs-12">(${item.year})</span>` : ''}${enrichedBadge}</div>
        <div class="panel-actions">${menuHtml([
          {label: 'Metadata — re-fetch from TMDB / AniList / MusicBrainz', call: `reEnrich('${svc}',${item.id},'metadata')`},
          {label: 'Summary — re-run the LLM on the existing data', call: `reEnrich('${svc}',${item.id},'summary')`},
          {label: 'Both', call: `reEnrich('${svc}',${item.id},'both')`},
        ], 'Re-enrich')}</div>
      </div>
      <div class="panel-item-meta mt-4">${sizeStr} · added ${addedStr}${item.root_folder ? ` · <code class="fs-10">${esc(item.root_folder)}</code>` : ''}${item.status ? ` · ${esc(item.status)}` : ''}</div>
      ${item.synopsis
        ? `<div class="panel-item-sub">${esc(item.synopsis)}</div>`
        : '<div class="panel-item-sub" style="font-style:italic">No synopsis on file — Re-enrich fetches one.</div>'}
    </div>
  `;
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
  const what = {metadata: 'metadata refresh', summary: 'summary rewrite', both: 'metadata + summary refresh'}[mode] || mode;
  try {
    await api('/api/library/reenrich', 'POST', { service: svc, arr_id: arrId, mode });
    toast(`Queued: ${what} — progress shows in Activity`, 'success');
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
  const placeholder = svc === 'lidarr' ? 'Search for an artist…'
    : svc === 'radarr' ? 'Search for a movie title…'
    : 'Search for a TV show…';
  contentEl.innerHTML = `
    <div class="toolbar">
      <input type="text" id="add-search-${svc}" class="input grow" aria-label="Search title" placeholder="${esc(placeholder)}" oninput="debouncedAddSearch('${svc}')" style="padding:9px 12px;font-size:13px">
      <span id="add-search-status-${svc}" class="status" style="min-width:80px"></span>
    </div>
    <div id="add-search-results-${svc}"></div>
    <p class="fs-11 t3 mt-8">
      Live search via the arr's lookup (TVDB / TMDB / MusicBrainz).
      Defaults from <button type="button" class="link-num t-amber" onclick="goToLibrarySettings('${svc}')">Settings → Library</button> are applied automatically.
      Items added here are tagged <code>curatarr</code> in your arr.
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
    stat.textContent = q.length ? 'min 2 chars' : '';
    return;
  }

  // Cancel any in-flight request for this service
  const st = _addNewState[svc];
  if (st.controller) {
    try { st.controller.abort(); } catch {}
  }
  st.controller = new AbortController();

  setStatus(stat, 'searching…', 'busy');
  try {
    const headers = {};
    if (state.token) headers['Authorization'] = `Bearer ${state.token}`;
    const r = await fetch(`/api/library/search/${svc}?q=${encodeURIComponent(q)}`, {
      headers, signal: st.controller.signal,
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({detail: r.statusText}));
      setStatus(stat, err.detail || 'failed', 'err');
      out.innerHTML = '';
      return;
    }
    const data = await r.json();
    st.results = data.matches || [];
    setStatus(stat, `${data.matches.length} hits`, null);
    if (data.matches.length === 0) {
      out.innerHTML = emptyHtml('No matches.');
      return;
    }
    out.innerHTML = data.matches.map((m, idx) => renderAddCard(svc, m, idx)).join('');
  } catch (e) {
    if (e.name === 'AbortError') return;   // newer request superseded us
    setStatus(stat, e.message || 'failed', 'err');
  }
}

export function renderAddCard(svc, m, idx) {
  const overview = m.overview ? esc(m.overview) : '';
  const action = m.already_added
    ? '<span class="badge muted badge-sm">already in your library</span>'
    : `<button type="button" class="btn btn-primary btn-sm" onclick="addArrItem('${svc}',${idx},this)">+ Add to ${svc}</button>`;
  const isMusic = svc === 'lidarr';
  const poster = `<div class="glow-interactive${isMusic?' is-music':''}">${_posterImg(m.poster, 174, isMusic?174:261, isMusic)}</div>`;
  return `
    <div class="card mb-12">
      <div class="poster-card">
        ${poster}
        <div class="grow">
          <div class="panel-item-head">
            <div class="panel-item-title" style="font-size:16px">${esc(m.title)}${m.year ? `<span class="t3 fs-12">(${m.year})</span>` : ''}${m.status ? `<span class="badge muted badge-sm">${esc(m.status)}</span>` : ''}</div>
            <div class="panel-actions">${action}</div>
          </div>
          ${overview ? `<div class="t2 mt-8" style="font-size:12.5px;line-height:1.55">${overview}</div>` : ''}
        </div>
      </div>
    </div>
  `;
}

export async function addArrItem(svc, idx, btn) {
  const st = _addNewState[svc] || {};
  const m = (st.results || [])[idx];
  if (!m) return;
  btnBusy(btn, 'Adding…');
  try {
    await api('/api/library/add', 'POST', {
      service: svc, title: m.title, year: m.year,
      tvdb_id: m.tvdb_id, tmdb_id: m.tmdb_id, mbid: m.mbid,
    });
    btnDone(btn, 'Added — searching', {keepDisabled: true});
    toast(`Added "${m.title}" to ${svc} — it searches for a release now`, 'success');
    m.already_added = true;   // so a second click cannot add it twice
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}

export function goToLibrarySettings(svc) {
  // Open Settings view + Library pane + scroll to the right card.
  showView('settings', document.querySelector('.sb-item[onclick*=settings]'));
  setTimeout(() => {
    openSettingsPane('library', document.querySelector('.settings-tab[data-pane="library"]'));
  }, 50);
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
  contentEl.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading backlog…</p>';

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
    contentEl.innerHTML = _errHtml(e, `renderSpotifyBacklog('${svc}')`);
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

  const header = `
    <section class="section">
      <div class="section-head"><h3>Spotify backlog</h3>
        <span class="section-hint">artists you stream on Spotify but don't own locally — one click adds them to Lidarr with your saved defaults and the <code>curatarr</code> tag</span>
        <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="renderSpotifyBacklog('${svc}')">Refresh</button></div>
      </div>
      <div class="section-body">
        <div class="row fs-12" style="gap:18px">
          <span><b>${total}</b> <span class="t3">unique artists</span></span>
          <span><b class="t-success">${resolved}</b> <span class="t3">MBID resolved (${pct}%)</span></span>
          <span><b class="t-amber">${pending}</b> <span class="t3">pending lookup</span></span>
          ${lidarrCacheLoaded ? `<span><b class="t-success">${inLidarrVisible}</b> <span class="t3">already in Lidarr (this page)</span></span>` : ''}
          <div class="row row-end">
            <label class="chip${st.only_resolved ? ' active' : ''}" title="Only artists whose MusicBrainz ID has been resolved by Phase 1.4 of the music pipeline — the ones with a clickable Add button.">
              <input type="checkbox" ${st.only_resolved ? 'checked' : ''} onchange="setBacklogOnlyResolved('${svc}', this.checked)"> Resolved only</label>
            <label class="chip${st.not_added_only ? ' active' : ''}" style="${lidarrCacheLoaded ? '' : 'opacity:.5'}"
                   title="${lidarrCacheLoaded ? 'Hide artists already in your Lidarr library. Combines with the resolved filter so you see exactly the queue you have not added yet.' : 'Lidarr cache not loaded — this filter is inactive until the Lidarr tab is opened once.'}">
              <input type="checkbox" ${st.not_added_only ? 'checked' : ''} onchange="setBacklogNotAddedOnly('${svc}', this.checked)" ${lidarrCacheLoaded ? '' : 'disabled'}> Not in Lidarr yet</label>
          </div>
        </div>
        ${pending > 0 && !st.only_resolved ? '<p class="fs-11 t3 mt-8" style="font-style:italic">Pending artists are still being looked up by Phase 1.4 of the music pipeline; they become addable once their MusicBrainz ID is resolved.</p>' : ''}
        ${!lidarrCacheLoaded ? '<p class="fs-11 t-amber mt-8" style="font-style:italic" title="The Lidarr library cache has not been loaded yet — the already-in-Lidarr badges appear after the next refresh.">Lidarr cache not loaded yet — duplicates are not flagged. Open the Lidarr tab once or wait for the next 30-min refresh.</p>' : ''}
      </div>
    </section>
  `;

  const artists = data.artists || [];
  if (artists.length === 0) {
    const reason = st.not_added_only
      ? 'No Spotify-only artists are missing from Lidarr — either every top artist is added already, or loosen a filter to see more.'
      : st.only_resolved
        ? 'No resolved artists yet — run the music pipeline, or loosen the "Resolved only" filter.'
        : 'No Spotify-only plays found — import your Spotify history first (Users → Spotify history import).';
    contentEl.innerHTML = header + emptyHtml(reason);
    return;
  }

  contentEl.innerHTML = header + artists.map((a, idx) => renderBacklogCard(svc, a, idx)).join('');
}

export function renderBacklogCard(svc, a, idx) {
  const tracks = (a.top_tracks || []).slice(0, 3);
  const tracksHTML = tracks.length
    ? `<div class="panel-item-sub" style="margin-top:4px"><span class="t2">Top tracks:</span> ${tracks.map(t => `<span style="margin-right:10px">${esc(t.title)} <span class="t3">×${t.plays}</span></span>`).join('')}</div>`
    : '';
  const mbidBadge = a.mbid_resolved
    ? '<span class="badge success badge-sm" title="MusicBrainz ID resolved by Phase 1.4">MBID</span>'
    : '<span class="badge amber badge-sm" title="Pending — Phase 1.4 has not resolved this artist yet">pending</span>';

  // Pass 16o: cross-check against the cached Lidarr library so we don't
  // surface a clickable "Add" button for an artist already in Lidarr.
  // Falls back to the previous behaviour if the cache hasn't loaded yet.
  let addBtn;
  if (a.in_lidarr) {
    addBtn = '<span class="badge success badge-sm" title="Already in your Lidarr library">in Lidarr</span>';
  } else if (a.mbid_resolved) {
    addBtn = `<button type="button" class="btn btn-primary btn-sm" onclick="addBacklogArtist('${svc}',${idx},this)">+ Add to Lidarr</button>`;
  } else {
    addBtn = '<button type="button" class="btn btn-secondary btn-sm" disabled title="MusicBrainz ID not resolved yet">MBID pending</button>';
  }

  return `
    <div class="panel-item">
      <div class="panel-item-head">
        <div class="panel-item-title" style="font-size:14px">${esc(a.artist_name || '(unknown)')}<span class="fs-11 t3">${a.play_count} plays</span>${mbidBadge}</div>
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
  btnBusy(btn, 'Adding…');
  try {
    await api('/api/library/add', 'POST', { service: 'lidarr', title: a.artist_name, mbid: a.mbid });
    btnDone(btn, 'Added', {keepDisabled: true});
    toast(`Added "${a.artist_name}" to Lidarr`, 'success');
    // No auto-refresh — the user may want to chain several adds in one go.
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}
