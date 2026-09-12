// ── MAPPING STATS + PROFILE BROWSER ──────────────────────────────────────────
// Two questions, two result areas — the second answer no longer overwrites the first.
import { CAT_LABELS, SVG_STAR, _errHtml, _errMsg, _fmtAbs, _fmtRel, btnBusy, btnDone, emptyHtml, esc, escAttr, menuHtml, pagerHtml, setBadge, toast } from './ui.js';
import { api } from './api.js';
import { state } from './state.js';
import { loadMusicStatus } from './music.js';
import { openMatchPicker } from './picker.js';
export async function loadMappingStats(btn) {
  const el = document.getElementById('mapping-stats');
  btnBusy(btn, 'Loading…');
  try {
    const s = await api('/api/enrichment/mapping-stats');
    el.innerHTML = `
      <div class="row mt-4" style="gap:16px">
        <span><b class="t-amber">${(s.total_entries || 0).toLocaleString()}</b> <span class="t2">total entries</span></span>
        <span><b class="t-success">${(s.tvdb_mapped || 0).toLocaleString()}</b> <span class="t2">TVDB mapped</span></span>
        <span><b>${(s.anilist_resolved || 0).toLocaleString()}</b> <span class="t2">AniList resolved</span></span>
        <span><b class="t-danger">${(s.anilist_missing || 0).toLocaleString()}</b> <span class="t2">AniList pending</span></span>
      </div>
      <div class="fs-11 t3 mt-4">Downloaded: ${s.downloaded_at ? esc(new Date(s.downloaded_at).toLocaleString()) : 'never'} · refreshes weekly</div>`;
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

export async function checkMappingCoverage(btn) {
  const el = document.getElementById('mapping-coverage');
  btnBusy(btn, 'Checking…');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Checking Sonarr coverage… this may take a moment</p>';
  try {
    const c = await api('/api/enrichment/mapping-coverage');
    if (c.error) el.innerHTML = `<p class="load-err">${esc(c.error)}</p>`;
    else el.innerHTML = `
      <div class="row mt-8" style="gap:16px">
        <span><b class="t-amber">${c.total_anime}</b> <span class="t2">anime in Sonarr</span></span>
        <span><b class="t-success">${c.covered} (${c.coverage_pct}%)</b> <span class="t2">in mapping</span></span>
        <span><b>${c.anilist_resolved}</b> <span class="t2">AniList ready</span></span>
        <span><b class="t-danger">${c.missing}</b> <span class="t2">not in mapping</span></span>
      </div>
      ${c.missing_sample?.length ? `
      <details class="mt-8">
        <summary class="fs-11 t3" style="cursor:pointer">Not mapped (${c.missing} total)</summary>
        <div class="mt-4">${c.missing_sample.map(m => `<div class="fs-11 t-danger">${esc(m.title)} tvdb:${m.tvdb_id || '?'} — ${esc(m.reason)}</div>`).join('')}</div>
      </details>` : ''}`;
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

const PROFILE_PAGE = 8;
export async function loadProfiles(offset = 0) {
  const cat    = document.getElementById('profile-cat')?.value    || 'movie';
  const src    = document.getElementById('profile-source')?.value || 'all';
  const search = document.getElementById('profile-search')?.value || '';
  const el = document.getElementById('profile-browser');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading profiles…</p>';
  try {
    const r = await api(`/api/enrichment/profiles?category=${cat}&source=${src}&limit=${PROFILE_PAGE}&offset=${offset}&search=${encodeURIComponent(search)}`);
    if (!r.profiles?.length) {
      el.innerHTML = emptyHtml('No enriched profiles found — run an enrichment first.');
      return;
    }
    const srcLabel = p => p.profile_source === 'watch_history' ? 'watched'
      : (p.profile_source || '').startsWith('arr:') ? p.profile_source.slice(4) : (p.profile_source || '?');
    const block = (label, html, mono) => html
      ? `<div class="mb-8"><div class="fs-10 t3" style="text-transform:uppercase;letter-spacing:.6px">${label}</div><div class="${mono ? 'fs-11 t3 mono' : 'fs-12 t2'} mt-4" style="line-height:1.6;word-break:break-word">${html}</div></div>` : '';
    el.innerHTML = `
      <div class="fs-12 t3 mb-8">${r.total.toLocaleString()} enriched · showing ${offset + 1}–${Math.min(offset + PROFILE_PAGE, r.total)}</div>
      ${r.profiles.map(p => `
        <details class="panel-item">
          <summary class="panel-item-head">
            <div class="grow">
              <div class="panel-item-title">${esc(p.title)}
                <span class="badge ${p.has_profile ? 'success' : 'muted'} badge-sm">${p.has_profile ? 'profile' : 'no profile'}</span>
                <span class="badge ${p.source?.includes('+llm') ? 'success' : p.source?.includes('rule_based') ? 'amber' : 'muted'} badge-sm">${esc(p.source || '?')}</span>
                <span class="badge muted badge-sm">${esc(srcLabel(p))}</span>
                ${p.rating ? `<span class="fs-11 t-amber">${SVG_STAR} ${p.rating}</span>` : ''}
                <span class="fs-11 t3">${p.enriched_at ? new Date(p.enriched_at).toLocaleDateString() : '?'}</span>
              </div>
              ${p.genres?.length ? `<div class="row mt-4" style="gap:4px">${p.genres.slice(0, 5).map(g => `<span class="badge muted badge-sm">${esc(g)}</span>`).join('')}</div>` : ''}
            </div>
            <span class="fs-11 t3">details</span>
          </summary>
          <div class="panel-item-foot" style="border-top:1px solid var(--border);padding-top:10px">
            ${block('Themes', p.themes?.length ? esc(p.themes.join(' · ')) : '')}
            ${block('Mood', p.mood?.length ? esc(p.mood.join(' · ')) : '')}
            ${block('Summary', p.plot_summary ? esc(p.plot_summary) : '')}
            ${block('Embedding text', p.embedding_text ? esc(p.embedding_text) : '', true)}
          </div>
        </details>`).join('')}
      ${pagerHtml({offset, limit: PROFILE_PAGE, total: r.total, call: 'loadProfiles({offset})'})}`;
  } catch (e) {
    el.innerHTML = _errHtml(e);
  }
}

// ── KNOWLEDGE BASE ────────────────────────────────────────────────────────────
// Five sub-views behind one tab row: Overview (the table every number comes
// from + the list behind any number), Needs attention, Maintenance, Music
// pipeline, Profile browser. Each loads when opened; the overview repolls
// itself while something runs. Lists reload only after an owner action
// (_kbRefresh), never on the poll — a list that re-renders under the cursor
// every 8 s is the thing this replaces.
// Moved state._kbTab to state
let _kbDrill = null;                       // {cat, states, offset} while a drilldown is open
let _kbAtt = {offset: 0, reason: null};    // the Needs-attention page + filter chip
const _kbLoaded = new Set();
export function showKbTab(name) {
  state._kbTab = name || 'overview';
  document.querySelectorAll('#kb-tabs .cat-tab').forEach(b => b.classList.toggle('active', b.dataset.kbtab === state._kbTab));
  document.querySelectorAll('#enrich-view .kb-tab').forEach(s => { s.hidden = s.dataset.kbtab !== state._kbTab; });
  if (state._kbTab === 'overview' || state._kbTab === 'maintenance') loadEnrichStatus();
  if (state._kbTab === 'attention') loadKbAttention(_kbAtt.offset, _kbAtt.reason);
  if (state._kbTab === 'music') loadMusicStatus();
  if (state._kbTab === 'profiles' && !_kbLoaded.has('profiles')) loadProfiles();
  _kbLoaded.add(state._kbTab);
}
// After an owner action: the numbers, and whichever lists are on screen.
export function _kbRefresh() {
  loadEnrichStatus();
  if (document.getElementById('kb-attention')?.dataset.loaded) loadKbAttention(_kbAtt.offset, _kbAtt.reason);
  if (_kbDrill) loadKbItems(_kbDrill.cat, _kbDrill.states, _kbDrill.offset);
}

// Fallback copy only — the live definitions arrive with the /overview payload
// (services/enrichment_state.py is the single source of truth for the states).
const KB_STATE_DEFS = {
  enriched:             'A full profile was written and its cache entry is live.',
  enriched_provisional: 'Profile from the fast pass; the slow sources are still merging in the background.',
  enriched_dead:        'The flag says enriched but the cached profile is gone — re-queued automatically.',
  rule_based:           'Heuristic profile without the LLM — retried for an upgrade.',
  awaiting_llm:         'API data cached, LLM polish paused (game mode).',
  retry_due:            'Not found so far; the waiting time is over — the next run tries again.',
  not_found:            'Every consulted source missed; waiting on the backoff (3, 6, 12, 24, then 30 days).',
  queued:               'Tracked but not processed yet.',
  processing_error:     'The pipeline crashed on this item — retried every run.',
  ignored:              'Accepted by the owner as unenriched — excluded from retries and the open count.',
  never_processed:      'Never entered the pipeline.',
};

export function _renderKbOverview(o, running, lastRun) {
  _updateKbBadge(o);
  const cats = o.categories || {};
  const served = Object.entries(o.state_definitions || {}).map(([k, v]) => [k, `${v.explainer} ${v.next_step || ''}`.trim()]);
  const defs = Object.assign({}, KB_STATE_DEFS, Object.fromEntries(served));
  const order = ['movie', 'show', 'anime', 'music'];
  // Every count is a door: click → the list behind it (same classification).
  const num = (v, cat, keys, tip, cls) => v
    ? `<button type="button" class="link-num${cls ? ' ' + cls : ''}" title="${escAttr(tip + ' — click to list')}" onclick="loadKbItems('${cat}','${keys}')">${v.toLocaleString()}</button>`
    : `<span class="t3" title="${escAttr(tip)}">0</span>`;
  const rows = order.filter(c => cats[c]).map(cat => {
    const c = cats[cat], d = c.denominator, s = c.states;
    const n = k => s[k] || 0;
    const done = n('enriched') + n('enriched_provisional');
    const pct = d.downloaded ? Math.round(100 * done / d.downloaded) : 0;
    const cell = keys => `<td class="t-right">${num(keys.reduce((a, k) => a + n(k), 0), cat, keys.join(','), keys.map(k => defs[k] || k).join(' · '))}</td>`;
    return `<tr>
      <td>${esc(CAT_LABELS[cat] || cat)}</td>
      <td class="t-right">${d.downloaded.toLocaleString()} <span class="t3">/ ${d.arr_total.toLocaleString()}</span></td>
      <td class="t-right">${num(done, cat, 'enriched,enriched_provisional', defs.enriched, 't-success b')} <span class="t3">${pct}%</span></td>
      ${cell(['enriched_dead'])}${cell(['rule_based', 'awaiting_llm'])}${cell(['retry_due'])}${cell(['not_found'])}${cell(['queued', 'processing_error'])}${cell(['ignored'])}${cell(['never_processed'])}
      <td class="t-right" title="Items with at least one vector-store entry (deduplicated by item identity)">${c.vectors.indexed.toLocaleString()}</td>
      <td class="t-right" title="Live raw cache entries carrying Wikipedia significance">${c.wikipedia.significance_cached.toLocaleString()}</td>
      <td class="t-right" title="Live raw cache entries with OMDb data (writer / awards / extended plot)">${c.omdb.covered.toLocaleString()}</td>
    </tr>`;
  }).join('');

  // Why not 100 %: one honest line per library — done / ignored / open, and
  // what the open rows are waiting for.
  const openLines = order.filter(c => cats[c]).map(cat => {
    const c = cats[cat], d = c.denominator, s = c.states, op = c.open || {};
    const n = k => s[k] || 0;
    const dl = d.downloaded || 0;
    const done = n('enriched') + n('enriched_provisional'), ign = n('ignored');
    const open = Math.max(0, dl - done - ign);
    const p = v => dl ? Math.round(100 * v / dl) : 0;
    const bits = [];
    if (op.due_now) bits.push(`${op.due_now.toLocaleString()} due next run`);
    if (op.waiting) bits.push(`${op.waiting.toLocaleString()} waiting on the backoff${op.next_due_at ? ` (next ${_fmtDate(op.next_due_at)})` : ''}`);
    if (op.needs_attention) bits.push(`<button type="button" class="link-num t-amber" onclick="showKbTab('attention')">${op.needs_attention.toLocaleString()} need attention</button>`);
    return `<div class="fs-11 t3"><b class="t2">${esc(CAT_LABELS[cat] || cat)}</b>: ${p(done)}% enriched · ${p(ign)}% ignored · ${p(open)}% open${bits.length ? ' — ' + bits.join(', ') : ''}</div>`;
  }).join('');

  const wh = o.watch_history_only || {};
  const whRows = order.filter(c => wh[c]).map(c =>
    `<tr><td>${esc(CAT_LABELS[c] || c)}</td><td class="t-right">${wh[c].rows.toLocaleString()}</td><td class="t-right">${wh[c].enriched.toLocaleString()}</td><td class="t-right t3">${(wh[c].not_found || 0).toLocaleString()}</td></tr>`).join('');
  const st = o.storage || {};

  document.getElementById('enrich-overview').innerHTML = `
    <section class="section">
      <div class="section-head"><h3>Library coverage</h3>
        <span class="section-hint">One denominator per row: downloaded arr items. Hover a header for its definition, click a number for the list behind it.</span>
        <div class="section-actions">
          ${running ? '<span class="badge amber badge-sm">Enrichment running <span id="enrich-pct-live"></span></span>' : ''}
          ${lastRun ? `<span class="fs-11 t3" title="${escAttr(_fmtAbs(lastRun))}">last run ${_fmtRel(lastRun)}</span>` : ''}
        </div>
      </div>
      <div class="section-body">
        <div class="tbl-wrap" style="overflow-x:auto">
          <table class="tbl"><thead><tr>
            <th>Library</th>
            <th class="t-right" title="Downloaded items / total items in the ARR — the ONE denominator every state column sums to">Items</th>
            <th class="t-right" title="Full profile written (including provisional fast-pass profiles)">Enriched</th>
            <th class="t-right" title="Enriched flag set but the cached profile is gone">Dead</th>
            <th class="t-right" title="Rule-based profile or LLM polish still pending">Partial</th>
            <th class="t-right" title="Not found so far; waiting time over — the next run tries again">Retry due</th>
            <th class="t-right" title="Every consulted source missed; waiting on the backoff">Not found</th>
            <th class="t-right" title="Queued or crashed — processed on the next run">Queued</th>
            <th class="t-right" title="Accepted by the owner as unenriched">Ignored</th>
            <th class="t-right">Never</th>
            <th class="t-right">Vectors</th>
            <th class="t-right">Wikipedia</th>
            <th class="t-right">OMDb</th>
          </tr></thead><tbody>${rows}</tbody></table>
        </div>
        <div class="mt-8">${openLines}</div>
        <p class="fs-11 t3 mt-8">Dead and metadata-changed entries re-queue automatically on the next enrichment run.</p>
      </div>
    </section>
    <div class="row" style="align-items:stretch">
      <section class="section grow" style="min-width:280px">
        <div class="section-head"><h3>Watch-history tracking</h3><span class="section-hint" title="Enrichment tracking rows from playback history (Plex/Spotify) — an independent set, deliberately NOT part of the library percentages">own set, not part of the percentages</span></div>
        <div class="section-body"><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Category</th><th class="t-right">Tracking rows</th><th class="t-right" title="Rows with a real profile (a not-found sentinel no longer counts)">Enriched</th><th class="t-right" title="Rows where every metadata source missed">Not found</th></tr></thead><tbody>${whRows}</tbody></table></div></div>
      </section>
      <section class="section grow" style="min-width:280px">
        <div class="section-head"><h3>Storage</h3><span class="section-hint">enrichment cache ${st.enrichment_cache_mb} MB · main db ${st.main_db_mb} MB · vectors ${st.chromadb_mb} MB · total ${st.total_mb} MB</span>
          <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="loadCacheInventory(this)" title="Per-source cache census: how many Wikipedia/OMDb/TVDB/… entries are stored, how many are stale, and what they weigh">Cache inventory</button></div></div>
        <div class="section-body"><div id="cache-inventory"></div></div>
      </section>
    </div>`;

  // The Spotify phases live on the Music-pipeline tab, fed by the same payload.
  const mp = o.music_pipeline || {};
  const phase = (label, pp, def) => pp ? `<tr><td title="${def}">${label}</td><td class="t-right">${pp.done.toLocaleString()} / ${pp.of.toLocaleString()}</td><td class="t-right">${pp.of ? Math.round(100 * pp.done / pp.of) : 0}%</td></tr>` : '';
  const mpEl = document.getElementById('music-phases');
  if (mpEl) mpEl.innerHTML = `<section class="section">
    <div class="section-head"><h3>Coverage</h3><span class="section-hint">where the imported plays stand</span></div>
    <div class="section-body"><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Phase</th><th class="t-right">Progress</th><th></th></tr></thead><tbody>
      ${phase('Plex match', mp.plex_match, 'Spotify plays matched to a real Plex track')}
      ${phase('MBID resolve', mp.mbid_resolve, 'Unique artists resolved to a MusicBrainz id')}
      ${phase('Genre coverage', mp.genre_coverage, 'Spotify plays carrying genre tags')}
    </tbody></table></div></div></section>`;
}

export async function loadCacheInventory(btn) {
  btnBusy(btn, 'Loading…');
  try {
    const r = await api('/api/enrichment/cache-inventory');
    const rows = r.classes.map(c =>
      `<tr><td>${esc(c.name)}</td><td style="text-align:right">${c.rows.toLocaleString()}</td>
       <td style="text-align:right">${c.live.toLocaleString()}</td>
       <td style="text-align:right;color:var(--text3)">${c.expired.toLocaleString()}</td>
       <td style="text-align:right">${c.mb}</td></tr>`).join('');
    const cov = r.coverage;
    document.getElementById('cache-inventory').innerHTML = `
      <div class="tbl-wrap" style="margin-top:8px"><table class="tbl">
        <thead><tr><th>Source class</th><th style="text-align:right">Rows</th>
          <th style="text-align:right">Live</th>
          <th style="text-align:right" title="Expired — unreadable through the cache API. Kept only where the stale row is still the last record (raw/prefetch of deleted media); the raw-cache refresh task re-pulls what matters.">Stale</th>
          <th style="text-align:right">MB</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p style="font-size:11px;color:var(--text3);margin-top:6px">
        Raw-layer coverage: Wikipedia significance ${cov.significance_text.toLocaleString()} with text / ${cov.significance_checked.toLocaleString()} checked ·
        OMDb writer ${cov.omdb_writer.toLocaleString()} · awards ${cov.omdb_awards.toLocaleString()} ·
        reception ${cov.reception_checked.toLocaleString()} — of ${cov.raw_total.toLocaleString()} raw entries.
        Total ${r.totals.rows.toLocaleString()} rows · ${r.totals.mb} MB (${r.totals.expired.toLocaleString()} stale).</p>`;
    if (btn) btn.remove();   // a permanent button for a finished job is clutter
  } catch (e) {
    btnDone(btn, 'Cache inventory (failed — retry)');
  }
}

// The numbers (overview + custodian + backfill). Repolls itself every 8 s
// while a run or a custodian tick is in progress; lists are NOT touched here
// (see _kbRefresh). Called from showKbTab, the Refresh button and after runs.
let _kbPollTimer = null;
export async function loadEnrichStatus() {
  if (_kbPollTimer) { clearTimeout(_kbPollTimer); _kbPollTimer = null; }
  const el = document.getElementById('enrich-overview');
  if (el && !el.innerHTML) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const [ov, st, cust] = await Promise.all([
      api('/api/enrichment/overview'),
      api('/api/enrichment/status?quick=true').catch(() => ({})),
      api('/api/enrichment/custodian').catch(() => null),
    ]);
    _renderKbOverview(ov, !!st.running, st.last_run);
    if (cust) _renderCustodianBar(cust);
    loadBackfillPanel();
    if (st.running || (cust && cust.ticking)) _kbPollTimer = setTimeout(loadEnrichStatus, 8000);
  } catch (e) {
    if (el) el.innerHTML = _errHtml(e, 'loadEnrichStatus()');
  }
}

// ── KB drilldown, Needs attention, match picker (healing 2026-09) ───────────
// The list behind every number: /api/enrichment/items shares the tile's
// classification, /api/enrichment/unmatched is the Needs-attention page.
// Owner actions: Search & pin (positive pin), Not this one (negative pin),
// Retry now, Ignore / Un-ignore. Button copy is mirrored in app_context.py.
const KB_SOURCE_LABELS = {tmdb: 'TMDB', omdb: 'OMDb', anilist: 'AniList', jikan: 'MAL', mb: 'MusicBrainz', lastfm: 'Last.fm'};
const KB_SOURCE_GLYPH = {ok: '✓', miss: '✗', transient: '…', skipped: '–'};
export const PIN_ID_KINDS = ['tmdb_id', 'tvdb_id', 'imdb_id', 'anilist_id', 'mal_id', 'mbid'];
const KB_PAGE = 50, KB_ATT_PAGE = 100;

// The sidebar bubble and the tab count are the same number: what the
// Needs-attention page lists (the overview counts it with the same rule).
export function _updateKbBadge(o) {
  let n = 0;
  for (const c of Object.values(o?.categories || {})) n += c?.open?.needs_attention || 0;
  setBadge('kb-badge', n);
  setBadge('kb-tab-badge', n);
}

export function _fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(/Z$|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z');
  return isNaN(d) ? '' : d.toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
}

export function _kbSourceChips(sources) {
  return (sources || []).map(s => {
    const name = KB_SOURCE_LABELS[s.source] || s.source;
    const hit = s.title ? ` → ${s.title}${s.year ? ` (${s.year})` : ''}` : '';
    return `<span class="badge muted badge-sm" title="${escAttr(`${name}: ${s.status}${hit}`)}">${esc(name)} ${KB_SOURCE_GLYPH[s.status] || esc(s.status || '')}</span>`;
  }).join(' ');
}

export function _kbData(it) {
  return `data-svc="${escAttr(it.service)}" data-id="${Number(it.arr_id)}" data-title="${escAttr(it.title || '')}" data-year="${it.year || ''}" data-cat="${escAttr(it.category || '')}"`;
}

// Row actions (admin): three visible, the rest behind More. Button copy is
// mirrored in app_context.py — rename here, rename there.
export function _kbItemActions(it) {
  if (!state.currentUser?.is_admin) return '';
  const d = _kbData(it);
  const ign = it.state === 'ignored';
  return `<div class="panel-actions">
    <button type="button" class="btn btn-secondary btn-sm" ${d} onclick="kbFixMatch(this)" title="Search the arr, TMDB or AniList and pin the entity this item really is">Search &amp; pin</button>
    <button type="button" class="btn btn-secondary btn-sm" ${d} onclick="kbRetry(this)" title="Attempts back to zero — the next enrichment run tries again">Retry now</button>
    ${ign ? `<button type="button" class="btn btn-secondary btn-sm" ${d} onclick="kbUnignore(this)" title="Back into the queue">Un-ignore</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" ${d} onclick="kbIgnore(this)" title="Accept the gap: no more retries, not counted as open">Ignore</button>`}
    ${menuHtml([it.arr_url ? {label: `Open in ${it.service}`, href: it.arr_url} : null])}
  </div>`;
}

// One row per item, the same anatomy everywhere: head (title, badges,
// actions) → the sentence → attempts/next try/match → findings → sources.
export function _kbItemCard(it, extraBadges) {
  const pin = it.pin ? `<span class="badge amber badge-sm" title="${escAttr(JSON.stringify(it.pin))}">pinned</span>` : '';
  const meta = [
    it.attempt_count ? `tried ${it.attempt_count}×` : '',
    it.next_retry_at ? `<span title="${escAttr(_fmtAbs(it.next_retry_at))}">next try ${_fmtRel(it.next_retry_at)}</span>` : '',
    (it.match_confidence != null && it.match_basis === 'title_search') ? `matched by title at ${Math.round(it.match_confidence * 100)}%` : '',
  ].filter(Boolean).join(' · ');
  const findings = (it.findings || []).map(f => `<div class="panel-item-meta row mt-4">
      <span class="badge danger badge-sm">${esc(f.label || f.base || f.kind)}</span><span>${esc(_kbFindingText(f))}</span>
      ${state.currentUser?.is_admin ? `<button type="button" class="btn btn-secondary btn-sm" data-fid="${Number(f.id)}" onclick="kbDismissFinding(this)" title="Silence this finding for good — the audit will not raise it again">Dismiss finding</button>` : ''}
    </div>`).join('');
  return `<div class="panel-item">
    <div class="panel-item-head">
      <div class="panel-item-title">${esc(it.title || '?')}${it.year ? `<span class="t3">(${it.year})</span>` : ''}
        <span class="badge muted badge-sm">${esc(it.service)}</span>
        <span class="badge muted badge-sm">${esc(it.state_label || it.state)}</span>${pin}${extraBadges || ''}</div>
      ${_kbItemActions(it)}
    </div>
    <div class="panel-item-sub">${esc(it.reason || '')}</div>
    ${meta ? `<div class="panel-item-meta mt-4">${meta}</div>` : ''}
    ${findings}
    ${it.sources?.length ? `<div class="panel-item-foot">${_kbSourceChips(it.sources)}</div>` : ''}
  </div>`;
}

export function _kbFindingText(f) {
  const d = f.detail || {};
  const yr = y => (y ? ` (${y})` : '');
  if (f.base === 'wrong_entity') return `arr says ${d.arr_title || '?'}${yr(d.arr_year)}, the profile is ${d.profile_title || '?'}${yr(d.profile_year)}`;
  if (f.base === 'id_conflict') return `${d.source || 'id'} ${d.value || ''} is shared by ${(d.titles || []).join(', ')}`;
  if (f.base === 'pin_violated') return `the profile's ${d.id || 'id'} ${d.profile ?? '?'} contradicts the pinned ${d.pinned ?? '?'}`;
  if (f.base === 'zero_rating') return 'no rating in any source';
  if (f.base === 'malformed') return 'the cached profile is not readable';
  return f.kind || '';
}
export function kbDismissFinding(btn) { return _kbAction(btn, `/api/enrichment/findings/${Number(btn.dataset.fid)}/dismiss`, 'POST', 'Finding dismissed — the audit will not raise it again'); }

// The list behind a number: a section under the overview table with the
// state explainer as its hint, a pager and a Close.
export async function loadKbItems(cat, states, offset = 0) {
  const el = document.getElementById('kb-drilldown');
  if (!el) return;
  const fresh = !_kbDrill || _kbDrill.cat !== cat || _kbDrill.states !== states;
  _kbDrill = {cat, states, offset};
  if (fresh) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const r = await api(`/api/enrichment/items?category=${encodeURIComponent(cat)}&state=${encodeURIComponent(states)}&offset=${offset}&limit=${KB_PAGE}`);
    const defs = Object.values(r.definitions || {});
    el.innerHTML = `<section class="section">
      <div class="section-head">
        <h3>${esc(CAT_LABELS[cat] || cat)} · ${esc(defs.map(d => d.label).join(' / '))} <span class="badge muted badge-sm">${r.total.toLocaleString()}</span></h3>
        <span class="section-hint">${esc(defs.map(d => `${d.explainer} ${d.next_step || ''}`).join(' '))}</span>
        <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="closeKbDrilldown()">Close</button></div>
      </div>
      <div class="section-body">
        ${r.items.length ? r.items.map(it => _kbItemCard(it)).join('') : emptyHtml('Nothing here.')}
        ${pagerHtml({offset, limit: KB_PAGE, total: r.total, call: `loadKbItems('${cat}','${states}',{offset})`})}
      </div>
    </section>`;
    if (fresh) el.scrollIntoView({behavior: 'smooth', block: 'nearest'});
  } catch (e) { el.innerHTML = _errHtml(e); }
}
export function closeKbDrilldown() {
  _kbDrill = null;
  const el = document.getElementById('kb-drilldown');
  if (el) el.innerHTML = '';
}

// Needs attention: filter chips per reason (server-side, so the page count
// is honest), the same rows as the drilldown plus one amber badge per reason.
export async function loadKbAttention(offset = 0, reason = null) {
  const el = document.getElementById('kb-attention');
  if (!el) return;
  _kbAtt = {offset, reason: reason || null};
  if (!el.dataset.loaded) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const q = new URLSearchParams({offset: String(offset), limit: String(KB_ATT_PAGE)});
    if (reason) q.set('reason', reason);
    const r = await api(`/api/enrichment/unmatched?${q.toString()}`);
    el.dataset.loaded = '1';
    const defs = r.reason_definitions || {};
    const chip = (key, label, n) => `<button type="button" class="chip${(reason || '') === key ? ' active' : ''}" onclick="loadKbAttention(0, ${key ? `'${key}'` : 'null'})" title="${escAttr(defs[key]?.explainer || 'Everything that needs a human')}">${esc(label)} <span class="t3">${Number(n || 0).toLocaleString()}</span></button>`;
    const chips = [chip('', 'All', r.total_all ?? r.total)]
      .concat(Object.entries(r.by_reason || {}).map(([k, n]) => chip(k, defs[k]?.label || k, n))).join('');
    const badgesFor = it => (it.reasons || []).map(k => `<span class="badge amber badge-sm" title="${escAttr(defs[k]?.explainer || '')}">${esc(defs[k]?.label || k)}</span>`).join('');
    const findingsNote = Object.keys(r.findings_summary || {}).length
      ? ` Open audit findings: ${Object.entries(r.findings_summary).map(([k, n]) => `${esc(defs[k]?.label || k)} ${n}`).join(' · ')}.` : '';
    const empty = reason ? emptyHtml('Nothing in this group.') : emptyHtml('Nothing needs a human right now.', null, null, {good: true});
    el.innerHTML = `<section class="section">
      <div class="section-head">
        <h3>Needs attention <span class="badge amber badge-sm">${r.total.toLocaleString()}</span></h3>
        <span class="section-hint">${reason ? esc(defs[reason]?.label || reason) : 'the items the pipeline cannot settle alone'}</span>
      </div>
      <div class="section-body">
        <p class="fs-12 t3 mb-8">Tried twice or more without a hit, found under the wrong year, refused as too far off, matched with middling confidence, or flagged by the weekly audit (wrong entity, shared id, pin contradicted). Pin the right entity, retry, dismiss a finding, or accept the gap.${findingsNote}</p>
        <div class="row mb-12">${chips}</div>
        ${r.items.length ? r.items.map(it => _kbItemCard(it, badgesFor(it))).join('') : empty}
        ${pagerHtml({offset, limit: KB_ATT_PAGE, total: r.total, call: `loadKbAttention({offset}, ${reason ? `'${reason}'` : 'null'})`})}
      </div>
    </section>`;
  } catch (e) { el.innerHTML = _errHtml(e); }
}

// Owner actions on a row: the button shows busy, the outcome is a toast, and
// the numbers + the lists on screen reload.
export async function _kbAction(btn, path, method, doneText) {
  btnBusy(btn);
  try {
    const r = await api(path, method);
    if (r.success) { toast(doneText, 'success'); _kbRefresh(); }
    else { toast(r.error || 'Failed', 'danger'); btnDone(btn); }
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
export function kbRetry(btn)    { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/retry`, 'POST', 'Attempts reset — the next run tries again'); }
export function kbIgnore(btn)   { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/ignore`, 'POST', 'Ignored — no more retries, not counted as open'); }
export function kbUnignore(btn) { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/ignore`, 'DELETE', 'Back in the queue'); }

export function kbFixMatch(btn) {
  const {svc, id, title, year, cat} = btn.dataset;
  openMatchPicker({service: svc, arrId: Number(id), title, year: year ? Number(year) : null, category: cat || undefined, onDone: _kbRefresh});
}

// ── DATA CUSTODIAN (debt-based maintenance — replaces the button zoo) ────────
export function _renderCustodianBar(c) {
  const el = document.getElementById('custodian-bar');
  if (!el) return;
  const due = (c.tasks || []).filter(t => t.due).length;
  let line;
  if (c.ticking) {
    line = 'Maintenance running in the background…';
  } else if (c.report && c.report.ts) {
    const ago = Math.max(0, Math.round((Date.now() - new Date(c.report.ts + 'Z').getTime()) / 60000));
    const acts = (c.report.actions || []);
    const summary = acts.length
      ? acts.map(a => `${a.task.replace('custodian_', '')}: ${a.result}`).join(' · ')
      : 'nothing was due';
    line = `Last run ${ago} min ago (${c.report.duration_s}s) — ${summary}`;
  } else {
    line = 'No maintenance run yet this session — first tick fires a few minutes after start.';
  }
  el.innerHTML = `<section class="section">
    <div class="section-head">
      <h3>Data custodian ${due ? `<span class="badge amber badge-sm">${due} task${due > 1 ? 's' : ''} due</span>` : '<span class="badge muted badge-sm">all caught up</span>'}</h3>
      <span class="section-hint" title="Every maintenance task carries a cadence and a last-run stamp; whatever is overdue runs automatically while the app is open — enrichment, OMDb, Wikipedia significance, Spotify phases, taste vectors, audits, backups.">${esc(line)}</span>
      <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" onclick="runMaintenance(this)"${c.ticking ? ' disabled' : ''}>Run maintenance now</button></div>
    </div></section>`;
}

// First-run backfill. The archive sources fill themselves on a daily tick,
// which is far too slow for a library that was set up this week. This panel
// offers the same walkers on demand and REMOVES ITSELF once a source is
// sufficiently covered — a permanent button for a finished job is clutter.
export async function loadBackfillPanel() {
  const el = document.getElementById('backfill-panel');
  if (!el) return;
  let d;
  try { d = await api('/api/enrichment/backfill-status'); }
  catch (e) { el.innerHTML = ''; return; }
  if (!d || !d.any_offer) { el.innerHTML = ''; return; }   // nothing worth offering
  const rows = d.sources.filter(s => s.offer).map(s => `
    <div class="panel-item${s.running ? ' live' : ''}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(s.label)}${s.running ? '<span class="badge amber badge-sm">running</span>' : ''}</div>
        <div class="panel-actions">${s.running
          ? `<button type="button" class="btn btn-secondary btn-sm" onclick="stopBackfill('${esc(s.key)}',this)">Stop</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" onclick="startBackfill('${esc(s.key)}',this)">Fetch now</button>`}</div>
      </div>
      <div class="panel-item-sub">${esc(s.blurb)}</div>
      <div class="panel-item-foot">
        <div class="progress-bar" style="margin-top:0"><div class="progress-fill" style="width:${Number(s.pct) || 0}%"></div></div>
        <div class="fs-11 t3 mt-4">${s.pct}% · ${s.missing.toLocaleString()} to go</div>
      </div>
    </div>`).join('');
  el.innerHTML = `<section class="section">
    <div class="section-head"><h3>Finish the backfill</h3><span class="section-hint">These sources feed the deletion judge. The daily tick fills them slowly; fetch them now and this panel disappears above ${d.threshold_pct}% coverage.</span></div>
    <div class="section-body">${rows}</div></section>`;
}

export async function startBackfill(source, btn) {
  btnBusy(btn, 'Starting…');
  try { await api('/api/enrichment/backfill/' + source, 'POST'); toast('Backfill started — progress shows in Activity', 'success'); }
  catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  // The panel re-reads coverage, so a finished source removes its own row.
  setTimeout(loadBackfillPanel, 1500);
}

export async function stopBackfill(source, btn) {
  btnBusy(btn, 'Stopping…');
  try { await api('/api/enrichment/backfill/' + source + '/stop', 'POST'); toast('Stop requested', 'info'); }
  catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadBackfillPanel, 1200);
}

export async function runMaintenance(btn) {
  btnBusy(btn, 'Starting…');
  try {
    const r = await api('/api/enrichment/custodian/run', 'POST');
    if (r.status === 'started') toast('Maintenance started — whatever is due runs now', 'success');
    else toast('Maintenance is already running', 'info');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadEnrichStatus, 4000);
}
