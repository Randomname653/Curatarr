// ── DELETIONS ─────────────────────────────────────────────────────────────────
// Moved state.currentDelCategory to state
// Client-side cache for the full proposals list from the last "All" fetch.
// Tab switches filter this in-memory instead of making redundant API calls.
// Invalidated whenever a targeted single-category analysis runs (because that
// category's slice would then be out of sync with the stored full list).
// Moved state._delProposalsAll to state
import { SVG_CHECK, SVG_TRASH, SVG_WARN, _errHtml, _errMsg, _posterImg, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, menuHtml, toast } from './ui.js';
import { state } from './state.js';
import { openMatchPicker } from './picker.js';
import { api } from './api.js';
import { _setDiscussBanner, addMsg, sendMessage } from './chat.js';
import { showView } from './nav.js';

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
  const label = ageDays <= 0 ? 'today'
              : ageDays === 1 ? 'yesterday'
              : ageDays <= 7  ? `${ageDays}d ago`
              :                 `${Math.floor(ageDays / 7)}w ago`;
  // Recent activity = small amber "new" tag. Small enough not to compete
  // with the confidence pill, distinct enough to spot at a glance.
  return `<span class="badge amber badge-sm" title="Latest episode/movie/track file imported ${ageDays}d ago — fresh activity may indicate this proposal is more relevant to review.">new · ${label}</span>`;
}

