// ── MUSIC PIPELINE ────────────────────────────────────────────────────────────
import { api } from './api.js';
import { CAT_LABELS, _errHtml, _errMsg, btnBusy, btnDone, confirmDialog, esc, loc, toast } from './ui.js';
import { loadEnrichStatus } from './kb.js';
import { showView } from './nav.js';
import { t, th } from './i18n.js';
let _musicPollTimer = null;

export async function loadMusicStatus() {
  try {
    const s = await api('/api/music/status');
    _renderMusicStatus(s);
  } catch(e) {
    document.getElementById('music-stats-bar').textContent = t('music.stats_failed', {error: _errMsg(e)});
  }
}

export function _renderMusicStatus(s) {
  const stats = s.stats || {};
  const running = s.running;
  const prog = s.progress || {};

  // Stats bar
  const bar = document.getElementById('music-stats-bar');
  bar.innerHTML = [
    `<b>${stats.total_music ?? '—'}</b> ${th('music.total_plays')}`,
    `<b>${stats.source_spotify ?? '—'}</b> ${th('music.from_spotify')}`,
    `<b>${stats.unmatched_spotify ?? '—'}</b> ${th('music.unmatched')}`,
    `<b>${stats.missing_genres ?? '—'}</b> ${th('music.missing_genres')}`,
    s.last_run ? th('music.last_run', {when: new Date(s.last_run).toLocaleString(loc())}) : th('music.never_run'),
  ].join(' · ');
  // Lyrics from Plex: the SoulSync sidecars on file and the artists profiled from them.
  const ly = s.lyrics;
  const lyEl = document.getElementById('music-lyrics-line');
  if (lyEl) {
    lyEl.hidden = !ly || !ly.tracks;
    if (ly && ly.tracks) {
      lyEl.textContent = t('music.lyrics_on_file', {x: ly.with_text.toLocaleString(loc()), of: ly.tracks.toLocaleString(loc())})
        + ` · ${t('music.lyrics_profiled', {x: ly.profiles.toLocaleString(loc()), of: ly.artists_with_text.toLocaleString(loc())})}`
        + (ly.explicit_artists ? ` · ${t('music.lyrics_explicit', {n: ly.explicit_artists})}` : '')
        + (ly.unreachable ? ` · ${t('music.lyrics_unreachable', {n: ly.unreachable})}` : '')
        + (ly.last_run ? ` · ${t('music.lyrics_collected', {when: new Date(ly.last_run + 'Z').toLocaleString(loc())})}` : '');
    }
  }

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
  plex_match:        () => [t('music.ph.plex_match'), 15],
  plex_match_done:   p => [t('music.ph.plex_match_done', {matched: p.matched ?? 0, unmatched: p.unmatched ?? 0}), 20],
  mbid_resolve:      p => [p.batch_size ? t('music.ph.mbid', {q: p.queried ?? 0, of: p.batch_size, resolved: p.resolved ?? 0, pct: p.pct ?? 0}) : t('music.ph.mbid_start'),
                           Math.min(20 + Math.round(7 * (p.pct ?? 0) / 100), 27)],
  mbid_resolve_done: p => [t('music.ph.mbid_done', {resolved: p.resolved ?? 0}), 27],
  spotify_enrich:    () => [t('music.ph.spotify'), 28],
  lastfm_enrich:     p => {
    const q = p.tracks_queried ?? 0, tot = p.total_unique ?? 0, pct = Math.min(100, p.pct ?? (tot ? Math.round(100 * q / tot) : 0));
    return [tot ? t('music.ph.lastfm', {q, of: tot, pct}) : t('music.ph.lastfm_open', {q}), Math.min(35 + Math.round(64 * pct / 100), 99)];
  },
  lastfm_batch_done: p => [`${t('music.ph.batch_done')}${p.spotify_enriched > 0 ? ` · Spotify: ${p.spotify_enriched}` : ''} · ${t('music.ph.lastfm_counts', {q: p.tracks_queried ?? 0, e: p.enriched_plays ?? 0})}`
                           + `${p.artist_fallback > 0 ? ` ${t('music.ph.via_fallback_paren', {n: p.artist_fallback})}` : ''}${p.no_data > 0 ? `, ${t('music.ph.no_data_cached', {n: p.no_data})}` : ''}.`, 100],
  done:              p => [t('music.ph.done', {matched: p.matched ?? 0})
                           + `${(p.mbid_resolved > 0 || p.mbid_remaining > 0) ? ` · ${t('music.ph.mbids', {n: p.mbid_resolved ?? 0})}${p.mbid_remaining > 0 ? `, ${t('music.ph.pending', {n: p.mbid_remaining})}` : ''}` : ''}`
                           + `${p.spotify_enriched > 0 ? ` · Spotify: ${p.spotify_enriched}` : ''} · ${t('music.ph.plays_enriched', {n: p.enriched_plays ?? 0})}`
                           + `${p.artist_fallback > 0 ? ` · ${t('music.ph.via_fallback', {n: p.artist_fallback})}` : ''}${p.no_data > 0 ? ` · ${t('music.ph.no_data', {n: p.no_data})}` : ''}`, 100],
};
export function _musicPhase(p) {
  const f = MUSIC_PHASES[p.phase || ''];
  return f ? f(p) : [t('music.starting'), 5];
}

