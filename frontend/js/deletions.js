// ── DELETIONS ─────────────────────────────────────────────────────────────────
// Moved state.currentDelCategory to state
// Client-side cache for the full proposals list from the last "All" fetch.
// Tab switches filter this in-memory instead of making redundant API calls.
// Invalidated whenever a targeted single-category analysis runs (because that
// category's slice would then be out of sync with the stored full list).
// Moved state._delProposalsAll to state
import { skeletonHtml, EL, EVENT, SVG_CHECK, SVG_TRASH, SVG_WARN, haptic, _errHtml, _errMsg, _fmtAbs, _fmtRel, _posterImg, act, actOn, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, menuHtml, setPulse, toast } from './ui.js';
import { state } from './state.js';
import { openMatchPicker } from './picker.js';
import { api } from './api.js';
import { _busyStreaming, enterThread, proposalOpener, sendMessage } from './chat.js';
import { tabToHash } from './nav.js';
import { t, th } from './i18n.js';

// The service a proposal deletes through, as its product spells it: the card
// printed the raw key ("radarr · frees 3.3 GB", UI audit 2026-11-14).
const _SERVICE_NAMES = {radarr: 'Radarr', sonarr: 'Sonarr', lidarr: 'Lidarr', plex: 'Plex', jellyfin: 'Jellyfin'};
export function serviceName(s) { return _SERVICE_NAMES[s] || s || ''; }

// Proposals deleted through the media server itself (music without Lidarr,
// or rebound from Plex): no arr entity to re-match, no import list.
const _MEDIA_SERVERS = new Set(['plex', 'jellyfin']);

// Disk space in the unit a person would say: 820 MB, 4.2 GB, 37 GB, 1.3 TB.
function _fmtSize(gb) {
  gb = Number(gb);
  if (!gb) return '';
  if (gb >= 1000) return `${(gb / 1000).toFixed(1)} TB`;
  if (gb < 1) return `${Math.round(gb * 1024)} MB`;
  return `${gb.toFixed(gb < 10 ? 1 : 0)} GB`;
}

// Pass 17: small badge showing "added Xd ago" when the proposal has a
// recent file-import timestamp. Threshold of 30 days keeps the badge
// signal-rich — older items would just be noise next to "30%/40%/50%"
// confidence values.
export function _recentActivityBadge(p) {
  if (!p.latest_activity_at) return '';
  const dt = new Date(p.latest_activity_at);
  if (isNaN(dt.getTime())) return '';
  const ageMs   = Date.now() - dt.getTime();
  const ageDays = Math.floor(ageMs / 86400000);
  if (ageDays > 30) return '';
  const label = ageDays <= 0 ? t('del.recent.today')
              : ageDays === 1 ? t('del.recent.yesterday')
              : ageDays <= 7  ? t('del.recent.days', {n: ageDays})
              :                 t('del.recent.weeks', {n: Math.floor(ageDays / 7)});
  // Recent activity = small amber "new" tag. Small enough not to compete
  // with the confidence pill, distinct enough to spot at a glance.
  return `<span class="badge amber badge-sm" title="${th('del.recent.title', {n: ageDays})}">${th('del.recent.badge', {label})}</span>`;
}

// "Why?" — the deletion score taken apart (engine.deletion_score_factors).
// The percentage it replaces was that score /100, clamped: a ranking, not a
// confidence, and it said nothing about WHY. A disclosure (button +
// aria-expanded + an in-flow panel) rather than a hover popover, so it works
// by touch and keyboard. Rows written before the breakdown existed show none.
const _WHY_TOP = 3;
// recommendations_engine.SHORTLIST_SCORE; tests/test_score_factors.py pins the two.
const _SHORTLIST_SCORE = 30;
// A guard verdict (the household floor) is a "why" of its own: it shows the
// button even on a row with no factor breakdown, and its sentence leads the panel.
export function _whyButton(p) {
  if (!p.score_factors?.factors?.length && !p.score_factors?.guard) return '';
  return `<button type="button" class="btn-why" aria-expanded="false" aria-controls="why-${p.id}" ${act('toggleWhy', EL)}>${th('del.why')}</button>`;
}
export function _whyPanel(p) {
  const f = p.score_factors?.factors || [];
  const guard = p.score_factors?.guard;
  if (!f.length && !guard) return '';
  const top = [...f].sort((a, b) => Math.abs(b.points) - Math.abs(a.points)).slice(0, _WHY_TOP);
  const rest = f.length - top.length;
  return `<div class="why-panel mt-8" id="why-${p.id}" tabindex="-1" hidden>
    ${guard ? `<div class="fs-12 t2 mb-6">${esc(_yourCallWhy(p))}.</div>` : ''}
    ${f.length ? `<div class="fs-12 t2 mb-6">${th('del.why_signals')}</div>
    <ul class="why-list">${top.map(x => `<li><span class="why-pts ${x.points > 0 ? 'to-delete' : 'to-keep'}" title="${th(x.points > 0 ? 'del.why_to_delete' : 'del.why_to_keep')}">${x.points > 0 ? '+' : '−'}${Math.round(Math.abs(x.points))}</span> ${esc(x.label)}</li>`).join('')}</ul>
    <div class="fs-11 t3 mt-6">${th(rest > 0 ? 'del.why_score_from' : 'del.why_score', {score: Math.round(p.score_factors.total), n: f.length})}. ${th('del.why_shortlist', {min: _SHORTLIST_SCORE})}</div>` : ''}
  </div>`;
}
// "Your call" has three causes: the curator found the title merely fine, or
// it wanted a cut that a floor overruled (score_factors.guard, set by
// recommendations_engine._with_guard) — someone else in the household engaged
// with it, or the title carries a major honour (the stature floor).
// Different reasons, different tooltips.
export function _yourCallWhy(p) {
  if (p.score_factors?.guard === 'household_floor') return t('del.your_call.household');
  if (p.score_factors?.guard === 'stature_floor') return t('del.your_call.stature');
  return t('del.your_call.fine');
}
export function toggleWhy(btn) {
  const panel = document.getElementById(btn.getAttribute('aria-controls'));
  if (!panel) return;
  const open = btn.getAttribute('aria-expanded') !== 'true';
  btn.setAttribute('aria-expanded', String(open));
  panel.hidden = !open;
  haptic('light');
}
// Escape closes an open Why? panel when focus is on its button or inside it,
// and puts focus back on the button. One delegated listener for every card.
export function _whyEscape(ev) {
  if (ev.key !== 'Escape' || !ev.target?.closest) return;
  let btn = ev.target.closest('.btn-why[aria-expanded="true"]');
  if (!btn) {
    const panel = ev.target.closest('.why-panel:not([hidden])');
    if (panel) btn = document.querySelector(`.btn-why[aria-controls="${panel.id}"]`);
  }
  if (!btn || btn.getAttribute('aria-expanded') !== 'true') return;
  ev.preventDefault();
  toggleWhy(btn);
  btn.focus();
}
if (typeof document !== 'undefined') document.addEventListener('keydown', _whyEscape);