export function _renderDeletionProposals(proposals) {
  const el = document.getElementById('del-content');
  if (!proposals?.length) {
    el.innerHTML = emptyHtml('No proposals yet — <b>Analyse library</b> asks the curator for deletion candidates.', 'Analyse library', `loadDeletions(${state.currentDelCategory ? '\'' + state.currentDelCategory + '\'' : 'null'},null,true)`);
    updateDelBulkCount();
    return;
  }
  const totalGb = proposals.reduce((s, p) => s + (p.size_gb || 0), 0).toFixed(1);
  // Card anatomy: poster (click = select for bulk) · head (title + badges,
  // three visible actions, the rest behind More) · facts · pitch · the note,
  // which saves itself when you click away.
  el.innerHTML = `
    <div class="fs-13 t2 mb-12">Potential savings: <b class="t-amber">${totalGb} GB</b> · ${proposals.length} proposals</div>` +
    proposals.map(p => {
      const genreTags = (p.genres||'').split(',').map(g=>g.trim()).filter(Boolean).slice(0,6)
        .map(g=>`<span class="badge muted badge-sm">${esc(g)}</span>`).join('');
      const placeholder = p.service==='lidarr'
        ? "e.g. 'Keep — listen to this regularly' or 'Delete — haven't touched it in years'"
        : p.service==='radarr'
          ? "e.g. 'Keep — haven't watched it yet' or 'Delete — awful film'"
          : "e.g. 'Keep — still watching this' or 'Delete — dropped after 2 eps'";
      const limbo = p.status === 'limbo';
      const ctx = `data-pid="${p.id}" data-title="${escAttr(p.title)}" data-pitch="${escAttr(p.pitch||p.reason||'')}" data-category="${escAttr(p.category||'')}" data-poster="${escAttr(p.poster_url||'')}"`;
      return `
      <div class="card mb-12${p.stagnant ? ' is-stagnant' : (p.confidence > .7 ? ' is-hot' : '')}" data-del-id="${p.id}" data-title="${escAttr(p.title)}">
        <div class="poster-card">
          <input type="checkbox" class="del-cb" data-id="${p.id}" data-gb="${p.size_gb||0}" onchange="updateDelBulkCount(); _syncDelPosterVisual(this)" hidden title="Select for bulk delete"/>
          <div class="glow-interactive selectable${p.category==='music'?' is-music':''}" data-id="${p.id}" role="button" tabindex="0" aria-pressed="false" aria-label="Select ${escAttr(p.title)} for bulk delete" onclick="toggleDelSelect(${p.id})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}" title="Click to select for bulk delete">
            ${_posterImg(p.poster_url, 174, p.category==='music'?174:261, p.category==='music')}
            <div class="del-poster-check">${SVG_CHECK.replace('width="13" height="13"', 'width="34" height="34"')}</div>
          </div>
          <div class="grow">
            <div class="panel-item-head">
              <div class="panel-item-title" style="font-size:16px">${esc(p.title)}
                <span class="badge ${p.confidence>.7?'danger':'muted'}" title="How sure the judge is that this can go">${Math.round((p.confidence||0)*100)}%</span>
                ${p.stagnant?`<span class="badge amber" title="Judge verdict: merely fine — not a clear cut, your call">Stagnant</span>`:''}${_recentActivityBadge(p)}
                ${limbo ? '<span class="badge danger" title="The previous delete attempt failed — the arr was unreachable">delete failed</span>' : ''}
              </div>
              <div class="panel-actions">
                <button type="button" class="btn btn-danger btn-sm" onclick="approveDelete(${p.id},this)">${limbo?'Retry Delete':'Delete'}</button>
                <button type="button" class="btn btn-secondary btn-sm" onclick="rejectDelete(${p.id},this)">Keep</button>
                <button type="button" class="btn btn-secondary btn-sm" onclick="onDiscussDeletion(this)" ${ctx}>Discuss</button>
                ${menuHtml([
                  {label: 'Reevaluate', call: 'onReevaluateDeletion(this)', attrs: ctx, title: 'Open a discussion thread and challenge the verdict with a Level 2 thematic scan (creator pedigree, subversion, psychological function)'},
                  p.media_id && p.service ? {label: 'Fix match', call: 'onFixMatch(this)', attrs: `${ctx} data-service="${escAttr(p.service)}" data-mediaid="${escAttr(p.media_id)}"`, title: 'Card or pitch describing the wrong same-named title? Pin the correct entity — the pin survives rescans and the item re-enriches on it.'} : null,
                  p.arr_url ? {label: `Open in ${p.service}`, href: p.arr_url} : null,
                ])}
              </div>
            </div>
            <div class="fs-12 t3 mt-4">${esc(p.service||'')} · ${p.size_gb||0} GB</div>
            ${genreTags ? `<div class="row mt-8" style="gap:5px">${genreTags}</div>` : ''}
            ${p.synopsis ? `<div class="fs-12 t3 mt-8" style="line-height:1.55;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden">${esc(p.synopsis)}</div>` : ''}
            <div class="t2 mt-8" style="font-size:13.5px;line-height:1.65;font-style:italic">"${esc(p.pitch||p.reason||'')}"</div>
            ${limbo ? `<div class="fs-11 t-amber mt-4">${SVG_WARN} Previous delete attempt failed — the arr was unreachable. Retry Delete tries again.</div>` : ''}
            <textarea id="del-comment-${p.id}" class="del-comment" data-saved="${escAttr(p.user_comment||'')}" placeholder="${escAttr(placeholder)}" onblur="saveComment(${p.id})" onkeydown="if(event.key==='Enter'&&(event.ctrlKey||event.metaKey))this.blur()">${esc(p.user_comment||'')}</textarea>
            <div class="fs-11 t3 mt-4" id="del-note-hint-${p.id}">${p.user_comment ? 'Note saved.' : 'A note teaches Curatarr your reasoning — it saves when you click away (or Ctrl+Enter).'}</div>
          </div>
        </div>
      </div>`;
    }).join('');
  // Fresh render = fresh selection: reset the bulk bar + master checkbox.
  const master = document.getElementById('del-select-all');
  if (master) master.checked = false;
  updateDelBulkCount();
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

export function _setAnalysePulse(on) {
  const dot = document.getElementById('sb-deletions-pulse');
  if (dot) dot.style.display = on ? '' : 'none';
}

export async function loadDeletions(category=null, btn=null, refresh=false) {
  state.currentDelCategory = category;
  // Restore the correct active tab whether called from a tab click, the Analyse
  // button, the Show Cached button, or a programmatic call like showView().
  document.querySelectorAll('#deletions-view .cat-tab').forEach(b => {
    const match = (b.dataset.cat || '') === (category || '');
    b.classList.toggle('active', btn ? b === btn : match);
  });
  const el = document.getElementById('del-content');

  const recentParams = _delRecentParams();
  const recentActive = recentParams.length > 0;

  // ── Pass 25: piggy-back on an in-flight Analyse ──────────────────────────
  // If the user is coming back to this view while a previous Analyse is
  // still running, await the SAME promise and render its result — instead
  // of opening a parallel DB read that returns the pre-commit snapshot.
  if (!refresh && _delAnalysePromise) {
    const elapsed = Math.round((Date.now() - _delAnalyseStartedAt) / 1000);
    el.innerHTML = `<p class="loading" role="status" aria-live="polite">Analysis still running (${elapsed}s)… auto-refreshing when it's done.</p>`;
    try {
      const r = await _delAnalysePromise;
      _renderEnrichmentCoverageBanner(r.enrichment_coverage);
      if (!category && !recentActive && r.proposals) state._delProposalsAll = r.proposals;
      _renderDeletionProposals(r.proposals || []);
    } catch(e) {
      el.innerHTML = `<p class="loading" role="status" aria-live="polite">${esc(e.message || 'Analyse failed')}</p>`;
    }
    return;
  }

  // ── Client-side fast path ────────────────────────────────────────────────
  // Tab clicks (btn !== null) reuse the in-memory "All" cache so we never
  // make a redundant API call just to slice data we already have.
  // "Analyse" (refresh=true), "Show cached" (btn=null), and the recent-only
  // filter always go to the backend — the latter because it's a server-side
  // filter, the cached "All" snapshot doesn't carry that filter state.
  if (btn !== null && !refresh && !recentActive && state._delProposalsAll) {
    const filtered = category
      ? state._delProposalsAll.filter(p => p.category === category)
      : state._delProposalsAll;
    _renderDeletionProposals(filtered);
    return;
  }

  // A targeted single-category refresh makes the stored "All" cache stale for
  // that slice — drop it so the next tab switch re-fetches correctly.
  if (refresh && category) state._delProposalsAll = null;

  el.innerHTML = '<p class="loading" role="status" aria-live="polite">' + (refresh ? 'Analysing library with AI…' : 'Loading cached proposals…') + '</p>';
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

    if (r.message && !r.proposals?.length) { el.innerHTML = `<p class="loading" role="status" aria-live="polite">${esc(r.message)}</p>`; return; }

    // Show enrichment coverage banner when data quality is low
    _renderEnrichmentCoverageBanner(r.enrichment_coverage);

    // Cache the full result whenever we fetched the unfiltered "All" view.
    // The recent-only branch is intentionally NOT cached here — the cache
    // is meant to slice an unfiltered snapshot, not a server-filtered subset.
    if (!category && !recentActive && r.proposals) state._delProposalsAll = r.proposals;

    _renderDeletionProposals(r.proposals || []);
  } catch(e) { el.innerHTML = _errHtml(e); }
}

