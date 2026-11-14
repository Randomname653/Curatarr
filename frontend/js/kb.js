// ── MAPPING STATS + PROFILE BROWSER ──────────────────────────────────────────
// Two questions, two result areas — the second answer no longer overwrites the first.
import { skeletonHtml, CAT_LABELS, EL, SVG_STAR, SVG_WARN, _errHtml, _errMsg, _fmtAbs, _fmtRel, act, btnBusy, btnDone, emptyHtml, esc, escAttr, loc, menuHtml, pagerHtml, setBadge, toast } from './ui.js';
import { api } from './api.js';
import { state } from './state.js';
import { loadMusicStatus } from './music.js';
import { openMatchPicker } from './picker.js';
import { tabToHash } from './nav.js';
import { t, th } from './i18n.js';
export async function loadMappingStats(btn) {
  const el = document.getElementById('mapping-stats');
  btnBusy(btn, t('common.loading'));
  try {
    const s = await api('/api/enrichment/mapping-stats');
    el.innerHTML = `
      <div class="gap-16 row mt-4">
        <span><b class="t-amber">${(s.total_entries || 0).toLocaleString(loc())}</b> <span class="t2">${th('kb.total_entries')}</span></span>
        <span><b class="t-success">${(s.tvdb_mapped || 0).toLocaleString(loc())}</b> <span class="t2">${th('kb.tvdb_mapped')}</span></span>
        ${s.anime != null ? `<span><b>${th('kb.x_of_y', {x: (s.with_anilist || 0).toLocaleString(loc()), y: s.anime.toLocaleString(loc())})}</b> <span class="t2">${th('kb.anime_with_anilist')}</span></span>
        <span><b>${(s.pinned || 0).toLocaleString(loc())}</b> <span class="t2">${th('kb.pinned_crossref', {n: (s.auto_pinned || 0).toLocaleString(loc())})}</span></span>` : ''}
      </div>
      <div class="fs-11 t3 mt-4">${th('kb.downloaded')} ${s.downloaded_at ? esc(new Date(s.downloaded_at).toLocaleString(loc())) : th('kb.never')} · ${th('kb.refreshes_weekly')}</div>`;
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

export async function checkMappingCoverage(btn) {
  const el = document.getElementById('mapping-coverage');
  btnBusy(btn, t('kb.checking'));
  el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('kb.checking_coverage')}</p>`;
  try {
    const c = await api('/api/enrichment/mapping-coverage');
    if (c.error) el.innerHTML = `<p class="load-err">${esc(c.error)}</p>`;
    else el.innerHTML = `
      <div class="gap-16 row mt-8">
        <span><b class="t-amber">${c.total_anime}</b> <span class="t2">${th('kb.anime_in_sonarr')}</span></span>
        <span><b class="t-success">${th('kb.n_pct', {n: c.covered, pct: c.coverage_pct})}</b> <span class="t2">${th('kb.in_mapping')}</span></span>
        <span><b>${c.anilist_resolved}</b> <span class="t2">${th('kb.anilist_ready')}</span></span>
        <span><b class="t-danger">${c.missing}</b> <span class="t2">${th('kb.not_in_mapping')}</span></span>
      </div>
      ${c.missing_sample?.length ? `
      <details class="mt-8">
        <summary class="pointer fs-11 t3">${th('kb.not_mapped', {n: c.missing})}</summary>
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
  el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('kb.loading_profiles')}</p>`;
  try {
    const r = await api(`/api/enrichment/profiles?category=${cat}&source=${src}&limit=${PROFILE_PAGE}&offset=${offset}&search=${encodeURIComponent(search)}`);
    if (!r.profiles?.length) {
      el.innerHTML = emptyHtml(th('kb.no_profiles'));
      return;
    }
    const srcLabel = p => p.profile_source === 'watch_history' ? t('kb.watched')
      : (p.profile_source || '').startsWith('arr:') ? p.profile_source.slice(4) : (p.profile_source || '?');
    const block = (label, html, mono) => html
      ? `<div class="mb-8"><div class="caps fs-10 t3">${label}</div><div class="lh-16 break-word ${mono ? 'fs-11 t3 mono' : 'fs-12 t2'} mt-4">${html}</div></div>` : '';
    el.innerHTML = `
      <div class="fs-12 t3 mb-8">${th('kb.enriched_showing', {n: r.total.toLocaleString(loc()), from: offset + 1, to: Math.min(offset + PROFILE_PAGE, r.total)})}</div>
      ${r.profiles.map(p => `
        <details class="panel-item">
          <summary class="panel-item-head">
            <div class="grow">
              <div class="panel-item-title">${esc(p.title)}
                <span class="badge ${p.has_profile ? 'success' : 'muted'} badge-sm">${th(p.has_profile ? 'kb.profile' : 'kb.no_profile')}</span>
                <span class="badge ${p.source?.includes('+llm') ? 'success' : p.source?.includes('rule_based') ? 'amber' : 'muted'} badge-sm">${esc(p.source || '?')}</span>
                <span class="badge muted badge-sm">${esc(srcLabel(p))}</span>
                ${p.rating ? `<span class="fs-11 t-amber">${SVG_STAR} ${p.rating}</span>` : ''}
                <span class="fs-11 t3">${p.enriched_at ? new Date(p.enriched_at).toLocaleDateString(loc()) : '?'}</span>
              </div>
              ${p.genres?.length ? `<div class="gap-4 row mt-4">${p.genres.slice(0, 5).map(g => `<span class="badge muted badge-sm">${esc(g)}</span>`).join('')}</div>` : ''}
            </div>
            <span class="fs-11 t3">${th('kb.details')}</span>
          </summary>
          <div class="rule-top panel-item-foot">
            ${block(th('kb.themes'), p.themes?.length ? esc(p.themes.join(' · ')) : '')}
            ${block(th('kb.mood'), p.mood?.length ? esc(p.mood.join(' · ')) : '')}
            ${block(th('kb.summary'), p.plot_summary ? esc(p.plot_summary) : '')}
            ${block(th('kb.embedding_text'), p.embedding_text ? esc(p.embedding_text) : '', true)}
          </div>
        </details>`).join('')}
      ${pagerHtml({offset, limit: PROFILE_PAGE, total: r.total, page: o => act('loadProfiles', o)})}`;
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
  tabToHash('enrich', state._kbTab);    // #enrich/attention survives a reload
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
  credit:               'A collaboration credit nobody knows as an act; its plays count for the artists it names.',
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

// running: true / false, or null when the status read failed (statusErr says why).
export function _renderKbOverview(o, running, lastRun, statusErr = null) {
  _updateKbBadge(o);
  const cats = o.categories || {};
  const served = Object.entries(o.state_definitions || {}).map(([k, v]) => [k, `${v.explainer} ${v.next_step || ''}`.trim()]);
  const defs = Object.assign({}, KB_STATE_DEFS, Object.fromEntries(served));
  const order = ['movie', 'show', 'anime', 'music'];
  // Every count is a door: click → the list behind it (same classification).
  const num = (v, cat, keys, tip, cls) => v
    ? `<button type="button" class="link-num${cls ? ' ' + cls : ''}" title="${th('kb.click_to_list', {tip})}" ${act('loadKbItems', cat, keys)}>${v.toLocaleString(loc())}</button>`
    : `<span class="t3" title="${escAttr(tip)}">0</span>`;
  // One card per library: how far enrichment got (a meter over the one
  // denominator, downloaded arr items), the states still open as chips —
  // only the ones that are not zero, each a door to its list — and what the
  // open rows are waiting for. The raw-layer counts sit quietly at the foot.
  const STATE_CHIPS = [['dead', ['enriched_dead']], ['partial', ['rule_based', 'awaiting_llm']],
    ['retry_due', ['retry_due']], ['not_found', ['not_found']], ['queued', ['queued', 'processing_error']],
    ['never', ['never_processed']], ['ignored', ['ignored']], ['credits', ['credit']]];
  const cards = order.filter(c => cats[c]).map(cat => {
    const c = cats[cat], d = c.denominator, s = c.states, op = c.open || {};
    const n = k => s[k] || 0;
    const dl = d.downloaded || 0;
    const done = n('enriched') + n('enriched_provisional');
    const pct = dl ? Math.round(100 * done / dl) : 0;
    const chips = STATE_CHIPS.map(([label, keys]) => {
      const v = keys.reduce((a, k) => a + n(k), 0);
      return v ? `<span class="kb-chip">${th(`kb.chip.${label}`)} ${num(v, cat, keys.join(','), keys.map(k => defs[k] || k).join(' · '))}</span>` : '';
    }).join('');
    const waits = [];
    if (op.due_now) waits.push(th('kb.due_next_run', {n: op.due_now.toLocaleString(loc())}));
    if (op.waiting) waits.push(th('kb.on_backoff', {n: op.waiting.toLocaleString(loc())}) + (op.next_due_at ? ` ${th('kb.next_on', {date: _fmtDate(op.next_due_at)})}` : ''));
    const attnLink = op.needs_attention
      ? `<button type="button" class="link-num t-amber" ${act('showKbTab', 'attention')}>${th('kb.need_attention', {n: op.needs_attention.toLocaleString(loc())})}</button>` : '';
    return `<article class="kb-lib${pct >= 95 ? ' is-done' : ''}">
      <div class="kb-lib-head"><h4>${esc(CAT_LABELS[cat] || cat)}</h4>
        <span class="fs-12 t3">${th('kb.downloaded_of', {n: dl.toLocaleString(loc()), total: d.arr_total.toLocaleString(loc())})}</span></div>
      <div class="kb-lib-pct"><b>${th('kb.pct', {pct})}</b><span>${th('kb.enriched')} · ${num(done, cat, 'enriched,enriched_provisional', defs.enriched)}</span></div>
      <progress class="kb-meter" max="100" value="${pct}" aria-label="${th('kb.meter_aria', {cat: CAT_LABELS[cat] || cat, pct})}"></progress>
      ${chips ? `<div class="kb-chips">${chips}</div>` : `<div class="kb-chips"><span class="fs-12 t3">${th('kb.nothing_open')}</span></div>`}
      ${waits.length || attnLink ? `<div class="kb-lib-wait">${waits.join(' · ')}${waits.length && attnLink ? ' · ' : ''}${attnLink}</div>` : ''}
      <div class="kb-lib-foot">
        <span title="${th('kb.vectors_title')}">${th('kb.vectors')} <b>${c.vectors.indexed.toLocaleString(loc())}</b></span>
        <span title="${th('kb.wikipedia_title')}">Wikipedia <b>${c.wikipedia.significance_cached.toLocaleString(loc())}</b></span>
        <span title="${th('kb.omdb_title')}">OMDb <b>${c.omdb.covered.toLocaleString(loc())}</b></span>
      </div>
    </article>`;
  }).join('');

  const wh = o.watch_history_only || {};
  const whRows = order.filter(c => wh[c]).map(c =>
    `<tr><td class="cell-primary">${esc(CAT_LABELS[c] || c)}</td><td class="t-right">${wh[c].rows.toLocaleString(loc())}</td><td class="t-right">${wh[c].enriched.toLocaleString(loc())}</td><td class="t-right t3">${(wh[c].not_found || 0).toLocaleString(loc())}</td></tr>`).join('');
  const st = o.storage || {};

  const mb = v => (v || 0) >= 1024 ? `${((v || 0) / 1024).toFixed(1)} GB` : `${Math.round(v || 0)} MB`;
  const store = [[th('kb.enrichment_cache'), st.enrichment_cache_mb], [th('kb.main_db'), st.main_db_mb], [th('kb.vectors'), st.chromadb_mb]]
    .map(([l, v]) => `<div class="kb-store-tile"><b>${mb(v)}</b><span>${l}</span></div>`).join('');

  document.getElementById('enrich-overview').innerHTML = `
    <section class="section">
      <div class="section-head"><h3>${th('kb.coverage')}</h3>
        <span class="section-hint">${th('kb.coverage_hint')}</span>
        <div class="section-actions">
          ${running ? `<span class="badge amber badge-sm">${th('kb.enrichment_running')} <span id="enrich-pct-live"></span></span>` : ''}
          ${running === null ? `<span class="badge muted badge-sm" title="${th('kb.status_unreadable', {error: statusErr || t('kb.no_answer')})}">${th('kb.status_unknown')}</span>` : ''}
          ${lastRun ? `<span class="fs-11 t3" title="${escAttr(_fmtAbs(lastRun))}">${th('kb.last_run', {when: _fmtRel(lastRun)})}</span>` : ''}
        </div>
      </div>
      <div class="section-body">
        ${cards ? `<div class="kb-lib-grid">${cards}</div>` : emptyHtml(th('kb.empty'), '', '', {mark: true})}
        <p class="fs-11 t3 mt-12">${th('kb.requeue_note')}</p>
      </div>
    </section>
    <div class="kb-pair">
      <section class="section">
        <div class="section-head"><h3>${th('kb.wh_tracking')}</h3><span class="section-hint" title="${th('kb.wh_title')}">${th('kb.wh_hint')}</span></div>
        <div class="section-body"><div class="tbl-wrap"><table class="tbl tbl-cards"><thead><tr><th>${th('kb.col_category')}</th><th class="t-right">${th('kb.col_tracking')}</th><th class="t-right" title="${th('kb.col_enriched_title')}">${th('kb.col_enriched')}</th><th class="t-right" title="${th('kb.col_not_found_title')}">${th('kb.chip.not_found')}</th></tr></thead><tbody>${whRows}</tbody></table></div></div>
      </section>
      <section class="section">
        <div class="section-head"><h3>${th('kb.storage')}</h3><span class="section-hint">${th('kb.in_all', {size: mb(st.total_mb)})}</span>
          <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('loadCacheInventory', EL)} title="${th('kb.cache_inventory_title')}">${th('kb.cache_inventory')}</button></div></div>
        <div class="section-body"><div class="kb-store">${store}</div><div id="cache-inventory"></div></div>
      </section>
    </div>`;

  // The Spotify phases live on the Music-pipeline tab, fed by the same payload.
  const mp = o.music_pipeline || {};
  const phase = (label, pp, def) => pp ? `<tr><td class="cell-primary" title="${def}">${label}</td><td class="t-right">${pp.done.toLocaleString(loc())} / ${pp.of.toLocaleString(loc())}</td><td class="t-right">${th('kb.pct', {pct: pp.of ? Math.round(100 * pp.done / pp.of) : 0})}</td></tr>` : '';
  // A queue that never empties used to read as a stuck bar. MusicBrainz
  // simply does not know every artist a Spotify export carries, and those
  // names are parked with a growing backoff rather than re-queried nightly.
  const _parkedNote = (p) => (p && p.waiting)
    ? `<p class="fs-12 t3 mt-8">${th('kb.parked', {n: p.waiting, count: p.waiting.toLocaleString(loc())})}${p.next_retry_at ? `. ${th('kb.next_due_back', {when: _fmtRel(p.next_retry_at)})}` : ''}.</p>`
    : '';
  const mpEl = document.getElementById('music-phases');
  if (mpEl) mpEl.innerHTML = `<section class="section">
    <div class="section-head"><h3>${th('kb.mp_coverage')}</h3><span class="section-hint">${th('kb.mp_hint')}</span></div>
    <div class="section-body"><div class="tbl-wrap"><table class="tbl tbl-cards"><thead><tr><th>${th('kb.col_phase')}</th><th class="t-right">${th('kb.col_progress')}</th><th></th></tr></thead><tbody>
      ${phase(th('kb.ph_plex'), mp.plex_match, th('kb.ph_plex_title'))}
      ${phase(th('kb.ph_mbid'), mp.mbid_resolve, th('kb.ph_mbid_title'))}
      ${phase(th('kb.ph_genre'), mp.genre_coverage, th('kb.ph_genre_title'))}
    </tbody></table></div>${_parkedNote(mp.mbid_parked)}</div></section>`;
}

export async function loadCacheInventory(btn) {
  btnBusy(btn, t('common.loading'));
  try {
    const r = await api('/api/enrichment/cache-inventory');
    const rows = r.classes.map(c =>
      `<tr><td class="cell-primary">${esc(c.name)}</td><td class="t-right">${c.rows.toLocaleString(loc())}</td>
       <td class="t-right">${c.live.toLocaleString(loc())}</td>
       <td class="t-right t3">${c.expired.toLocaleString(loc())}</td>
       <td class="t-right">${c.mb}</td></tr>`).join('');
    const cov = r.coverage;
    document.getElementById('cache-inventory').innerHTML = `
      <div class="mt-8 tbl-wrap"><table class="tbl tbl-cards">
        <thead><tr><th>${th('kb.col_source')}</th><th class="t-right">${th('kb.col_rows')}</th>
          <th class="t-right">${th('kb.col_live')}</th>
          <th class="t-right" title="${th('kb.col_stale_title')}">${th('kb.col_stale')}</th>
          <th class="t-right">MB</th></tr></thead>
        <tbody>${rows}</tbody></table></div>
      <p class="fs-11 t3 mt-6">
        ${th('kb.raw_coverage', {text: cov.significance_text.toLocaleString(loc()), checked: cov.significance_checked.toLocaleString(loc())})} ·
        ${th('kb.raw_omdb', {writer: cov.omdb_writer.toLocaleString(loc()), awards: cov.omdb_awards.toLocaleString(loc())})} ·
        ${th('kb.raw_reception', {n: cov.reception_checked.toLocaleString(loc()), total: cov.raw_total.toLocaleString(loc())})}
        ${th('kb.raw_total', {rows: r.totals.rows.toLocaleString(loc()), mb: r.totals.mb, stale: r.totals.expired.toLocaleString(loc())})}</p>`;
    if (btn) btn.remove();   // a permanent button for a finished job is clutter
  } catch (e) {
    btnDone(btn, t('kb.cache_inventory_failed'));
  }
}

// The numbers (overview + custodian + backfill). Repolls itself every 8 s
// while a run or a custodian tick is in progress; lists are NOT touched here
// (see _kbRefresh). Called from showKbTab, the Refresh button and after runs.
let _kbPollTimer = null;
export async function loadEnrichStatus() {
  if (_kbPollTimer) { clearTimeout(_kbPollTimer); _kbPollTimer = null; }
  const el = document.getElementById('enrich-overview');
  // The first open reads the whole library (seconds on a fresh install): its shape, not a bare line.
  if (el && !el.innerHTML) el.innerHTML = skeletonHtml(t('common.loading'), {kind: 'tiles'});
  try {
    // A failed status or custodian read used to fall back to {} / null:
    // "not running" in the middle of a run, and the 8 s refresh stopped. It
    // is "status unknown" now, re-checked every 30 s.
    const [ov, st, cust] = await Promise.all([
      api('/api/enrichment/overview'),
      api('/api/enrichment/status?quick=true').catch(e => ({_err: e})),
      api('/api/enrichment/custodian').catch(e => ({_err: e})),
    ]);
    _renderKbOverview(ov, st._err ? null : !!st.running, st.last_run, st._err ? _errMsg(st._err) : null);
    if (cust._err) _renderCustodianUnknown(_errMsg(cust._err));
    else _renderCustodianBar(cust);
    loadBackfillPanel();
    const delay = _kbStatusDelay(st, cust);
    if (delay) _kbPollTimer = setTimeout(loadEnrichStatus, delay);
  } catch (e) {
    if (el) el.innerHTML = _errHtml(e, act('loadEnrichStatus'));
  }
}

// When to look again: 30 s while either answer is unknown, 8 s while work
// runs, otherwise not at all.
export function _kbStatusDelay(st, cust) {
  if (st?._err || cust?._err) return 30000;
  if (st?.running || cust?.ticking) return 8000;
  return 0;
}

function _renderCustodianUnknown(msg) {
  const el = document.getElementById('custodian-bar');
  if (!el) return;
  el.innerHTML = `<section class="section">
    <div class="section-head">
      <h3>${th('kb.custodian')} <span class="badge muted badge-sm" title="${escAttr(msg)}">${th('kb.status_unknown_lc')}</span></h3>
      <span class="section-hint load-err">${SVG_WARN} ${th('kb.custodian_unreadable', {error: msg})}</span>
    </div></section>`;
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
  return isNaN(d) ? '' : d.toLocaleDateString(loc(), {day: 'numeric', month: 'short'});
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
    <button type="button" class="btn btn-secondary btn-sm" ${d} ${act('kbFixMatch', EL)} title="${th('kb.search_pin_title')}">${th('kb.search_pin')}</button>
    <button type="button" class="btn btn-secondary btn-sm" ${d} ${act('kbRetry', EL)} title="${th('kb.retry_now_title')}">${th('kb.retry_now')}</button>
    ${ign ? `<button type="button" class="btn btn-secondary btn-sm" ${d} ${act('kbUnignore', EL)} title="${th('kb.unignore_title')}">${th('kb.unignore')}</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" ${d} ${act('kbIgnore', EL)} title="${th('kb.ignore_title')}">${th('kb.ignore')}</button>`}
    ${menuHtml([it.arr_url ? {label: t('actions.open_in', {server: it.service}), href: it.arr_url} : null])}
  </div>`;
}

// One row per item, the same anatomy everywhere: head (title, badges,
// actions) → the sentence → attempts/next try/match → findings → sources.
export function _kbItemCard(it, extraBadges) {
  const pin = it.pin ? `<span class="badge amber badge-sm" title="${escAttr(JSON.stringify(it.pin))}">${th('kb.pinned')}</span>` : '';
  const meta = [
    it.attempt_count ? th('kb.tried', {n: it.attempt_count}) : '',
    it.next_retry_at ? `<span title="${escAttr(_fmtAbs(it.next_retry_at))}">${th('kb.next_try', {when: _fmtRel(it.next_retry_at)})}</span>` : '',
    (it.match_confidence != null && it.match_basis === 'title_search') ? th('kb.matched_by_title', {pct: Math.round(it.match_confidence * 100)}) : '',
  ].filter(Boolean).join(' · ');
  const findings = (it.findings || []).map(f => `<div class="panel-item-meta row mt-4">
      <span class="badge danger badge-sm">${esc(f.label || f.base || f.kind)}</span><span>${esc(_kbFindingText(f))}</span>
      ${state.currentUser?.is_admin ? `<button type="button" class="btn btn-secondary btn-sm" data-fid="${Number(f.id)}" ${act('kbDismissFinding', EL)} title="${th('kb.dismiss_finding_title')}">${th('kb.dismiss_finding')}</button>` : ''}
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
  if (f.base === 'wrong_entity') return t('kb.f_wrong_entity', {arr: `${d.arr_title || '?'}${yr(d.arr_year)}`, profile: `${d.profile_title || '?'}${yr(d.profile_year)}`});
  if (f.base === 'id_conflict') return t('kb.f_id_conflict', {source: d.source || 'id', value: d.value || '', titles: (d.titles || []).join(', ')});
  if (f.base === 'pin_violated') return t('kb.f_pin_violated', {id: d.id || 'id', profile: d.profile ?? '?', pinned: d.pinned ?? '?'});
  if (f.base === 'zero_rating') return t('kb.f_zero_rating');
  if (f.base === 'malformed') return t('kb.f_malformed');
  return f.kind || '';
}
export function kbDismissFinding(btn) { return _kbAction(btn, `/api/enrichment/findings/${Number(btn.dataset.fid)}/dismiss`, 'POST', t('kb.finding_dismissed')); }

// The list behind a number: a section under the overview table with the
// state explainer as its hint, a pager and a Close.
export async function loadKbItems(cat, states, offset = 0) {
  const el = document.getElementById('kb-drilldown');
  if (!el) return;
  const fresh = !_kbDrill || _kbDrill.cat !== cat || _kbDrill.states !== states;
  _kbDrill = {cat, states, offset};
  if (fresh) el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('common.loading')}</p>`;
  try {
    const r = await api(`/api/enrichment/items?category=${encodeURIComponent(cat)}&state=${encodeURIComponent(states)}&offset=${offset}&limit=${KB_PAGE}`);
    const defs = Object.values(r.definitions || {});
    el.innerHTML = `<section class="section">
      <div class="section-head">
        <h3>${esc(CAT_LABELS[cat] || cat)} · ${esc(defs.map(d => d.label).join(' / '))} <span class="badge muted badge-sm">${r.total.toLocaleString(loc())}</span></h3>
        <span class="section-hint">${esc(defs.map(d => `${d.explainer} ${d.next_step || ''}`).join(' '))}</span>
        <div class="section-actions"><button type="button" class="btn btn-secondary btn-sm" ${act('closeKbDrilldown')}>${th('common.close')}</button></div>
      </div>
      <div class="section-body">
        ${r.items.length ? r.items.map(it => _kbItemCard(it)).join('') : emptyHtml(th('kb.nothing_here'))}
        ${pagerHtml({offset, limit: KB_PAGE, total: r.total, page: o => act('loadKbItems', cat, states, o)})}
      </div>
    </section>`;
    if (fresh) el.scrollIntoView({behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'nearest'});
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
  if (!el.dataset.loaded) el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('common.loading')}</p>`;
  try {
    const q = new URLSearchParams({offset: String(offset), limit: String(KB_ATT_PAGE)});
    if (reason) q.set('reason', reason);
    const r = await api(`/api/enrichment/unmatched?${q.toString()}`);
    el.dataset.loaded = '1';
    const defs = r.reason_definitions || {};
    const chip = (key, label, n) => `<button type="button" class="chip${(reason || '') === key ? ' active' : ''}" ${act('loadKbAttention', 0, key || null)} title="${escAttr(defs[key]?.explainer || t('kb.everything_human'))}">${esc(label)} <span class="t3">${Number(n || 0).toLocaleString(loc())}</span></button>`;
    const chips = [chip('', t('common.all'), r.total_all ?? r.total)]
      .concat(Object.entries(r.by_reason || {}).map(([k, n]) => chip(k, defs[k]?.label || k, n))).join('');
    const badgesFor = it => (it.reasons || []).map(k => `<span class="badge amber badge-sm" title="${escAttr(defs[k]?.explainer || '')}">${esc(defs[k]?.label || k)}</span>`).join('');
    const findingsNote = Object.keys(r.findings_summary || {}).length
      ? ` ${th('kb.open_findings')} ${Object.entries(r.findings_summary).map(([k, n]) => `${esc(defs[k]?.label || k)} ${n}`).join(' · ')}.` : '';
    const empty = reason ? emptyHtml(th('kb.nothing_in_group')) : emptyHtml(th('kb.nothing_needs_human'), null, null, {good: true});
    el.innerHTML = `<section class="section">
      <div class="section-head">
        <h3>${th('kb.needs_attention')} <span class="badge amber badge-sm">${r.total.toLocaleString(loc())}</span></h3>
        <span class="section-hint">${reason ? esc(defs[reason]?.label || reason) : th('kb.attention_hint')}</span>
      </div>
      <div class="section-body">
        <p class="fs-12 t3 mb-8">${th('kb.attention_intro')}${findingsNote}</p>
        <div class="row mb-12">${chips}</div>
        ${r.items.length ? r.items.map(it => _kbItemCard(it, badgesFor(it))).join('') : empty}
        ${pagerHtml({offset, limit: KB_ATT_PAGE, total: r.total, page: o => act('loadKbAttention', o, reason || null)})}
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
    else { toast(r.error || t('kb.update_failed'), 'danger'); btnDone(btn); }
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
export function kbRetry(btn)    { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/retry`, 'POST', t('kb.attempts_reset')); }
export function kbIgnore(btn)   { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/ignore`, 'POST', t('kb.ignored_toast')); }
export function kbUnignore(btn) { return _kbAction(btn, `/api/enrichment/items/${encodeURIComponent(btn.dataset.svc)}/${Number(btn.dataset.id)}/ignore`, 'DELETE', t('kb.back_in_queue')); }

export function kbFixMatch(btn) {
  const {svc, id, title, year, cat} = btn.dataset;
  openMatchPicker({service: svc, arrId: Number(id), title, year: year ? Number(year) : null, category: cat || undefined, onDone: _kbRefresh});
}

// ── DATA CUSTODIAN (debt-based maintenance — replaces the button zoo) ────────
export function _renderCustodianBar(c) {
  const el = document.getElementById('custodian-bar');
  if (!el) return;
  // Tasks that run on every tick are always "due" between two ticks.
  const due = (c.tasks || []).filter(x => x.due && !x.every_tick).length;
  // Two queues since 2026-09-27: model work and everything else, side by
  // side. Name what each one is on, so a long deletion scan no longer reads
  // as "maintenance is stuck".
  const Q = c.queues || {};
  const labelOf = id => ((c.tasks || []).find(x => x.job_id === id) || {}).label || id;
  const running = [['model', t('kb.q_gpu')], ['background', t('kb.q_other')]]
    .filter(([q]) => Q[q] && Q[q].busy)
    .map(([q, name]) => `${name}: ${Q[q].current ? labelOf(Q[q].current) : t('kb.starting_lc')}`);
  const allBusy = !!(Q.model && Q.model.busy && Q.background && Q.background.busy);
  let line;
  if (c.ticking) {
    line = running.length ? t('kb.running_list', {list: running.join(' · ')}) : t('kb.maint_running');
  } else if (c.report && c.report.ts) {
    const ago = Math.max(0, Math.round((Date.now() - new Date(c.report.ts + 'Z').getTime()) / 60000));
    const acts = (c.report.actions || []);
    const summary = acts.length
      ? acts.map(a => `${a.task.replace('custodian_', '')}: ${a.result}`).join(' · ')
      : t('kb.nothing_due');
    line = t('kb.last_run_line', {n: ago, s: c.report.duration_s, summary});
  } else {
    line = t('kb.no_run_yet');
  }
  // Keep working (2026-10-06): a sprint until a set time, two-minute ticks,
  // the large budgets, the PC kept from idle-sleeping. Stop ends it early.
  const sprint = c.sprint && c.sprint.until ? new Date(c.sprint.until + 'Z') : null;
  // "stays awake" only where the server can keep its machine awake (Windows);
  // a container has nothing to keep awake.
  const awake = c.sprint && c.sprint.awake ? t('kb.stays_awake') : '';
  if (sprint) line = `${t('kb.keep_working_until', {time: sprint.toLocaleTimeString(loc(), {hour: '2-digit', minute: '2-digit'})})}${awake} · ${line}`;
  const sprintCtl = sprint
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('stopSprint', EL)} title="${th('kb.stop_sprint_title')}">${th('kb.stop')}</button>`
    : `<select id="sprint-hours" class="input w-70" aria-label="${th('kb.sprint_hours_aria')}">${[2, 4, 6, 8, 10, 12].map(h => `<option value="${h}"${h === 10 ? ' selected' : ''}>${h} h</option>`).join('')}</select>
       <button type="button" class="btn btn-secondary btn-sm" ${act('startSprint', EL)} title="${th('kb.keep_working_title')}">${th('kb.keep_working')}</button>`;
  el.innerHTML = `<section class="section">
    <div class="section-head">
      <h3>${th('kb.custodian')} ${due ? `<span class="badge amber badge-sm">${th('kb.tasks_due', {n: due})}</span>` : `<span class="badge muted badge-sm">${th('kb.all_caught_up')}</span>`}</h3>
      <span class="section-hint" title="${th('kb.custodian_title')}">${esc(line)}</span>
      <div class="section-actions">${sprintCtl}<button type="button" class="btn btn-secondary btn-sm" ${act('runMaintenance', EL)}${allBusy ? ' disabled' : ''}>${th('kb.run_maintenance')}</button></div>
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
  catch (e) {
    // Keep the panel (and its Stop button): removing it on a failed read
    // looked exactly like "the backfill is finished". One line says why.
    const msg = `${SVG_WARN} ${th('kb.backfill_unreadable', {error: _errMsg(e)})}`;
    const line = el.querySelector('.backfill-err');
    if (line) line.innerHTML = msg;
    else el.insertAdjacentHTML('beforeend', `<p class="load-err fs-11 mt-4 backfill-err" role="status">${msg}</p>`);
    return;
  }
  if (!d || !d.any_offer) { el.innerHTML = ''; return; }   // nothing worth offering
  const rows = d.sources.filter(s => s.offer).map(s => `
    <div class="panel-item${s.running ? ' live' : ''}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(s.label)}${s.running ? `<span class="badge amber badge-sm">${th('kb.running')}</span>` : ''}</div>
        <div class="panel-actions">${s.running
          ? `<button type="button" class="btn btn-secondary btn-sm" ${act('stopBackfill', s.key, EL)}>${th('kb.stop')}</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" ${act('startBackfill', s.key, EL)}>${th('kb.fetch_now')}</button>`}</div>
      </div>
      <div class="panel-item-sub">${esc(s.blurb)}</div>
      <div class="panel-item-foot">
        <div class="mt-0 progress-bar"><div class="progress-fill" data-style="width:${Number(s.pct) || 0}%"></div></div>
        <div class="fs-11 t3 mt-4">${th('kb.pct', {pct: s.pct})} · ${th('kb.to_go', {n: s.missing.toLocaleString(loc())})}</div>
      </div>
    </div>`).join('');
  el.innerHTML = `<section class="section">
    <div class="section-head"><h3>${th('kb.backfill')}</h3><span class="section-hint">${th('kb.backfill_hint', {pct: d.threshold_pct})}</span></div>
    <div class="section-body">${rows}</div></section>`;
}

export async function startBackfill(source, btn) {
  btnBusy(btn, t('kb.starting'));
  try { await api('/api/enrichment/backfill/' + encodeURIComponent(source), 'POST'); toast(t('kb.backfill_started'), 'success'); }
  catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  // The panel re-reads coverage, so a finished source removes its own row.
  setTimeout(loadBackfillPanel, 1500);
}

export async function stopBackfill(source, btn) {
  btnBusy(btn, t('kb.stopping'));
  try { await api('/api/enrichment/backfill/' + encodeURIComponent(source) + '/stop', 'POST'); toast(t('kb.stop_requested'), 'info'); }
  catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadBackfillPanel, 1200);
}

export async function runMaintenance(btn) {
  btnBusy(btn, t('kb.starting'));
  try {
    const r = await api('/api/enrichment/custodian/run', 'POST');
    if (r.status === 'started') toast(t('kb.maint_started'), 'success');
    else toast(t('kb.maint_already'), 'info');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadEnrichStatus, 4000);
}

// Keep working: the custodian's sprint (data_custodian.set_sprint).
export async function startSprint(btn) {
  const hours = Number(document.getElementById('sprint-hours')?.value) || 10;
  btnBusy(btn, t('kb.starting'));
  try {
    const r = await api(`/api/enrichment/custodian/sprint?hours=${hours}`, 'POST');
    if (r.until) toast(t('kb.keeping_at_it', {h: hours}) + (r.awake ? ` ${t('kb.awake_meanwhile')}` : ''), 'success');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadEnrichStatus, 3000);
}

export async function stopSprint(btn) {
  btnBusy(btn, t('kb.stopping'));
  try {
    await api('/api/enrichment/custodian/sprint?hours=0', 'POST');
    toast(t('kb.normal_rhythm'), 'info');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
  setTimeout(loadEnrichStatus, 3000);
}