// The pool behind the list is paged (routers/recommendations._proposal_page):
// `page` is the server's {total, total_gb} for the whole tab, so the header
// counts the pool, not the ten on screen, and "Show more" appends the next ten.
function _delSummaryHtml(shown, page) {
  const gb = page?.total_gb ?? shown.reduce((s, p) => s + (p.size_gb || 0), 0);
  const count = page && page.total > shown.length ? t('del.count_of', {shown: shown.length, total: page.total}) : `${shown.length}`;
  return `${th('del.savings')} <b class="t-amber">${Number(gb).toFixed(1)} GB</b> · ${th('del.proposals', {n: page?.total ?? shown.length, count})}`;
}
function _delMoreHtml(shown, page) {
  const left = page ? page.total - shown.length : 0;
  return left > 0
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('loadMoreDeletions', EL)}>${th('del.show_more', {n: Math.min(left, _DEL_PAGE)})}</button>`
    : '';
}
const _DEL_PAGE = 10;

export function _renderDeletionProposals(proposals, page = null) {
  const el = document.getElementById('del-content');
  state._delProposalsAll = proposals?.length ? [...proposals] : null;
  state._delPage = page;
  if (!proposals?.length) {
    el.innerHTML = emptyHtml(`${th('del.empty_pre')} <b>${th('del.run_analysis')}</b> ${th('del.empty_post')}`, t('del.run_analysis'), act('loadDeletions', state.currentDelCategory || null, null, true));
    updateDelBulkCount();
    return;
  }
  el.innerHTML = `
    <div class="fs-13 t2 mb-12" id="del-summary">${_delSummaryHtml(proposals, page)}</div>
    <div id="del-cards">${proposals.map(_delCardHtml).join('')}</div>
    <div id="del-more" class="mt-12">${_delMoreHtml(proposals, page)}</div>`;
  // Fresh render = fresh selection: reset the bulk bar + master checkbox.
  const master = document.getElementById('del-select-all');
  if (master) master.checked = false;
  updateDelBulkCount();
  prefetchDeleteTargets(proposals.map(p => p.service));
}

// The next ten of the tab's pool, appended: the selection and the notes
// being typed above stay as they are.
export async function loadMoreDeletions(btn) {
  const shown = state._delProposalsAll || [];
  const category = state.currentDelCategory;
  btnBusy(btn, t('common.loading'));
  try {
    const r = await api(`/api/recommendations/deletions?refresh=false&offset=${shown.length}&limit=${_DEL_PAGE}`
                        + (category ? `&category=${category}` : '') + _delRecentParams());
    if (category !== state.currentDelCategory) return;
    const seen = new Set(shown.map(p => String(p.id)));
    const fresh = (r.proposals || []).filter(p => !seen.has(String(p.id)));
    state._delProposalsAll = [...shown, ...fresh];
    state._delPage = r;
    document.getElementById('del-cards')?.insertAdjacentHTML('beforeend', fresh.map(_delCardHtml).join(''));
    const sum = document.getElementById('del-summary');
    if (sum) sum.innerHTML = _delSummaryHtml(state._delProposalsAll, r);
    const more = document.getElementById('del-more');
    if (more) more.innerHTML = _delMoreHtml(state._delProposalsAll, r);
    updateDelBulkCount();
    prefetchDeleteTargets(fresh.map(p => p.service));
  } catch (e) {
    toast(_errMsg(e), 'danger');
    btnDone(btn);
  }
}

// Card anatomy: poster (click = select for bulk) · head (title + badges,
// three visible actions, the rest behind More) · facts · pitch · the note,
// which saves itself when you click away. Actions run safe → destructive:
// Keep, Discuss, then Delete set apart — the leftmost slot is the habitual
// click and belongs to the safe choice. More holds the rarer actions (and
// is where Unmonitor will go).
function _delCardHtml(p) {
      // The server's pick (display_tags.py): genres minus what the tab already
      // says, rarest first, then up to two themes. Plain genres for a list
      // that came without them.
      const tags = Array.isArray(p.tags) ? p.tags
        : (p.genres||'').split(',').map(g=>g.trim()).filter(Boolean).slice(0,4).map(label => ({label, kind: 'genre'}));
      const genreTags = tags.map(tag => tag.kind === 'theme'
        ? `<span class="badge theme badge-sm" title="${th('del.theme')}">${esc(tag.label)}</span>`
        : `<span class="badge muted badge-sm">${esc(tag.label)}</span>`).join('');
      const placeholder = t(p.service==='lidarr' ? 'del.note_ph.lidarr' : p.service==='radarr' ? 'del.note_ph.radarr' : 'del.note_ph.other');
      const limbo = p.status === 'limbo';
      const ctx = `data-pid="${p.id}" data-title="${escAttr(p.title)}" data-pitch="${escAttr(p.pitch||p.reason||'')}" data-category="${escAttr(p.category||'')}" data-poster="${escAttr(p.poster_url||'')}" data-stagnant="${p.stagnant ? 1 : 0}"`;
      return `
      <div class="card mb-12${p.stagnant ? ' is-stagnant' : ''}" data-del-id="${p.id}" data-title="${escAttr(p.title)}" data-service="${escAttr(p.service || '')}">
        <div class="poster-card">
          <input type="checkbox" class="del-cb" data-id="${p.id}" data-gb="${p.size_gb||0}" ${actOn('change', 'onDelCheckbox', EL)} hidden title="${th('del.select_bulk')}"/>
          <div class="glow-interactive selectable${p.category==='music'?' is-music':''}" data-id="${p.id}" role="button" tabindex="0" aria-pressed="false" title="${th('del.select_bulk')}" aria-label="${th('del.select_title_bulk', {title: p.title})}" ${act('toggleDelSelect', p.id)} ${actOn('keydown', 'keyActivate', EVENT, EL)} title="${th('del.click_select')}">
            ${_posterImg(p.poster_url, 174, p.category==='music'?174:261, p.category==='music')}
            <div class="del-poster-check">${SVG_CHECK.replace('width="13" height="13"', 'width="34" height="34"')}</div>
          </div>
          <div class="grow">
            <div class="panel-item-head">
              <div class="fs-16 panel-item-title">${esc(p.title)}
                ${p.stagnant
                  ? `<span class="badge amber" title="${escAttr(_yourCallWhy(p))}">${th('del.your_call')}</span>`
                  : `<span class="badge muted" title="${th('del.cut_title')}">${th('del.cut')}</span>`}${_recentActivityBadge(p)}
                ${_whyButton(p)}
                ${limbo ? `<span class="badge amber" title="${th('del.parked_title')}">${th('del.parked')}</span>` : ''}
              </div>
              <div class="panel-actions">
                <button type="button" class="btn btn-secondary btn-sm" ${act('rejectDelete', p.id, EL)} title="${th('del.keep_title')}">${th('del.keep')}</button>
                <button type="button" class="btn btn-secondary btn-sm" ${act('onDiscussDeletion', EL)} ${ctx}>${th('del.discuss')}</button>
                <button type="button" class="btn btn-danger btn-sm ml-8" ${act('approveDelete', p.id, EL)}>${th(limbo ? 'del.retry_delete' : 'del.delete')}</button>
                ${menuHtml([
                  {label: t('del.challenge'), action: act('onReevaluateDeletion', EL), attrs: ctx, title: t('del.challenge_title')},
                  p.media_id && p.service && !_MEDIA_SERVERS.has(p.service) ? {label: t('del.fix_match'), action: act('onFixMatch', EL), attrs: `${ctx} data-service="${escAttr(p.service)}" data-mediaid="${escAttr(p.media_id)}"`, title: t('del.fix_match_title')} : null,
                  p.arr_url ? {label: t('del.open_in', {service: serviceName(p.service)}), href: p.arr_url} : null,
                ])}
              </div>
            </div>
            ${_whyPanel(p)}
            <div class="quote-text mt-8">${esc(p.pitch||p.reason||'')}</div>
            <div class="meta-quiet mt-8">${esc(serviceName(p.service))}${p.size_gb ? ` · ${th('del.frees')} <b>${_fmtSize(p.size_gb)}</b>` : ''}</div>
            ${genreTags ? `<div class="gap-5 row mt-8">${genreTags}</div>` : ''}
            ${p.synopsis ? `<div class="clamp-3 fs-12 t3 mt-8">${esc(p.synopsis)}</div>` : ''}
            ${limbo ? `<div class="fs-11 t-amber mt-4">${SVG_WARN} ${th('del.parked_note')}</div>` : ''}
            <textarea id="del-comment-${p.id}" class="del-comment" data-saved="${escAttr(p.user_comment||'')}" placeholder="${escAttr(placeholder)}" ${actOn('blur', 'saveComment', p.id)} ${actOn('keydown', 'blurOnCtrlEnter', EVENT, EL)}>${esc(p.user_comment||'')}</textarea>
            <div class="fs-11 t3 mt-4" id="del-note-hint-${p.id}">${p.user_comment ? th('del.note_saved') : ''}</div>
          </div>
        </div>
      </div>`;
}

// ── Fix match: owner-pinned entity resolution (same-named-twins repair) ──────
// Deletion cards open the same picker the Knowledge-Base page uses (the arr's
// own lookup + TMDB / AniList candidates, free id fields, "Not this one");
// the category is resolved server-side from the item, never invented here.
export function onFixMatch(btn) {
  const { title, category, service, mediaid } = btn.dataset;
  openMatchPicker({service, arrId: Number(mediaid), title, category: category || undefined,
                   onDone: () => { state._delProposalsAll = null; loadDeletions(state.currentDelCategory); }});
}

// Pass 17: read the "Just-arrived" toggle + days dropdown from the DOM.
// Keeping this in a helper means loadDeletions, toggleRecentOnly, and the
// dropdown's onchange all see the same source of truth.
export function _delRecentParams() {
  const cb   = document.getElementById('del-recent-only');
  const days = document.getElementById('del-recent-days');
  if (!cb || !cb.checked) return '';
  const d = parseInt(days?.value || '7', 10);
  return `&recent_only=true&recent_days=${d}`;
}

export function toggleRecentOnly(checked) {
  // Show/hide the days dropdown based on toggle state, then reload.
  const days = document.getElementById('del-recent-days');
  if (days) days.hidden = !checked;
  // The recent-only filter is a backend filter — the client-side
  // state._delProposalsAll cache is from the unfiltered query, so bypass it
  // by clearing. Re-loaded data re-populates as needed.
  state._delProposalsAll = null;
  loadDeletions(state.currentDelCategory);
}

// Pass 25: track an in-flight Analyse request globally so that
//   (a) the Deletions sidebar entry can pulse while it's running,
//   (b) if the user navigates away and comes back DURING the analyse,
//       we await the SAME promise instead of fetching the still-old DB
//       state and showing stale data right before the new commit lands.
// _delAnalysePromise is the awaitable; _delAnalyseStartedAt powers a
// "running since Xs" hint for long runs.
let _delAnalysePromise = null;
let _delAnalyseStartedAt = 0;

// The dot has two sources: this tab's own Analyse request (here) and the
// server's task stream (activity.js), so an analysis started in another
// tab — or before this page loaded — pulses here too.
export function _setAnalysePulse(on) {
  setPulse('sb-deletions-pulse', 'local', on);
}

// The last REQUEST wins, not the last response: a tab click or view switch
// racing an earlier load let the older answer repaint the list — and
// re-fill the "All" cache with a proposal an approve/keep had just evicted.
let _delLoadSeq = 0;

export async function loadDeletions(category=null, btn=null, refresh=false) {
  const seq = ++_delLoadSeq;
  state.currentDelCategory = category;
  tabToHash('deletions', category);     // #deletions/movie survives a reload
  // Restore the correct active tab whether called from a tab click, the Analyse
  // button, the Show Cached button, or a programmatic call like showView().
  document.querySelectorAll('#deletions-view .cat-tab').forEach(b => {
    const match = (b.dataset.cat || '') === (category || '');
    b.classList.toggle('active', btn ? b === btn : match);
  });
  const el = document.getElementById('del-content');
  _loadReturned(category);

  const recentParams = _delRecentParams();
  const recentActive = recentParams.length > 0;

  // ── Pass 25: piggy-back on an in-flight Analyse ──────────────────────────
  // If the user is coming back to this view while a previous Analyse is
  // still running, await the SAME promise and render its result — instead
  // of opening a parallel DB read that returns the pre-commit snapshot.
  if (!refresh && _delAnalysePromise) {
    const elapsed = Math.round((Date.now() - _delAnalyseStartedAt) / 1000);
    el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('del.still_running', {s: elapsed})}</p>`;
    try {
      const r = await _delAnalysePromise;
      if (seq !== _delLoadSeq) return;
      _renderEnrichmentCoverageBanner(r.enrichment_coverage);
      _renderRunBanner(r.last_run);
      _renderQueuedBanner(r.queued_note);
      _renderActivityBanner(r.activity_note);
      _renderDeletionProposals(r.proposals || [], r);
    } catch(e) {
      // Was the raw {"detail":…} body in grey "loading" type, with no way back in.
      if (seq === _delLoadSeq) el.innerHTML = _errHtml(e, act('loadDeletions', category || null, null, true));
    }
    return;
  }

  // Every tab asks the server for its own first page. Tab clicks used to
  // slice the "All" list in the browser, which held the top 30 of every
  // category together: the Anime tab showed the anime among them (9 of 50).
  el.innerHTML = skeletonHtml(t(refresh ? 'del.analysing' : 'del.loading_cached'));
  try {
    const url = `/api/recommendations/deletions?refresh=${refresh}`
              + (category ? `&category=${category}` : '')
              + recentParams;

    // Pass 25: if this is an Analyse, expose the promise so concurrent
    // view-switches can await it. The sidebar pulse turns on for the
    // duration. Cleared in a `finally` so a crash doesn't strand the dot.
    let promise;
    if (refresh) {
      _delAnalyseStartedAt = Date.now();
      _setAnalysePulse(true);
      promise = api(url);
      _delAnalysePromise = promise;
    } else {
      promise = api(url);
    }

    let r;
    try {
      r = await promise;
    } finally {
      if (refresh && _delAnalysePromise === promise) {
        _delAnalysePromise = null;
        _setAnalysePulse(false);
      }
    }
    if (seq !== _delLoadSeq) return;

    _renderRunBanner(r.last_run);
    _renderQueuedBanner(r.queued_note);
    // The arr history couldn't be read: the message (empty list) or this
    // banner (partial list) says so — never "nothing imported recently".
    _renderActivityBanner(r.message && !r.proposals?.length ? null : r.activity_note);
    if (r.message && !r.proposals?.length) { el.innerHTML = `<p class="loading" role="status" aria-live="polite">${esc(r.message)}</p>`; return; }

    // Show enrichment coverage banner when data quality is low
    _renderEnrichmentCoverageBanner(r.enrichment_coverage);

    _renderDeletionProposals(r.proposals || [], r);
  } catch(e) { if (seq === _delLoadSeq) el.innerHTML = _errHtml(e); }
}

