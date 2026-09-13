// ── MUSIC PIPELINE ────────────────────────────────────────────────────────────
import { api } from './api.js';
import { CAT_LABELS, _errHtml, _errMsg, btnBusy, btnDone, confirmDialog, esc, toast } from './ui.js';
import { loadEnrichStatus } from './kb.js';
import { showView } from './nav.js';
let _musicPollTimer = null;

export async function loadMusicStatus() {
  try {
    const s = await api('/api/music/status');
    _renderMusicStatus(s);
  } catch(e) {
    document.getElementById('music-stats-bar').textContent = 'Could not load music stats: ' + _errMsg(e);
  }
}

export function _renderMusicStatus(s) {
  const stats = s.stats || {};
  const running = s.running;
  const prog = s.progress || {};

  // Stats bar
  const bar = document.getElementById('music-stats-bar');
  bar.innerHTML = [
    `<b>${stats.total_music ?? '—'}</b> total plays`,
    `<b>${stats.source_spotify ?? '—'}</b> from Spotify`,
    `<b>${stats.unmatched_spotify ?? '—'}</b> unmatched`,
    `<b>${stats.missing_genres ?? '—'}</b> missing genres`,
    s.last_run ? `last run ${new Date(s.last_run).toLocaleString()}` : 'never run',
  ].join(' · ');

  // Running badge + the one Start/Stop slot
  document.getElementById('music-running-badge').hidden = !running;
  document.getElementById('music-start-btn').hidden = running;
  document.getElementById('music-stop-btn').hidden = !running;

  // Progress: the bar is a journey across the phases, not a percentage of one
  const progBar   = document.getElementById('music-progress-bar');
  const progLabel = document.getElementById('music-progress-label');
  const progFill  = document.getElementById('music-progress-fill');
  if (running || (prog.phase && prog.phase !== 'done')) {
    progBar.hidden = false;
    const [label, width] = _musicPhase(prog);
    progLabel.textContent = label;
    progFill.style.width = width + '%';
  } else {
    progBar.hidden = true;
  }

  // Auto-poll while running
  if (running) {
    if (!_musicPollTimer) _musicPollTimer = setInterval(loadMusicStatus, 4000);
  } else if (_musicPollTimer) {
    clearInterval(_musicPollTimer); _musicPollTimer = null;
  }
}

// One entry per pipeline phase: [label, bar position]. Phase 1.4 (MusicBrainz
// at ~1 req/s) used to leave the UI silent for minutes — it reports too.
const MUSIC_PHASES = {
  plex_match:        () => ['Phase 1: matching plays to Plex…', 15],
  plex_match_done:   p => [`Phase 1 done — matched ${p.matched ?? 0}, unmatched ${p.unmatched ?? 0}. Resolving MBIDs…`, 20],
  mbid_resolve:      p => [p.batch_size ? `Phase 1.4: MusicBrainz — ${p.queried ?? 0} / ${p.batch_size} artists (${p.resolved ?? 0} resolved, ${p.pct ?? 0}%)` : 'Phase 1.4: resolving artist MBIDs…',
                           Math.min(20 + Math.round(7 * (p.pct ?? 0) / 100), 27)],
  mbid_resolve_done: p => [`Phase 1.4 done — ${p.resolved ?? 0} MBIDs resolved. Starting Spotify…`, 27],
  spotify_enrich:    () => ['Phase 1.5: fetching genres from Spotify…', 28],
  lastfm_enrich:     p => {
    const q = p.tracks_queried ?? 0, t = p.total_unique ?? 0, pct = Math.min(100, p.pct ?? (t ? Math.round(100 * q / t) : 0));
    return [t ? `Phase 2: Last.fm — ${q} / ${t} tracks (${pct}%)` : `Phase 2: Last.fm — ${q} tracks queried…`, Math.min(35 + Math.round(64 * pct / 100), 99)];
  },
  lastfm_batch_done: p => [`Batch done${p.spotify_enriched > 0 ? ` · Spotify: ${p.spotify_enriched}` : ''} · Last.fm: ${p.tracks_queried ?? 0} queried, ${p.enriched_plays ?? 0} enriched`
                           + `${p.artist_fallback > 0 ? ` (${p.artist_fallback} via artist fallback)` : ''}${p.no_data > 0 ? `, ${p.no_data} cached as no-data` : ''}.`, 100],
  done:              p => [`Done — Plex: ${p.matched ?? 0} matched`
                           + `${(p.mbid_resolved > 0 || p.mbid_remaining > 0) ? ` · MBIDs: ${p.mbid_resolved ?? 0} resolved${p.mbid_remaining > 0 ? `, ${p.mbid_remaining} pending` : ''}` : ''}`
                           + `${p.spotify_enriched > 0 ? ` · Spotify: ${p.spotify_enriched}` : ''} · Last.fm: ${p.enriched_plays ?? 0} plays enriched`
                           + `${p.artist_fallback > 0 ? ` · ${p.artist_fallback} via artist fallback` : ''}${p.no_data > 0 ? ` · ${p.no_data} no-data cached` : ''}`, 100],
};
export function _musicPhase(p) {
  const f = MUSIC_PHASES[p.phase || ''];
  return f ? f(p) : ['Starting…', 5];
}

