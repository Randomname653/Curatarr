// ── Protections (judge auto-saves AND chat-intent grants, both liftable) ─────
import { api } from './api.js';
import { SVG_CHECK, _errHtml, _errMsg, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, toast } from './ui.js';
import { state } from './state.js';
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
        ? '<span class="badge amber">CHAT</span>'
        : (p.verdict === 'KEEP_WITH_FLAG'
            ? '<span class="badge amber">KEEP · downscale</span>'
            : '<span class="badge muted">HARD_KEEP</span>');
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString() : '';
      return `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(p.title || '—')} ${badge}
            <span class="panel-item-meta">${esc(p.category || '')}${when ? ' · ' + when : ''}</span></div>
          <div class="panel-actions">
            <button type="button" class="btn btn-secondary btn-sm" onclick="liftProtection(${p.id},this)">Lift protection</button>
            ${p.arr_url ? `<a href="${esc(p.arr_url)}" target="_blank" rel="noopener" aria-label="Open ${escAttr(p.title)} in ${esc(_arrLabel(p.category))}" class="btn btn-secondary btn-sm">Open in ${esc(_arrLabel(p.category))}</a>` : ''}
          </div>
        </div>
        <div class="panel-item-sub" style="white-space:pre-line">${esc(p.reason || '')}</div>
      </div>`;
    }).join('');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function liftProtection(id, btn) {
  const res = await confirmDialog({title: 'Lift protection', confirmLabel: 'Lift', body: '<p>The title is re-judged on the next analysis.</p>'});
  if (!res.ok) return;
  btnBusy(btn);
  try {
    await api('/api/recommendations/protections/' + id, 'DELETE');
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
      el.innerHTML = emptyHtml('Nothing learned yet — principles are captured from your deletion debates once PRINCIPLES_ENABLED is on.');
      return;
    }
    const nActive = rows.filter(p => p.status === 'active').length;
    const condenseBar = nActive >= 2 ? `
      <div class="row mb-8">
        <span class="fs-12 t3">${nActive} active rules</span>
        <button type="button" class="btn btn-secondary btn-sm row-end" onclick="condensePrinciples(this)"
          title="Let the curator consolidate near-duplicate active rules (conservative — usually finds nothing on a clean set)">Condense duplicates</button>
      </div>` : '';
    el.innerHTML = condenseBar + rows.map(p => {
      const st = p.status === 'active'
        ? '<span class="badge success">active</span>'
        : p.status === 'rejected'
          ? '<span class="badge muted">rejected</span>'
          : p.status === 'merged'
            ? '<span class="badge muted" title="Consolidated into a newer rule">merged</span>'
            : '<span class="badge amber">shadow</span>';
      const nov = p.novelty === 'contradiction'
        ? ' <span class="badge danger" title="Conflicts with an existing principle — your call">contradiction</span>'
        : (p.novelty ? ` <span class="badge muted">${esc(p.novelty)}</span>` : '');
      const reinf = (p.times_reinforced || 0) > 0 ? ` <span class="fs-11 t3">×${p.times_reinforced} reinforced</span>` : '';
      const meta = [p.basis, p.category].filter(Boolean).map(esc).join(' · ');
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString() : '';
      const actions = p.status === 'merged'
        ? ''
        : p.status === 'active'
          ? `<button type="button" class="btn btn-secondary btn-sm" onclick="setPrinciple(${p.id},'shadow',this)">Deactivate</button>`
          : `<button type="button" class="btn btn-secondary btn-sm" onclick="setPrinciple(${p.id},'activate',this)">Activate</button>`;
      return `<div class="panel-item">
        <div class="panel-item-head" style="align-items:flex-start">
          <div class="grow" style="min-width:200px">
            <div>${st}${nov}${reinf}</div>
            <div class="fs-13 mt-4">${esc(p.text || '')}</div>
            <div class="panel-item-meta fs-11 mt-4">${meta}${when ? ' · ' + when : ''}${p.related ? ' · vs: ' + esc(p.related) : ''}</div>
          </div>
          <div class="panel-actions">
            ${actions}
            ${p.status === 'merged' ? '' : `<button type="button" class="btn btn-secondary btn-sm" onclick="setPrinciple(${p.id},'reject',this)">Reject</button>`}
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
    await api('/api/recommendations/principles/' + id + '/' + action, 'POST');
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
  if (state.taskEventSource) { try { state.taskEventSource.close(); } catch (e) {} state.taskEventSource = null; }
  try { await api('/api/system/shutdown', 'POST'); } catch (e) { /* server may die before replying */ }
  const ov = document.createElement('div');
  ov.className = 'sd-overlay';
  ov.innerHTML =
    '<div class="sd-ripple"></div><div class="sd-ripple"></div><div class="sd-ripple"></div>' +
    '<div style="position:relative;display:flex;flex-direction:column;align-items:center;gap:16px;color:var(--text)">' +
    '<svg class="sd-logo" width="96" height="96" viewBox="0 0 64 64">' +
    '<defs><linearGradient id="sdg" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="#f7c14a"/><stop offset="1" stop-color="#e5a00d"/></linearGradient></defs>' +
    '<g stroke="url(#sdg)" stroke-width="9" fill="none">' +
    '<path d="M 48.9 17.9 A 22 22 0 0 0 18.2 14.9"/>' +
    '<path d="M 14.9 18.2 A 22 22 0 0 0 14.9 45.8"/>' +
    '<path d="M 18.2 49.1 A 22 22 0 0 0 48.9 46.1"/></g>' +
    '<path d="M 40.5 23.5 A 12 12 0 1 0 40.5 40.5" fill="none" stroke="#f0b93a" stroke-width="3.6" stroke-linecap="round"/>' +
    '<circle cx="32" cy="32" r="5" fill="url(#sdg)"/><circle cx="32" cy="32" r="2" fill="#ffd873"/></svg>' +
    '<h2 style="font-weight:600">Curatarr is shutting down…</h2>' +
    '<p style="color:var(--text3);font-size:13px">You can close this tab. Start the server again to come back.</p></div>';
  document.body.appendChild(ov);

  // Wind-down: pulse + ripples run at full speed, then decelerate to a
  // standstill over ~20s (the server is long gone — endless pulsing would
  // suggest activity). playbackRate is eased down phase-continuously.
  const logo = ov.querySelector('.sd-logo');
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
  return category === 'movie' ? 'radarr' : category === 'music' ? 'lidarr' : 'sonarr';
}

export async function loadDownscale() {
  const el = document.getElementById('downscale-content');
  if (!el) return;
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading downscale candidates…</p>';
  try {
    const r = await api('/api/recommendations/downscale');
    const rows = r.candidates || [];
    if (!rows.length) {
      el.innerHTML = emptyHtml('No downscale candidates — KEEP_WITH_FLAG verdicts from the next scan land here.');
      return;
    }
    const head = `<div class="fs-12 t2 mb-8">${rows.length} title(s)${r.total_gb ? ` · ${r.total_gb} GB bound (actual file size)` : ''}</div>`;
    el.innerHTML = head + rows.map(p => {
      const tech = p.tech
        ? `<span class="badge muted">${esc(p.tech.resolution || '?')} ${esc(p.tech.codec || '')} · ${p.tech.size_gb} GB${p.tech.mb_per_min ? ` · ${p.tech.mb_per_min} MB/min` : ''}</span>`
        : '<span class="badge muted">no tech profile</span>';
      const when = p.created_at ? new Date(p.created_at).toLocaleDateString() : '';
      return `<div class="panel-item">
        <div class="panel-item-head">
          <div class="panel-item-title">${esc(p.title || '—')} ${tech}
            <span class="panel-item-meta">${esc(p.category || '')}${when ? ' · ' + when : ''}</span></div>
          <div class="panel-actions">
            <button type="button" class="btn btn-secondary btn-sm" onclick="downscaleDone(${p.id},this)" title="File has been transcoded — protection stays (HARD_KEEP); leaves this list">Done</button>
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
      (intra.length ? `<div class="list-head"><span>Multiple qualities of one item</span><span class="fs-12 t3" style="font-weight:400">${r.intra_redundant_gb} GB</span></div>` +
        intra.map(intraCard).join('') : '') +
      (cross.length ? `<div class="list-head"><span>Same title as separate items</span><span class="fs-12 t3" style="font-weight:400">${r.cross_redundant_gb} GB</span></div>` +
        cross.map(crossCard).join('') : '');
  } catch(e) {
    el.innerHTML = _errHtml(e);
  }
}

export async function downscaleDone(id, btn) {
  btnBusy(btn);
  try {
    await api('/api/recommendations/downscale/' + id + '/done', 'POST');
    toast('Marked done — the protection stays', 'success');
    loadDownscale();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