// The last analysis, when it has something to say. Judge calls that failed
// (the model timed out or answered nonsense) used to vanish into the same
// silence as "nothing to delete"; a run is now Partial or Failed out loud,
// and the banner stays until the next run replaces it — never a toast that
// is gone before anyone reads it.
// An analysis asked for while the node was away (services/deletion_queue):
// it starts by itself once the node is free; the stored proposals stay.
export function _renderQueuedBanner(note) {
  document.getElementById('del-queued-banner')?.remove();
  if (!note) return;
  const banner = document.createElement('div');
  banner.id = 'del-queued-banner';
  banner.className = 'banner warn';
  banner.setAttribute('role', 'status');
  banner.innerHTML = `<span class="banner-icon">${SVG_WARN}</span><div class="banner-text">${esc(note)}</div>`;
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(banner, delContent);
}

export function _renderRunBanner(run) {
  document.getElementById('del-run-banner')?.remove();
  if (!run || !(run.failed || run.deferred || run.stopped || run.degraded || run.fallback)) return;
  const banner = document.createElement('div');
  banner.id = 'del-run-banner';
  banner.setAttribute('role', 'status');
  const when = run.at ? ` <span class="t3" title="${escAttr(_fmtAbs(run.at + 'Z'))}">${esc(_fmtRel(run.at + 'Z'))}</span>` : '';
  const again = `<button type="button" class="btn btn-secondary btn-sm" ${act('loadDeletions', state.currentDelCategory || null, null, true)}>${th('del.run_analysis')}</button>`;
  const warn = SVG_WARN.replace('width="13" height="13"', 'width="18" height="18"');
  const deferred = run.deferred ? ` ${th('del.run.deferred', {n: run.deferred})}` : '';
  // The cause comes from the server (llm_errors.DESCRIPTIONS): "Ollama is not
  // answering" and "another program took the graphics card" need different
  // fixes, and the old fixed sentence blamed the model for both.
  const cause = esc(run.failed_reason || t('del.run.no_answer'));
  const stopped = run.stopped ? ` ${th('del.run.stopped')} ${esc(run.stopped)}.` : '';
  // An unreadable taste profile: the run went on, but its ranking is weaker.
  const degraded = run.degraded ? ` ${th('del.run.note')} ${esc(run.degraded)}.` : '';
  // The node away for JUDGE_FALLBACK_HOURS: this server's smaller model judged,
  // and the node re-judges those proposals once it is back.
  const fallback = run.fallback ? ` ${th('del.run.fallback', {node: run.fallback})}` : '';
  const summary = th('del.run.summary', {flagged: run.flagged, judged: run.judged});
  if (run.failed && run.failed >= run.judged) {
    banner.className = 'banner danger';
    banner.innerHTML = `<span class="banner-icon">${warn}</span><div class="banner-text"><b>${th('del.run.none_judged')}</b>${when} — ${cause}.${stopped} ${th('del.run.check_ollama')}${fallback}${deferred}${degraded}</div>${again}`;
  } else if (run.failed) {
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span><div class="banner-text"><b>${summary}</b>${when}. ${run.failed} ${th('del.run.failed_pre')} ${cause}; ${th('del.run.failed_post')}${stopped}${fallback}${deferred}${degraded}</div>${again}`;
  } else if (run.stopped) {
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span><div class="banner-text"><b>${summary}</b>${when}.${stopped} ${th('del.run.rest')}${fallback}${deferred}${degraded}</div>${again}`;
  } else if (run.degraded) {
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span><div class="banner-text"><b>${summary}</b>${when}.${degraded}${fallback}${deferred}</div>${again}`;
  } else {
    banner.className = 'banner';
    banner.innerHTML = `<div class="banner-text">${summary}${when}.${fallback}${deferred}</div>`;
  }
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(banner, delContent);
}

export function _renderActivityBanner(note) {
  document.getElementById('del-activity-banner')?.remove();
  if (!note) return;
  const banner = document.createElement('div');
  banner.id = 'del-activity-banner';
  banner.className = 'banner warn';
  banner.setAttribute('role', 'status');
  const warn = SVG_WARN.replace('width="13" height="13"', 'width="18" height="18"');
  banner.innerHTML = `<span class="banner-icon">${warn}</span><div class="banner-text">${esc(note)}</div>`;
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(banner, delContent);
}

export function _renderEnrichmentCoverageBanner(cov) {
  // Remove any existing banner
  const existing = document.getElementById('enrich-cov-banner');
  if (existing) existing.remove();
  if (!cov) return;

  const banner = document.createElement('div');
  banner.id = 'enrich-cov-banner';
  const warn = SVG_WARN.replace('width="13" height="13"', 'width="18" height="18"');
  if (cov.unknown) {
    // The status table couldn't be read — say so instead of "never run".
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span>
      <div class="banner-text"><b>${th('del.cov.unknown_head')}</b> ${th('del.cov.unknown_pre')}${cov.error ? ` (${esc(cov.error)})` : ''}${th('del.cov.unknown_post')}
        <div class="fs-11 t3 mt-4">${th('del.cov.unknown_hint')}</div></div>`;
  } else if (cov.never_run) {
    banner.className = 'banner danger';
    banner.innerHTML = `<span class="banner-icon">${warn}</span>
      <div class="banner-text"><b>${th('del.cov.never_head')}</b> ${th('del.cov.never_text')}
        <div class="fs-11 t3 mt-4">${th('del.cov.never_hint')}</div></div>
      <button type="button" class="btn btn-primary btn-sm" ${act('startArrPreEnrich', EL)}>${th('del.cov.enrich_now')}</button>`;
  } else if (cov.low) {
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span>
      <div class="banner-text"><b>${th('del.cov.low_head', {pct: cov.pct, enriched: cov.enriched, total: cov.total})}</b> ${th('del.cov.low_text')}
        <div class="fs-11 t3 mt-4">${th('del.cov.low_hint')}</div></div>
      <button type="button" class="btn btn-secondary btn-sm" ${act('startArrPreEnrich', EL)}>${th('del.cov.enrich_batch')}</button>`;
  } else {
    // Coverage is OK — a quiet stat, no warning
    banner.className = 'banner ok';
    banner.innerHTML = `<span class="banner-icon">${SVG_CHECK}</span><div class="banner-text">${th('del.cov.ok_pre')} <b>${th('del.cov.pct', {pct: cov.pct})}</b> ${th('del.cov.ok_post', {enriched: cov.enriched, total: cov.total})}</div>`;
  }
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(banner, delContent);
}

export async function startArrPreEnrich(btn) {
  btnBusy(btn, t('del.starting'));
  try {
    await api('/api/enrichment/arr-pre-enrich', 'POST');
    btnDone(btn, t('del.running_bg'), {keepDisabled: true});
    toast(t('del.enrich_started'), 'success');
  } catch (e) {
    toast(_errMsg(e) || t('del.enrich_failed'), 'danger');
    btnDone(btn);
  }
}

// ── Back after deletion ───────────────────────────────────────────────────────
// Titles Curatarr deleted that are in the library again (an arr list, a
// request, SoulSync's playlist sync). The daily custodian check finds them
// (src/services/deletion_returns.py); Delete again or Keep closes each one.
// A failed check used to read as "nothing came back" (the section vanished);
// it says it could not check instead.
export async function _loadReturned(category) {
  let items = [];
  try {
    const q = category ? `?category=${encodeURIComponent(category)}` : '';
    items = (await api(`/api/recommendations/deletions/returned${q}`)).items || [];
  } catch (e) {
    _renderReturnedError(_errMsg(e));
    return;
  }
  _renderReturned(items);
}

const _day = iso => (iso ? iso.slice(0, 10) : '?');

function _returnedBox() {
  document.getElementById('del-returned')?.remove();
  const box = document.createElement('section');
  box.id = 'del-returned';
  box.className = 'section';
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(box, delContent);
  return box;
}

export function _renderReturnedError(msg) {
  _returnedBox().innerHTML = `<p class="load-err fs-12" role="status">${SVG_WARN} ${th('del.ret.check_failed', {error: msg})}</p>`;
}

export function _renderReturned(items) {
  document.getElementById('del-returned')?.remove();
  if (!items.length) return;
  const box = document.createElement('section');
  box.id = 'del-returned';
  box.className = 'section';
  const rows = items.map(it => `
    <div class="panel-item" data-title="${escAttr(it.title)}" data-service="${escAttr(it.service || '')}">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(it.title)} <span class="badge muted badge-sm">${esc(it.category || '')}</span>${it.match === 'title' ? `<span class="badge muted badge-sm" title="${th('del.ret.by_title_hint')}">${th('del.ret.by_title')}</span>` : ''}</div>
        <div class="panel-actions">
          <button type="button" class="btn btn-secondary btn-sm" ${act('keepReturned', it.id, EL)}>${th('del.keep')}</button>
          <button type="button" class="btn btn-danger btn-sm ml-8" ${act('deleteReturned', it.id, EL)}>${th('del.delete_again')}</button>
        </div>
      </div>
      <div class="panel-item-meta">${esc(it.service || '')} · ${th('del.ret.deleted', {day: _day(it.deleted_at)})} · ${it.kind === 'returned' ? th('del.ret.back_since', {day: _day(it.back_since)}) : th('del.ret.still')}${it.size_gb ? ` · ${it.size_gb} GB` : ''}</div>
    </div>`).join('');
  box.innerHTML = `
    <div class="section-head"><h3>${th('del.ret.head')} <span class="badge amber badge-sm">${items.length}</span></h3>
      <span class="section-hint">${th('del.ret.hint')}</span></div>
    <div class="section-body">${rows}</div>`;
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(box, delContent);
}

function _returnedRowGone(row) {
  row?.remove();
  const box = document.getElementById('del-returned');
  if (!box) return;
  const n = box.querySelectorAll('.panel-item').length;
  if (!n) { box.remove(); return; }
  const badge = box.querySelector('.section-head .badge');
  if (badge) badge.textContent = String(n);
}

export async function deleteReturned(id, btn) {
  const row = btn.closest('.panel-item');
  const title = row?.dataset.title || t('del.this_item');
  const res = await confirmDialog({
    title: t('del.delete_again'), danger: true, countdown: 3, confirmLabel: t('del.delete'),
    body: _deleteBody(title, [t('del.ret.came_back'), ...await deleteFateLines([row?.dataset.service])]),
  });
  if (!res.ok) return;
  btnBusy(btn, t('del.deleting'));
  try {
    const r = await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/returned/delete`, 'POST');
    _returnedRowGone(row);
    if (!r.ok) {
      // The new proposal stays in the list (parked, protected, or failed).
      toast(r.error || t('del.ret.not_deleted', {title}), 'amber', {ms: 8000});
      state._delProposalsAll = null;
      loadDeletions(state.currentDelCategory);
      return;
    }
    toast(t('del.ret.deleted_again', {title}), 'success');
    if (r.warning) toast(r.warning, 'amber', {ms: 10000});
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