export function _renderEnrichmentCoverageBanner(cov) {
  // Remove any existing banner
  const existing = document.getElementById('enrich-cov-banner');
  if (existing) existing.remove();
  if (!cov) return;

  const banner = document.createElement('div');
  banner.id = 'enrich-cov-banner';
  const warn = SVG_WARN.replace('width="13" height="13"', 'width="18" height="18"');
  if (cov.never_run) {
    banner.className = 'banner danger';
    banner.innerHTML = `<span class="banner-icon">${warn}</span>
      <div class="banner-text"><b>ARR enrichment has never run.</b> Curatarr has no rating or genre data for your library — proposals may be inaccurate.
        <div class="fs-11 t3 mt-4">Run enrichment once to populate the metadata cache; afterwards it runs nightly (02:30).</div></div>
      <button type="button" class="btn btn-primary btn-sm" onclick="startArrPreEnrich(this)">Enrich library now</button>`;
  } else if (cov.low) {
    banner.className = 'banner warn';
    banner.innerHTML = `<span class="banner-icon">${warn}</span>
      <div class="banner-text"><b>Low enrichment coverage: ${cov.pct}% (${cov.enriched}/${cov.total} items)</b> Proposals for unenriched items use neutral rating fallbacks — results are less precise.
        <div class="fs-11 t3 mt-4">Nightly enrichment runs at 02:30; a batch can run now.</div></div>
      <button type="button" class="btn btn-secondary btn-sm" onclick="startArrPreEnrich(this)">Enrich batch now</button>`;
  } else {
    // Coverage is OK — a quiet stat, no warning
    banner.className = 'banner ok';
    banner.innerHTML = `<span class="banner-icon">${SVG_CHECK}</span><div class="banner-text">Enrichment coverage <b>${cov.pct}%</b> (${cov.enriched}/${cov.total} arr items have rating + genre data)</div>`;
  }
  const delContent = document.getElementById('del-content');
  delContent.parentNode.insertBefore(banner, delContent);
}