export async function startMusicPipeline(btn) {
  const batch = parseInt(document.getElementById('music-batch-input').value) || 300;
  btnBusy(btn, t('music.starting'));
  try { await api('/api/music/start', 'POST', { batch }); toast(t('music.started'), 'success'); await loadMusicStatus(); }
  catch (e) { toast(_errMsg(e), 'danger'); }
  btnDone(btn);
}

export async function stopMusicPipeline(btn) {
  btnBusy(btn, t('music.stopping'));
  try { await api('/api/music/stop', 'POST'); toast(t('music.stop_requested'), 'info'); await loadMusicStatus(); }
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
  btnBusy(btn, t('music.starting'));
  try {
    await api('/api/enrichment/start', 'POST', {categories: cats, source: _enrichSrc()});
    toast(t('music.enrich_started', {list: cats.map(c => CAT_LABELS[c] || c).join(', ')}), 'success');
    el.innerHTML = '';
    setTimeout(loadEnrichStatus, 3000);
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}

export async function startEnrichForce(btn) {
  const cats = _enrichCats();
  if (!cats.length) { toast(t('music.select_library'), 'amber'); return; }
  const labels = cats.map(c => CAT_LABELS[c] || c).join(', ');
  const res = await confirmDialog({
    title: t('music.force_title'), danger: true, confirmLabel: t('music.force_confirm'),
    body: `<p>${th('music.force_body_pre')} <b>${esc(labels)}</b> ${th('music.force_body_post')}</p><p class="t3 fs-12 mt-8">${th('music.force_hint')}</p>`,
  });
  if (!res.ok) return;
  const el = document.getElementById('enrich-progress-area');
  btnBusy(btn, t('music.working'));
  try {
    await api('/api/enrichment/start', 'POST', {categories: cats, source: _enrichSrc(), force: true});
    toast(t('music.force_started', {list: labels}), 'amber');
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
  const label = cats.length ? cats.map(c => CAT_LABELS[c] || c).join(', ') : t('music.all_categories');
  btnBusy(btn, t('music.starting'));
  try {
    const qs = cats.map(c => `categories=${encodeURIComponent(c)}`).join('&');
    await api(qs ? `/api/enrichment/compute-taste?${qs}` : '/api/enrichment/compute-taste', 'POST');
    toast(t('music.taste_updating', {list: label}), 'success');
    setTimeout(() => showView('history'), 3000);
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
  btnBusy(btn, t('music.scanning'));
  el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('music.audit_scanning')}</p>`;

  // the reasons are server keys; their words are in the dictionary (music.reason.*)
  const _REASONS = ['zero_rating', 'not_found', 'malformed', 'wrong_entity:title', 'wrong_entity:year', 'wrong_entity:mbid'];
  const _reasonLabel = k => (_REASONS.includes(k) ? t(`music.reason.${k.replace(':', '_')}`) : k);
  const _fmtBreakdown = (by) => Object.entries(by || {})
    .map(([k, n]) => `${n}× ${_reasonLabel(k)}`).join(', ');

  try {
    // Step 1 — dry run: scan + count only, change nothing.
    const scan = await api('/api/enrichment/audit-requeue?dry_run=true', 'POST');
    if (!scan.incomplete) {
      el.innerHTML = `<p class="fs-13 t-success">${th('music.audit_clean', {n: scan.scanned})}</p>`;
      return;
    }
    const res = await confirmDialog({
      title: t('music.requeue_title'), confirmLabel: t('music.requeue_confirm', {n: scan.incomplete}),
      body: `<p>${th('music.requeue_body', {scanned: scan.scanned, n: scan.incomplete}).replace(/\{b\}(.*?)\{\/b\}/g, '<b>$1</b>')} ${esc(_fmtBreakdown(scan.by_reason))}.</p>
             <p class="t3 fs-12 mt-8">${th('music.requeue_hint')}</p>`,
    });
    if (!res.ok) {
      el.innerHTML = `<p class="fs-13 t3">${th('music.audit_cancelled', {n: scan.incomplete})}</p>`;
      return;
    }
    // Step 2 — real requeue.
    el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('music.requeuing')}</p>`;
    const r = await api('/api/enrichment/audit-requeue?dry_run=false', 'POST');
    el.innerHTML = `<p class="fs-13 t-success">${th('music.requeued', {n: r.requeued, of: r.incomplete, why: _fmtBreakdown(r.by_reason)})}</p>`;
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
    title: t('music.omdb_title'), confirmLabel: t('music.omdb_confirm'),
    body: `<p>${th('music.omdb_body')}</p><p class="t3 fs-12 mt-8">${th('music.omdb_hint')}</p>`,
  });
  if (!res.ok) return;
  btnBusy(btn, t('music.starting'));
  try {
    await api('/api/enrichment/omdb-backfill', 'POST');
    toast(t('music.omdb_started'), 'success');
    el.innerHTML = '';
  } catch (e) { el.innerHTML = _errHtml(e); }
  btnDone(btn);
}