export async function keepReturned(id, btn) {
  const row = btn.closest('.panel-item');
  btnBusy(btn, t('actions.keeping'));
  try {
    await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/returned/keep`, 'POST');
    _returnedRowGone(row);
    toast(t('del.ret.kept', {title: row?.dataset.title || t('del.it')}), 'success');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// The body every delete confirmation shares: what goes, and what happens to
// the files. lines come from deleteFateLines(); titles (bulk) list every item
// in a scrolling box so a "Select all" never deletes a title nobody saw.
export function _deleteBody(what, lines = [], titles = null) {
  return `<div class="t-center"><div class="t-danger">${SVG_TRASH}</div>
    <div class="b fs-13 mt-4">${th('del.about_to_delete')}</div>
    <div class="t-amber b mt-4">${esc(what)}</div></div>
    ${titles?.length ? `<ul class="del-list mt-8">${titles.map(x => `<li>${esc(x)}</li>`).join('')}</ul>` : ''}
    ${lines.map(l => `<p class="t2 fs-12 mt-8">${esc(l)}</p>`).join('')}`;
}

// Where deleted files go, per service (the arr's own Recycle Bin setting).
// Prefetched when the list renders so the dialog opens without waiting; a
// click on a cold cache waits for it (5 s server-side timeout at most).
const _FATE_TTL_MS = 5 * 60 * 1000;
const _fate = {};   // service -> {at, recycle_bin, cleanup_days}
const _SVC = {radarr: 'Radarr', sonarr: 'Sonarr', lidarr: 'Lidarr', plex: 'Plex', jellyfin: 'Jellyfin'};
export async function prefetchDeleteTargets(services) {
  const need = [...new Set(services.filter(Boolean))].filter(s => !(_fate[s] && Date.now() - _fate[s].at < _FATE_TTL_MS));
  if (!need.length) return;
  try {
    const r = await api(`/api/recommendations/deletions/delete-target?services=${encodeURIComponent(need.join(','))}`);
    for (const [svc, tg] of Object.entries(r.targets || {})) _fate[svc] = {...tg, at: Date.now()};
  } catch { /* unknown stays unknown: the dialog says so */ }
}
export async function deleteFateLines(services, plural = false) {
  const svcs = [...new Set((services || []).filter(Boolean))];
  await prefetchDeleteTargets(svcs);
  const lines = svcs.map(svc => {
    const name = _SVC[svc] || svc, f = _fate[svc];
    if (!f || f.recycle_bin === null || f.recycle_bin === undefined) return t('del.fate.unknown', {name});
    if (!f.recycle_bin) return t('del.fate.permanent', {name});
    return f.cleanup_days ? t('del.fate.bin_days', {name, n: f.cleanup_days})
                          : t('del.fate.bin', {name});
  });
  if (!svcs.length) lines.push(t('del.fate.none'));
  if (svcs.some(s => !_MEDIA_SERVERS.has(s))) lines.push(t(plural ? 'del.fate.excluded_many' : 'del.fate.excluded_one'));
  // Music: the delete also bans the artist in SoulSync, whose playlist sync
  // would otherwise fetch a liked song again (deletion_returns.py).
  if (svcs.some(s => _fate[s]?.soulsync_ban)) lines.push(t(plural ? 'del.fate.soulsync_many' : 'del.fate.soulsync_one'));
  return lines;
}

// ── BULK DELETE (multi-select) ────────────────────────────────────────────────

export function delToggleAll(master) {
  document.querySelectorAll('#del-content .del-cb').forEach(cb => { cb.checked = master.checked; _syncDelPosterVisual(cb); });
  updateDelBulkCount();
}

// Poster click toggles the same (hidden) checkbox bulkDelete()/
// updateDelBulkCount() already read -- one state, two ways to flip it
// (click the poster, or "Select all"), always kept in sync through here.
export function _syncDelPosterVisual(cb) {
  const el = document.querySelector(`.glow-interactive[data-id="${cb.dataset.id}"]`);
  if (!el) return;
  el.classList.toggle('selected', cb.checked);
  el.setAttribute('aria-pressed', cb.checked ? 'true' : 'false');
}
export function toggleDelSelect(id) {
  const cb = document.querySelector(`.del-cb[data-id="${id}"]`);
  if (!cb) return;
  cb.checked = !cb.checked;
  _syncDelPosterVisual(cb);
  updateDelBulkCount();
}

// The selection bar shows only while something is selected; the bulk button
// lives there and nowhere else.
export function updateDelBulkCount() {
  const boxes = [...document.querySelectorAll('#del-content .del-cb:checked')];
  const bar = document.getElementById('del-select-bar');
  if (!bar) return;
  const gb = boxes.reduce((s, b) => s + (parseFloat(b.dataset.gb) || 0), 0);
  bar.classList.toggle('show', boxes.length > 0);
  document.getElementById('del-select-count').textContent = t('del.selected', {n: boxes.length});
  document.getElementById('del-select-gb').textContent = boxes.length ? `${gb.toFixed(1)} GB` : '';
  const btn = document.getElementById('del-bulk-btn');
  btn.disabled = boxes.length === 0;
  btn.textContent = t('del.delete_selected');
}
export function delClearSelection() {
  document.querySelectorAll('#del-content .del-cb:checked').forEach(cb => { cb.checked = false; _syncDelPosterVisual(cb); });
  const master = document.getElementById('del-select-all');
  if (master) master.checked = false;
  updateDelBulkCount();
}

// The shared reason is sent per item as "Deleted: <reason>" so the backend
// stays on the no-LLM fast path.
export async function bulkDelete() {
  const boxes = [...document.querySelectorAll('#del-content .del-cb:checked')];
  if (!boxes.length) return;
  const gb = boxes.reduce((s, b) => s + (parseFloat(b.dataset.gb) || 0), 0);
  const res = await confirmDialog({
    title: t('del.delete_selected'), danger: true, countdown: 3, confirmLabel: t('del.delete_n', {n: boxes.length}),
    body: _deleteBody(`${t('del.n_titles', {n: boxes.length})} · ${gb.toFixed(1)} GB`,
      await deleteFateLines(boxes.map(b => b.closest('.card')?.dataset.service), boxes.length > 1),
      boxes.map(b => b.closest('.card')?.dataset.title || '?')),
    reason: {label: t('del.reason_shared')},
  });
  if (!res.ok) return;
  const btn = document.getElementById('del-bulk-btn');
  btnBusy(btn, t('del.deleting_n', {n: boxes.length}));
  let started;
  try {
    started = await api('/api/recommendations/deletions/bulk-approve', 'POST', {
      ids: boxes.map(b => parseInt(b.dataset.id, 10)),
      comment: res.reason || null,
    });
  } catch (e) {
    let d = e.message || t('common.failed');
    try { d = JSON.parse(d).detail || d; } catch {}
    toast(t('del.bulk.start_failed', {error: d}), 'danger');
    updateDelBulkCount();
    return;
  }
  // The job runs server-side (visible live in Activity too) — poll the task
  // registry until it finishes, then reload the list and show the summary.
  // A failing poll used to be swallowed (`catch {}`) and retried forever:
  // the button sat on "Deleting N…" and no outcome ever came, on the one
  // irreversible action. A sign-out, or five misses in a row, stops polling
  // and says the outcome is unknown; a job the server no longer lists
  // (restarted mid-run) is unknown too, never "finished".
  const tid = started.task_id;
  let misses = 0;
  const poll = setInterval(async () => {
    let r;
    try {
      r = await api('/api/tasks/');
      misses = 0;
    } catch (e) {
      if (e.status === 401 || ++misses >= 5) {
        clearInterval(poll);
        updateDelBulkCount();
        toast(t('del.bulk.lost', {error: _errMsg(e)}),
          'danger', {sticky: true});
      }
      return;
    }
    const task = (r.tasks || []).find(x => x.id === tid);
    if (task && ['running', 'pending'].includes(task.status)) return;
    clearInterval(poll);
    state._delProposalsAll = null;
    loadDeletions(state.currentDelCategory);
    if (!task) {
      toast(t('del.bulk.unknown'), 'amber', {sticky: true});
      return;
    }
    const last = task.logs?.length ? task.logs[task.logs.length - 1].msg : task.status;
    toast(t('del.bulk.result', {result: last}), task.status === 'done' ? 'success' : 'amber', {ms: 8000, sticky: task.status !== 'done'});
  }, 2000);
}

// The reason typed into the Delete / Keep dialog is what Curatarr learns
// from. Its save used to be .catch(()=>{}) under a success toast; now the
// toast admits it. Returns the suffix for that toast ('' when saved or none).
// The toast's suffix when the reason was lost: a key, read when the toast is said.
export const REASON_LOST = 'del.reason_lost';
export async function _saveReason(id, prefix, reason) {
  if (!reason) return '';
  try {
    await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/comment?comment=${encodeURIComponent(prefix + reason)}`, 'POST');
    return '';
  } catch {
    return t(REASON_LOST);
  }
}