export async function startArrPreEnrich(btn) {
  btnBusy(btn, 'Starting…');
  try {
    await api('/api/enrichment/arr-pre-enrich', 'POST');
    btnDone(btn, 'Running in background…', {keepDisabled: true});
    toast('ARR enrichment started — progress shows in Activity', 'success');
  } catch (e) {
    toast(_errMsg(e) || 'Failed to start enrichment', 'danger');
    btnDone(btn);
  }
}

// The body every delete confirmation shares: what goes, and what that means.
// The 3-second countdown and the reason field come from confirmDialog().
export function _deleteBody(what, note) {
  return `<div class="t-center"><div class="t-danger">${SVG_TRASH}</div>
    <div class="b fs-13 mt-4">You're about to delete</div>
    <div class="t-amber b mt-4">${esc(what)}</div>
    <p class="t3 fs-12 mt-8">${esc(note)}</p></div>`;
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
  document.getElementById('del-select-count').textContent = `${boxes.length} selected`;
  document.getElementById('del-select-gb').textContent = boxes.length ? `${gb.toFixed(1)} GB` : '';
  const btn = document.getElementById('del-bulk-btn');
  btn.disabled = boxes.length === 0;
  btn.textContent = 'Delete selected';
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
    title: 'Delete selected', danger: true, countdown: 3, confirmLabel: `Delete ${boxes.length}`,
    body: _deleteBody(`${boxes.length} items · ${gb.toFixed(1)} GB`, 'This removes them from your *arr libraries and deletes the files. This cannot be undone.'),
    reason: {label: 'Shared reason (optional — Curatarr learns from it)'},
  });
  if (!res.ok) return;
  const btn = document.getElementById('del-bulk-btn');
  btnBusy(btn, `Deleting ${boxes.length}…`);
  let started;
  try {
    started = await api('/api/recommendations/deletions/bulk-approve', 'POST', {
      ids: boxes.map(b => parseInt(b.dataset.id, 10)),
      comment: res.reason || null,
    });
  } catch (e) {
    let d = e.message || 'failed';
    try { d = JSON.parse(d).detail || d; } catch {}
    toast('Bulk delete failed to start: ' + d, 'danger');
    updateDelBulkCount();
    return;
  }
  // The job runs server-side (visible live in Activity too) — poll the task
  // registry until it finishes, then reload the list and show the summary.
  const tid = started.task_id;
  const poll = setInterval(async () => {
    try {
      const r = await api('/api/tasks/');
      const t = (r.tasks || []).find(x => x.id === tid);
      if (!t || !['running', 'pending'].includes(t.status)) {
        clearInterval(poll);
        const last = t?.logs?.length ? t.logs[t.logs.length - 1].msg : 'finished';
        state._delProposalsAll = null;
        loadDeletions(state.currentDelCategory);
        toast('Bulk delete: ' + last, t?.status === 'done' ? 'success' : 'amber', {ms: 8000});
      }
    } catch {}
  }, 2000);
}