export async function startMusicPipeline(btn) {
  const batch = parseInt(document.getElementById('music-batch-input').value) || 300;
  btnBusy(btn, 'Starting…');
  try { await api('/api/music/start', 'POST', { batch }); toast('Music pipeline started', 'success'); await loadMusicStatus(); }
  catch (e) { toast(_errMsg(e), 'danger'); }
  btnDone(btn);
}

export async function stopMusicPipeline(btn) {
  btnBusy(btn, 'Stopping…');
  try { await api('/api/music/stop', 'POST'); toast('Stop requested — the current batch finishes first', 'info'); await loadMusicStatus(); }
  catch (e) { toast(_errMsg(e), 'danger'); }
  btnDone(btn);
}

// Manual runs: the pressed button shows busy, the outcome is a toast; the
// status line under the toolbar keeps only what outlives a toast (a scan
// result, a load error).
export function _enrichCats() { return ['music','movie','show','anime'].filter(c => document.getElementById(`enrich-cat-${c}`)?.checked); }
export function _enrichSrc()  { return document.querySelector('input[name="enrich-src"]:checked')?.value || 'watch_history'; }

export async function startEnrichNew(btn) {
  const cats = _enrichCats();
  const el = document.getElementById('enrich-progress-area');
  btnBusy(btn, 'Starting…');
  try {
    await api('/api/enrichment/start', 'POST', {categories: cats, source: _enrichSrc()});
    toast(`Enrichment started for ${cats.map(c => CAT_LABELS[c] || c).join(', ')}`, 'success');
    el.innerHTML = '';
    setTimeout(loadEnrichStatus, 3000);
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

export async function startEnrichForce(btn) {
  const cats = _enrichCats();
  if (!cats.length) { toast('Select at least one library first.', 'amber'); return; }
  const labels = cats.map(c => CAT_LABELS[c] || c).join(', ');
  const res = await confirmDialog({
    title: 'Force re-enrich', danger: true, confirmLabel: 'Clear and re-enrich',
    body: `<p>Every existing profile for <b>${esc(labels)}</b> is cleared and re-processed from scratch.</p><p class="t3 fs-12 mt-8">Use this after changing the model or the enrichment prompts.</p>`,
  });
  if (!res.ok) return;
  const el = document.getElementById('enrich-progress-area');
  btnBusy(btn, 'Working…');
  try {
    await api('/api/enrichment/start', 'POST', {categories: cats, source: _enrichSrc(), force: true});
    toast(`Force re-enrichment started for ${labels} — cache cleared`, 'amber');
    el.innerHTML = '';
    setTimeout(loadEnrichStatus, 3000);
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

export async function computeTaste(btn) {
  const el = document.getElementById('enrich-progress-area');
  // Pass 62: category-aware. Reads the same enrich-cat-* checkboxes that
  // Start Enrichment / Force Re-Enrich use, instead of always recomputing
  // all four. The /compute-taste endpoint already accepted a `categories`
  // query param — only the frontend never sent it. Empty selection → omit
  // the param → backend recomputes everything (matches startEnrichNew's
  // "no checkboxes = all" behaviour). Lets the user recompute the big
  // movie/show/anime backlog without touching the music vectors.
  const cats = _enrichCats();
  const label = cats.length ? cats.map(c => CAT_LABELS[c] || c).join(', ') : 'all categories';
  btnBusy(btn, 'Starting…');
  try {
    const qs = cats.map(c => `categories=${encodeURIComponent(c)}`).join('&');
    await api(qs ? `/api/enrichment/compute-taste?${qs}` : '/api/enrichment/compute-taste', 'POST');
    toast(`Taste vector computation started for ${label}`, 'success');
    setTimeout(() => showView('history', document.querySelector('.sb-item[onclick*=history]')), 3000);
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

// Pass 55: two-step audit-requeue. First a dry-run that only counts, so the
// user sees exactly what's about to be touched; then — on confirm — the real
// requeue (cache rows dropped + EnrichmentStatus flipped to pending). The
// next enrichment run picks the items up; this button does NOT start a run
// itself (deliberate — see the Pass-55 design decision).
export async function auditRequeueEnrichments(btn) {
  const el = document.getElementById('enrich-progress-area');
  btnBusy(btn, 'Scanning…');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Scanning enrichment cache for incomplete or mis-resolved metadata…</p>';

  const _reasonLabel = {
    zero_rating: 'missing/zero rating', not_found: 'not found in any API', malformed: 'corrupt cache entry',
    'wrong_entity:title': 'wrong entity (title mismatch)',
    'wrong_entity:year': 'wrong entity (year mismatch)',
    'wrong_entity:mbid': 'wrong entity (artist mismatch)',
  };
  const _fmtBreakdown = (by) => Object.entries(by || {})
    .map(([k, n]) => `${n}× ${_reasonLabel[k] || k}`).join(', ');

  try {
    // Step 1 — dry run: scan + count only, change nothing.
    const scan = await api('/api/enrichment/audit-requeue?dry_run=true', 'POST');
    if (!scan.incomplete) {
      el.innerHTML = `<p class="fs-13 t-success">Scanned ${scan.scanned} profiles — all have complete metadata, nothing to requeue.</p>`;
      return;
    }
    const res = await confirmDialog({
      title: 'Requeue flagged profiles', confirmLabel: `Requeue ${scan.incomplete}`,
      body: `<p>Scanned <b>${scan.scanned}</b> enrichment profiles; <b>${scan.incomplete}</b> need re-enrichment: ${esc(_fmtBreakdown(scan.by_reason))}.</p>
             <p class="t3 fs-12 mt-8">Their cached profiles are dropped and they are marked pending. The next enrichment run re-fetches them — this does not start a run itself.</p>`,
    });
    if (!res.ok) {
      el.innerHTML = `<p class="fs-13 t3">Audit cancelled — ${scan.incomplete} flagged profiles left untouched.</p>`;
      return;
    }
    // Step 2 — real requeue.
    el.innerHTML = '<p class="loading" role="status" aria-live="polite">Requeuing incomplete profiles…</p>';
    const r = await api('/api/enrichment/audit-requeue?dry_run=false', 'POST');
    el.innerHTML = `<p class="fs-13 t-success">Requeued ${r.requeued} of ${r.incomplete} flagged profiles (${esc(_fmtBreakdown(r.by_reason))}). Run "Start Enrichment" to re-fetch them.</p>`;
    setTimeout(loadEnrichStatus, 2000);
  } catch (e) {
    el.innerHTML = _errHtml(e);
  } finally {
    btnDone(btn);
  }
}

// Bulk OMDb backfill — tops up writer / awards / extended plot for every cached
// title that has an imdb_id but never got OMDb (it needs an imdb_id and was long
// rate-limited, so only ~1/3 of the library has those fields). Background job.
export async function omdbBackfill(btn) {
  const el = document.getElementById('enrich-progress-area');
  const res = await confirmDialog({
    title: 'Backfill OMDb', confirmLabel: 'Start backfill',
    body: '<p>Fetches writer, awards and the extended plot for every cached title with an imdb_id but no OMDb data yet.</p><p class="t3 fs-12 mt-8">Runs in the background over the supporter key — progress shows in Activity.</p>',
  });
  if (!res.ok) return;
  btnBusy(btn, 'Starting…');
  try {
    await api('/api/enrichment/omdb-backfill', 'POST');
    toast('OMDb backfill started — progress shows in Activity', 'success');
    el.innerHTML = '';
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}