export async function approveDelete(id, btn) {
  const card = btn.closest('.card');
  const title = card?.dataset.title || t('del.this_item');
  const res = await confirmDialog({
    title: t('del.delete_from_library'), danger: true, countdown: 3, confirmLabel: t('del.delete'),
    body: _deleteBody(title, await deleteFateLines([card?.dataset.service])),
    reason: {label: t('del.reason_delete')},
  });
  if (!res.ok) return;
  btnBusy(btn, t('del.deleting'));
  try {
    const lost = await _saveReason(id, 'Deleted: ', res.reason);
    const r = await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/approve`, 'POST');
    if (r.limbo) {
      // ARR unreachable — the proposal stays in limbo, the button becomes a retry
      toast((r.error || t('del.parked_toast')) + lost, 'amber', {ms: 8000});
      btnDone(btn, t('del.retry_delete'));
      return;
    }
    if (!r.ok) { toast((r.error || t('del.delete_failed')) + lost, 'danger'); btnDone(btn); return; }
    // Evict from the client-side cache so switching tabs doesn't show the card
    if (state._delProposalsAll) state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
    card?.classList.add('done');
    btnDone(btn, t('del.deleted'), {keepDisabled: true});
    toast(t('del.deleted_title', {title}) + lost, lost ? 'amber' : 'success');
    // Music: SoulSync did not take the ban, so its playlist sync may fetch it again.
    if (r.warning) toast(r.warning, 'amber', {ms: 10000});
    // Pass 22: no post-delete "tell Curatarr more?" nag — the reason was
    // asked once. Discuss stays clickable on the faded card for anyone who
    // genuinely wants to elaborate.
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// Keep: a 90-day pause on suggesting the title again (the engine's
// KEEP_COOLDOWN_DAYS), or — ticked — a permanent protection. The dialog always
// shows so that choice is never hidden; the card's note pre-fills the reason.
export async function rejectDelete(id, btn) {
  const card = btn.closest('.card');
  const title = card?.dataset.title || t('del.this_item');
  const note = (document.getElementById(`del-comment-${id}`)?.value || '').trim();
  const res = await confirmDialog({
    title: t('del.keep_title_dialog', {title}), confirmLabel: t('del.keep'),
    body: `<p>${th('del.keep_body')}</p>`,
    reason: {label: t('del.reason_keep'), value: note},
    check: {label: t('del.protect'), hint: t('del.protect_hint')},
  });
  if (!res.ok) return;
  const reason = res.reason;
  btnBusy(btn, t('actions.keeping'));
  try {
    const lost = await _saveReason(id, 'Keeping: ', reason);
    await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/reject${res.checked ? '?protect=true' : ''}`, 'POST');
    if (state._delProposalsAll) state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
    card?.classList.add('done');
    btnDone(btn, t(res.checked ? 'del.protected' : 'del.kept'), {keepDisabled: true});
    toast(t(res.checked ? 'del.protected_title' : 'del.kept_title', {title}) + lost, lost ? 'amber' : 'success');
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// Pass 81: "Level 2 Override" — force the curator to challenge its own
// deletion verdict by opening the standard deletion-discussion thread and
// auto-sending a Level-2 challenge prompt. NO separate endpoint / parallel
// LLM pipeline: the verdict streams in via the normal /api/chat/message
// flow, the user sees it live and can immediately follow up ("but check
// director X"), and memory extraction picks it up like any other turn.
//
// The Level-2 prompt is hardcoded here (not server-side) on purpose:
// (a) the chat backend's ``_build_discuss_context_block`` already injects
// the title + original pitch + synopsis as RAG, so we only need to carry
// the *challenge framing*; (b) keeping it client-side means no extra
// round-trip before the user sees their own message land in the chat.
// Pass 81: "Level 2 Override" — challenge the curator's deletion verdict
// by opening the standard deletion-discuss thread + firing a one-shot
// ``reevaluate`` flag. The chat backend sees the flag, looks up the
// proposal as usual, AND appends a Level-2 challenge framing
// (``_LEVEL_2_REEVAL_FRAMING`` in chat.py) to the system prompt for THIS
// turn only.
//
// Pass 81d split: the long Level-2 prompt moved server-side. Three wins:
//   - the user's chat-input no longer pastes a 1.4 kB wall of text they
//     have to scroll past — they see a single short line
//   - the long framing never lands in ``ConversationMessage``, so memory
//     extraction can't misinterpret rules-text as user preferences
//   - the framing is centrally maintainable / future-translatable in one
//     Python constant
//
// Iteration history (81a → 81d) lives in the engine-module comment block
// next to ``_LEVEL_2_REEVAL_FRAMING``. Short version: don't open with a
// meta-disclaimer, training corpus IS the knowledge base, per-axis
// hedging only, no named-work anchors.
export async function onReevaluateDeletion(btn) {
  if (!btn || _busyStreaming()) return;
  const pid = parseInt(btn.dataset.pid || '0', 10);
  if (!pid) return;
  const title    = btn.dataset.title    || '';
  const pitch    = btn.dataset.pitch    || '';
  const category = btn.dataset.category || '';
  const poster   = btn.dataset.poster   || '';

  // 1. Same context priming as the Discuss button, plus a one-shot
  //    ``reevaluate`` flag. ``sendMessage`` snapshots the context into
  //    this turn's payload then clears the flag, so follow-up turns in
  //    the same thread don't re-inject the Level-2 framing on every
  //    "but also check…" message.
  // 2. Switch to chat view (this proposal's thread history) and show the
  //    original verdict as an assistant bubble — same UX as Discuss, so the
  //    user always sees what they're challenging before the new answer
  //    streams in.
  const ctx = {
    kind: 'deletion_proposal',
    proposal_id: pid,
    category: category || null,
    title: title || null,
    poster_url: poster || null,
    reevaluate: true,
  };
  if (!await enterThread(ctx, t('disc.banner_reevaluate', {title}),
                         proposalOpener(title, pitch, btn.dataset.stagnant === '1'))) return;

  // 3. Pre-fill a short, readable user message and auto-send. The actual
  //    Level-2 framing arrives via the system prompt on the backend —
  //    this short line is all the user sees in their bubble + chat
  //    history. The user can still type a follow-up after the curator
  //    responds.
  const input = document.getElementById('chat-input');
  if (input) {
    input.value = t('disc.level2');
    input.focus();
    setTimeout(() => sendMessage(), 300);
  }
}

// The Deletions toolbar (Analyse library, Show cached, the recent-days select)
// used to pass the global currentDelCategory. Module state has no global
// name, so inline handlers call this and it reads the state.
export function reloadDeletions(refresh = false) {
  return loadDeletions(state.currentDelCategory, null, refresh);
}

export function onRecentOnlyChange(el) { toggleRecentOnly(el.checked); }

// The bulk-select checkbox drives the count and the poster's selected look in one step.
export function onDelCheckbox(el) { updateDelBulkCount(); _syncDelPosterVisual(el); }
// Ctrl/Cmd+Enter in the note textarea blurs it, which saves the note.
export function blurOnCtrlEnter(event, el) { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) el.blur(); }