export async function approveDelete(id, btn) {
  const card = btn.closest('.card');
  const title = card?.dataset.title || 'this item';
  const res = await confirmDialog({
    title: 'Delete from library', danger: true, countdown: 3, confirmLabel: 'Delete',
    body: _deleteBody(title, 'This removes it from your *arr library and deletes the files. This cannot be undone.'),
    reason: {label: 'What made you decide? (optional — Curatarr learns from it)'},
  });
  if (!res.ok) return;
  btnBusy(btn, 'Deleting…');
  try {
    if (res.reason) {
      await api(`/api/recommendations/deletions/${id}/comment?comment=${encodeURIComponent('Deleted: ' + res.reason)}`, 'POST').catch(()=>{});
    }
    const r = await api(`/api/recommendations/deletions/${id}/approve`, 'POST');
    if (r.limbo) {
      // ARR unreachable — the proposal stays in limbo, the button becomes a retry
      toast(r.error || 'The arr was unreachable — the proposal stays, retry when it is back', 'amber', {ms: 8000});
      btnDone(btn, 'Retry Delete');
      return;
    }
    if (!r.ok) { toast(r.error || 'Delete failed', 'danger'); btnDone(btn); return; }
    // Evict from the client-side cache so switching tabs doesn't show the card
    if (state._delProposalsAll) state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
    card?.classList.add('done');
    btnDone(btn, 'Deleted', {keepDisabled: true});
    toast(`Deleted "${title}"`, 'success');
    // Pass 22: no post-delete "tell Curatarr more?" nag — the reason was
    // asked once. Discuss stays clickable on the faded card for anyone who
    // genuinely wants to elaborate.
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// Keep: the note on the card is the reason; only an empty note asks for one.
export async function rejectDelete(id, btn) {
  const card = btn.closest('.card');
  const title = card?.dataset.title || 'this item';
  let reason = (document.getElementById(`del-comment-${id}`)?.value || '').trim();
  if (!reason) {
    const res = await confirmDialog({
      title: `Keep "${title}"`, confirmLabel: 'Keep',
      body: '<p>The proposal is closed and the title stays.</p>',
      reason: {label: 'Why keep it? (optional — Curatarr learns from it)'},
    });
    if (!res.ok) return;
    reason = res.reason;
  }
  btnBusy(btn, 'Keeping…');
  try {
    if (reason) {
      await api(`/api/recommendations/deletions/${id}/comment?comment=${encodeURIComponent('Keeping: ' + reason)}`, 'POST').catch(()=>{});
    }
    await api(`/api/recommendations/deletions/${id}/reject`, 'POST');
    if (state._delProposalsAll) state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
    card?.classList.add('done');
    btnDone(btn, 'Kept', {keepDisabled: true});
    toast(`Kept "${title}"`, 'success');
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
export function onReevaluateDeletion(btn) {
  if (!btn) return;
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
  state.pendingDiscussContext = {
    kind: 'deletion_proposal',
    proposal_id: pid,
    category: category || null,
    title: title || null,
    poster_url: poster || null,
    reevaluate: true,
  };
  _setDiscussBanner(`Reevaluating deletion of "${title}"`);

  // 2. Switch to chat view and show the original verdict as an assistant
  //    bubble — same UX as Discuss, so the user always sees what they're
  //    challenging before the new answer streams in.
  showView('chat', document.querySelector('.sb-item[onclick*=chat]'));
  addMsg(`I have suggested "${title}" for deletion. Reason: "${pitch}"`, 'assistant');

  // 3. Pre-fill a short, readable user message and auto-send. The actual
  //    Level-2 framing arrives via the system prompt on the backend —
  //    this short line is all the user sees in their bubble + chat
  //    history. The user can still type a follow-up after the curator
  //    responds.
  const input = document.getElementById('chat-input');
  if (input) {
    input.value = 'Run a Level 2 thematic scan on this deletion.';
    input.focus();
    setTimeout(() => sendMessage(), 300);
  }
}
