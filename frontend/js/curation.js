// ── Protections (judge auto-saves AND chat-intent grants, both liftable) ─────
import { api } from './api.js';
import { EL, SVG_CHECK, _errHtml, _errMsg, act, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, loc, toast } from './ui.js';
import { startTaskStream, stopTaskStream } from './activity.js';
import { brandMark } from './brand.js';
export async function loadJudgeProtections() {
  const el = document.getElementById('protections-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading protected titles…</p>';
  try {
    const r = await api('/api/recommendations/protections');
    const rows = r.protections || [];
    if (!rows.length) {
      el.innerHTML = emptyHtml('No protections yet — titles appear here when the judge protects them or you grant protection in chat.');
      return;
    }
    el.innerHTML = rows.map(p => {
      const badge = p.source && p.source !== 'judge'
        ? '<span class="badge amber badge-sm">Asked in chat</span>'
        : (p.verdict === 'KEEP_WITH_FLAG'
            ? '<span class="badge amber badge-sm">Keep · downscale</span>'
            : '<span class="badge muted badge-sm">Protected</span>');
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString(loc()) : '';
      return `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(p.title || '—')} ${badge}
            <span class="panel-item-meta">${esc(p.category || '')}${when ? ' · ' + when : ''}</span></div>
          <div class="panel-actions">
            <button type="button" class="btn btn-secondary btn-sm" ${act('liftProtection', p.id, EL)}>Lift protection</button>
            ${p.arr_url ? `<a href="${esc(p.arr_url)}" target="_blank" rel="noopener" aria-label="Open ${escAttr(p.title)} in ${esc(_arrLabel(p.category))}" class="btn btn-secondary btn-sm">Open in ${esc(_arrLabel(p.category))}</a>` : ''}
          </div>
        </div>
        ${_pillarReason(p.reason)}
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

// The judge's reason is one line per pillar ("III Household — …", "Bitrate
// — …"). Folded by default — a list of protections reads as titles, not as
// walls of text — and unfolded into a two-column sheet: the pillar, then
// what the judge found. A reason in another shape (a chat grant) stays prose.
const PILLAR_LINE = /^\s*(?:(?:III|II|I|0)\s+)?(Household|Custodian|Resonance|Ego|Bitrate)\s+[—-]\s+(.+)$/;
export function _pillarReason(reason) {
  const text = String(reason || '').trim();
  if (!text) return '';
  const lines = text.split(/\r?\n/).map(l => l.trim()).filter(Boolean);
  const parsed = lines.map(l => l.match(PILLAR_LINE));
  if (!parsed.every(Boolean)) return `<div class="pre-line panel-item-sub">${esc(text)}</div>`;
  return `<details class="pillar-why"><summary>Why it's protected</summary><dl class="pillar-list">${
    parsed.map(m => `<dt>${esc(m[1])}</dt><dd>${esc(m[2])}</dd>`).join('')}</dl></details>`;
}

export async function liftProtection(id, btn) {
  const res = await confirmDialog({title: 'Lift protection', confirmLabel: 'Lift', body: '<p>The title is re-judged on the next analysis.</p>'});
  if (!res.ok) return;
  btnBusy(btn);
  try {
    await api('/api/recommendations/protections/' + encodeURIComponent(id), 'DELETE');
    toast('Protection lifted — re-judged on the next analysis', 'success');
    loadJudgeProtections();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

export async function loadPrinciples() {
  const el = document.getElementById('principles-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading learned principles…</p>';
  try {
    const r = await api('/api/recommendations/principles');
    const rows = r.principles || [];
    if (!rows.length) {
      el.innerHTML = emptyHtml('No learned principles yet. They appear as you debate deletions (if learning is turned on for this server).');
      return;
    }
    const nActive = rows.filter(p => p.status === 'active').length;
    const condenseBar = nActive >= 2 ? `
      <div class="row mb-8">
        <span class="fs-12 t3">${nActive} active rules</span>
        <button type="button" class="btn btn-secondary btn-sm row-end" ${act('condensePrinciples', EL)}
          title="Let the curator consolidate near-duplicate active rules (conservative — usually finds nothing on a clean set)">Condense duplicates</button>
      </div>` : '';
    el.innerHTML = condenseBar + rows.map(p => {
      const st = p.status === 'active'
        ? '<span class="badge success">active</span>'
        : p.status === 'rejected'
          ? '<span class="badge muted">rejected</span>'
          : p.status === 'merged'
            ? '<span class="badge muted" title="Consolidated into a newer rule">merged</span>'
            : p.status === 'superseded'
              ? '<span class="badge muted" title="Replaced by a rule you adopted that contradicts it">superseded</span>'
              : '<span class="badge amber">shadow</span>';
      const nov = p.novelty === 'contradiction'
        ? ' <span class="badge danger" title="Conflicts with an existing principle — your call">contradiction</span>'
        : (p.novelty ? ` <span class="badge muted">${esc(p.novelty)}</span>` : '');
      const reinf = (p.times_reinforced || 0) > 0 ? ` <span class="fs-11 t3">×${p.times_reinforced} reinforced</span>` : '';
      const meta = [p.basis, p.category].filter(Boolean).map(esc).join(' · ');
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString(loc()) : '';
      const actions = p.status === 'merged'
        ? ''
        : p.status === 'active'
          ? `<button type="button" class="btn btn-secondary btn-sm" ${act('setPrinciple', p.id, 'shadow', EL)}>Deactivate</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" ${act('setPrinciple', p.id, 'activate', EL)}>Activate</button>`;
      return `<div class="panel-item">
        <div class="items-start panel-item-head">
          <div class="minw-200 grow">
            <div>${st}${nov}${reinf}</div>
            <div class="fs-13 mt-4">${esc(p.text || '')}</div>
            <div class="panel-item-meta fs-11 mt-4">${meta}${when ? ' · ' + when : ''}${p.related ? ' · vs: ' + esc(p.related) : ''}</div>
          </div>
          <div class="panel-actions">
            ${actions}
            ${p.status === 'merged' ? '' : `<button type="button" class="btn btn-secondary btn-sm" ${act('setPrinciple', p.id, 'reject', EL)}>Reject</button>`}
          </div>
        </div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function setPrinciple(id, action, btn) {
  if (action === 'reject') {
    const res = await confirmDialog({title: 'Reject this principle', danger: true, confirmLabel: 'Reject',
      body: '<p>The rule is marked rejected and no longer applied. A rejection is a verdict the curator learns from.</p>'});
    if (!res.ok) return;
  }
  btnBusy(btn);
  try {
    // Encoded: action arguments come from markup, and a "../" segment would
    // otherwise walk the POST onto any other endpoint.
    await api('/api/recommendations/principles/' + encodeURIComponent(id) + '/' + encodeURIComponent(action), 'POST');
    toast({activate: 'Principle activated', shadow: 'Principle deactivated', reject: 'Principle rejected'}[action] || 'Saved', 'success');
    loadPrinciples();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

export async function shutdownServer() {
  const res = await confirmDialog({
    title: 'Shut down Curatarr', danger: true, confirmLabel: 'Shut down',
    body: '<p>The server stops gracefully: running jobs are finalized, pending memory extractions are flushed, and the database is released for sync.</p><p class="t3 fs-12 mt-8">You will need to start it again on the server.</p>',
  });
  if (!res.ok) return;
  // Release this tab's SSE stream BEFORE asking — open streams are exactly
  // what used to pin "Waiting for connections to close".
  stopTaskStream();
  try { await api('/api/system/shutdown', 'POST'); } catch (e) {
    // A network error is the server dying before it replied: expected. An
    // HTTP error (e.status set) is the server refusing and still running, so
    // "close this tab" would be a lie.
    if (e && e.status) {
      toast(`Shutdown refused: ${_errMsg(e)}`, 'danger');
      startTaskStream();
      return;
    }
  }
  const ov = document.createElement('div');
  ov.className = 'sd-overlay';
  ov.innerHTML =
    '<div class="sd-ripple"></div><div class="sd-ripple"></div><div class="sd-ripple"></div>' +
    '<div class="stack-center-16">' +
    '<h2 class="b">Curatarr is shutting down…</h2>' +
    '<p class="t3 fs-13">You can close this tab. Start the server again to come back.</p></div>';
  document.body.appendChild(ov);
  ov.querySelector('.stack-center-16').prepend(brandMark('sd-logo'));

  // Wind-down: pulse + ripples run at full speed, then decelerate to a
  // standstill over ~20s (the server is long gone — endless pulsing would
  // suggest activity). playbackRate is eased down phase-continuously.
  const logo = ov.querySelector('.sd-logo');
  // Web Animations ignore the stylesheet's reduced-motion block: honour the
  // preference here and show the powered-off end state straight away.
  if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    logo.style.opacity = '.55';
    return;
  }
  const anims = [logo.animate(
    [{ transform: 'scale(1)', opacity: 1 },
     { transform: 'scale(1.12)', opacity: .72 },
     { transform: 'scale(1)', opacity: 1 }],
    { duration: 1500, iterations: Infinity, easing: 'ease-in-out' })];
  ov.querySelectorAll('.sd-ripple').forEach((r, i) => anims.push(r.animate(
    [{ transform: 'translate(-50%,-50%) scale(.04)', opacity: .5 },
     { transform: 'translate(-50%,-50%) scale(1)', opacity: 0 }],
    { duration: 2800, delay: i * 900, iterations: Infinity, easing: 'ease-out' })));
  const t0 = Date.now(), WIND_DOWN_MS = 20000;
  const tick = setInterval(() => {
    const rate = Math.max(0, 1 - (Date.now() - t0) / WIND_DOWN_MS);
    anims.forEach(a => { a.playbackRate = rate; });
    if (rate <= 0) {
      clearInterval(tick);
      anims.forEach(a => a.cancel());   // ripples return to opacity 0 base
      logo.style.transition = 'opacity 1.5s';
      logo.style.opacity = '.55';       // the eye dims — powered off
    }
  }, 150);
}

export async function condensePrinciples(btn) {
  const res = await confirmDialog({title: 'Condense duplicates', confirmLabel: 'Condense',
    body: '<p>The curator consolidates near-duplicate active rules. Merged originals are kept as an audit trail (status "merged").</p>'});
  if (!res.ok) return;
  const el = document.getElementById('principles-content');
  btnBusy(btn, 'Reviewing…');
  if (el) el.innerHTML = '<p class="loading" role="status" aria-live="polite">Curator is reviewing the rule set… (one model call per category)</p>';
  let note = '';
  try {
    const r = await api('/api/recommendations/principles/condense', 'POST');
    const merges = r.merged || [];
    if (!merges.length) {
      note = `<div class="banner ok"><span class="banner-icon">${SVG_CHECK}</span><div class="banner-text">No duplicates found — the rule set is already clean (${r.active_before} active rules).</div></div>`;
    } else {
      // The merge report outlives a toast: it stays above the list until the next load.
      note = `<div class="banner ok"><div class="banner-text"><b>Consolidated ${merges.length} group${merges.length > 1 ? 's' : ''}</b> · active rules ${r.active_before} → ${r.active_after}
        ${merges.map(m => `<div class="mt-8">${(m.sources || []).map(s => `<div class="t3">– ${esc(s.text)}</div>`).join('')}<div>→ ${esc(m.text)}</div></div>`).join('')}</div></div>`;
    }
  } catch (e) {
    toast('Condense failed: ' + _errMsg(e), 'danger');
  }
  await loadPrinciples();
  if (note && el) el.insertAdjacentHTML('afterbegin', note);
}

export function _arrLabel(category) {
  return category === 'movie' ? 'Radarr' : category === 'music' ? 'Lidarr' : 'Sonarr';
}

export async function loadDownscale() {
  const el = document.getElementById('downscale-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading downscale candidates…</p>';
  try {
    const r = await api('/api/recommendations/downscale');
    const rows = r.candidates || [];
    if (!rows.length) {
      el.innerHTML = emptyHtml('No downscale candidates. Titles that are kept but flagged for an oversized file appear here after the next scan.');
      return;
    }
    const head = `<div class="fs-12 t2 mb-8">${rows.length} title(s)${r.total_gb ? ` · ${r.total_gb} GB bound (actual file size)` : ''}</div>`;
    el.innerHTML = head + rows.map(p => {
      const tech = p.tech
        ? `<span class="badge muted">${esc(p.tech.resolution || '?')} ${esc(p.tech.codec || '')} · ${p.tech.size_gb} GB${p.tech.mb_per_min ? ` · ${p.tech.mb_per_min} MB/min` : ''}</span>`
        : '<span class="badge muted">no tech profile</span>';
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString(loc()) : '';
      return `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(p.title || '—')} ${tech}
            <span class="panel-item-meta">${esc(p.category || '')}${when ? ' · ' + when : ''}</span></div>
          <div class="panel-actions">
            <button type="button" class="btn btn-secondary btn-sm" ${act('downscaleDone', p.id, EL)} title="File has been transcoded — protection stays (HARD_KEEP); leaves this list">Done</button>
            ${p.arr_url ? `<a href="${esc(p.arr_url)}" target="_blank" rel="noopener" aria-label="Open ${escAttr(p.title)} in ${esc(_arrLabel(p.category))}" class="btn btn-secondary btn-sm">Open in ${esc(_arrLabel(p.category))}</a>` : ''}
          </div>
        </div>
        ${p.bitrate_note ? `<div class="panel-item-sub">${esc(p.bitrate_note)}</div>` : ''}
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function loadUpgrades() {
  const el = document.getElementById('upgrade-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Scanning for loved-but-lean titles…</p>';
  try {
    const r = await api('/api/recommendations/upgrade-candidates');
    const rows = r.candidates || [];
    if (!rows.length) {
      el.innerHTML = emptyHtml('No upgrade candidates — everything you love is already in decent shape.', null, null, {good: true});
      return;
    }
    el.innerHTML = `<div class="fs-12 t2 mb-8">${rows.length} title(s) worth a better version</div>` +
      rows.map(p => `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(p.title || '—')}
            <span class="badge amber">${esc(p.weakness || '')}</span>
            <span class="panel-item-meta">${esc(p.category || '')}${p.size_gb ? ` · ${p.size_gb} GB` : ''}</span></div>
          ${p.arr_url ? `<div class="panel-actions"><a href="${esc(p.arr_url)}" target="_blank" rel="noopener" aria-label="Open ${escAttr(p.title)} in ${esc(_arrLabel(p.category))}" class="btn btn-secondary btn-sm">Open in ${esc(_arrLabel(p.category))}</a></div>` : ''}
        </div>
        <div class="panel-item-sub">Why it qualifies: ${esc(p.love_reason || '')}</div>
        ${p.kind === 'edition' ? `<div class="row mt-4"><button type="button" class="btn btn-secondary btn-sm" ${act('checkUncensored', p.arr_id, EL)} title="Ask Sonarr's indexers (seasons 1–2) for releases named uncensored or from Blu-ray/DVD">Search releases</button></div>` : ''}
      </div>`).join('');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function loadRedundancy() {
  const el = document.getElementById('redundancy-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Auditing redundant storage…</p>';
  try {
    const r = await api('/api/recommendations/redundancy');
    const intra = r.intra_item || [];
    const cross = r.cross_item || [];
    if (!intra.length && !cross.length) {
      el.innerHTML = emptyHtml('No redundant versions found — the library keeps one copy per title.', null, null, {good: true});
      return;
    }
    const CAT = { movie: 'Movies', show: 'TV Shows', anime: 'Anime', music: 'Music' };
    const ARR = { movie: 'radarr', show: 'sonarr', anime: 'sonarr', music: 'lidarr' };
    const links = (e) => {
      const plex = r.plex_web_base && e.plex_rating_key
        ? `<a href="${esc(r.plex_web_base + encodeURIComponent('/library/metadata/' + e.plex_rating_key))}" aria-label="Open ${escAttr(e.title || 'media')} in Plex" target="_blank" class="btn btn-secondary btn-sm">Open in Plex</a>` : '';
      const arr = e.arr_url
        ? `<a href="${esc(e.arr_url)}" target="_blank" aria-label="Open ${escAttr(e.title || 'media')} in ${esc(ARR[e.media_type] || 'arr')}" class="btn btn-secondary btn-sm">Open in ${esc(ARR[e.media_type] || 'arr')}</a>` : '';
      return plex + arr;
    };
    const techBadge = (e) => `<span class="badge muted">${esc(e.resolution || '?')}${e.is_remux ? ' remux' : ''}${e.codec ? ' ' + esc(e.codec) : ''} · ${e.size_gb} GB</span>`;

    const intraCard = (e) => `<div class="panel-item">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(e.title)}
          <span class="badge muted">${esc(CAT[e.media_type] || e.media_type || '')}</span>
          <span class="badge muted">${e.versions} versions</span>
          <span class="badge amber">${e.redundant_gb} GB redundant</span></div>
        <div class="panel-actions">${links(e)}</div>
      </div>
      ${(e.files || []).length
        ? `<div class="panel-item-sub">${e.files.map(f =>
            `${esc(f.resolution || '?')} · ${f.size_gb} GB · ${esc(f.file || '')}`).join('<br>')}</div>`
        : `<div class="panel-item-sub">Primary: ${esc(e.resolution || '?')}${e.is_remux ? ' remux' : ''} — per-file detail unavailable (series aggregate).</div>`}
    </div>`;

    const crossCard = (g) => `<div class="panel-item">
      <div class="panel-item-head">
        <div class="panel-item-title">${esc(g.title)}
          <span class="badge muted">${g.count} copies</span>
          <span class="badge amber">${g.redundant_gb} GB redundant</span></div>
      </div>
      <div class="panel-item-sub">${(g.copies || []).map(c =>
        `<div class="row mt-4">
          <span>${esc(CAT[c.media_type] || c.media_type || '')} · ${esc(c.resolution || '?')}${c.is_remux ? ' remux' : ''} · ${c.size_gb} GB</span>
          <span class="row row-end">${links(c)}</span>
        </div>`).join('')}</div>
    </div>`;

    el.innerHTML =
      `<div class="fs-12 t2 mb-8">Total reclaimable: <b class="t-amber">${r.total_redundant_gb || 0} GB</b> · ${intra.length + cross.length} title(s)</div>` +
      (intra.length ? `<div class="list-head"><span>Multiple qualities of one item</span><span class="fw-400 fs-12 t3">${r.intra_redundant_gb} GB</span></div>` +
        intra.map(intraCard).join('') : '') +
      (cross.length ? `<div class="list-head"><span>Same title as separate items</span><span class="fw-400 fs-12 t3">${r.cross_redundant_gb} GB</span></div>` +
        cross.map(crossCard).join('') : '');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function downscaleDone(id, btn) {
  btnBusy(btn);
  try {
    await api('/api/recommendations/downscale/' + encodeURIComponent(id) + '/done', 'POST');
    toast('Marked done — the protection stays', 'success');
    loadDownscale();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

export function curationSection(kind, el) {
  if (!el.open) return;
  if (kind === 'protections') loadJudgeProtections();
  else if (kind === 'downscale') loadDownscale();
  else if (kind === 'upgrades') loadUpgrades();
  else if (kind === 'redundancy') loadRedundancy();
  else if (kind === 'principles') loadPrinciples();
}


// Series editions: the upgrade row's on-demand release search through Sonarr.
export async function checkUncensored(arrId, btn) {
  btnBusy(btn, 'Searching…');
  try {
    const r = await api(`/api/library/editions/${encodeURIComponent(arrId)}/releases`);
    const names = (r.releases || []).slice(0, 3).map(x => x.title).join(' · ');
    toast(r.total ? `${r.total} release(s) uncensored by name or from Blu-ray/DVD: ${names}` : 'No release named uncensored or from Blu-ray/DVD on your indexers (seasons 1–2)', r.total ? 'success' : 'info', {ms: 10000});
    btnDone(btn, r.total ? `${r.total} found` : 'None found', {revertMs: 6000});
  } catch (e) {
    toast(_errMsg(e), 'danger', {ms: 8000});
    btnDone(btn);
  }
}
