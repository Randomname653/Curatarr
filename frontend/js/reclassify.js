// ── Reclassify (Manage → anime ↔ TV library config audit) ────────────────
// noAutoReplace: cards carry unsaved checkbox selections (and, for
// "uncertain" items, a picked direction) that moveReclassify() reads
// straight from the DOM -- a background revalidation must never silently
// redraw these out from under a selection in progress. moveReclassify()
// invalidates explicitly before its own reload, so a real refresh after an
// apply still shows the post-apply state, not a stale one.
import { _errHtml, _errMsg, _posterImg, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, toast } from './ui.js';
import { _swrCache, _swrInvalidate, _swrRun, api } from './api.js';
export function _renderReclassify(res) {
  const el = document.getElementById('reclassify-content');
  const sum = document.getElementById('reclassify-summary');
  if (res.error) { el.innerHTML = `<p class="load-err">${esc(res.error)}</p>`; return; }
  const c = res.counts || {};
  if (sum) sum.textContent = `${c.to_tv||0} → TV · ${c.to_anime||0} → Anime · ${c.fix_settings||0} settings · ${c.uncertain||0} uncertain`;
  const sections = [
    ['Western cartoons in Anime → move to TV', res.to_tv, 'fix'],
    ['Anime in the TV library → move to Anime', res.to_anime, 'fix'],
    ['Right library, wrong settings → fix in place', res.fix_settings, 'fix'],
    ['Uncertain — pick a direction, or leave it', res.uncertain, 'uncertain'],
  ];
  let html = '';
  for (const [label, items, mode] of sections) {
    if (!items || !items.length) continue;
    const all = mode === 'fix' ? `<label class="chip row-end"><input type="checkbox" onchange="rcToggleSection(this)"> select all</label>` : '';
    html += `<div class="list-head"><span>${label}</span><span class="fs-12 t3" style="font-weight:400">${items.length}</span>${all}</div>`;
    html += '<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(360px,1fr));gap:10px">';
    for (const it of items) html += reclassifyCard(it, mode);
    html += '</div>';
  }
  el.innerHTML = html || emptyHtml('Everything is correctly filed and configured.', null, null, {good: true});
  updateReclassifyCount();
}

export async function loadReclassify() {
  const el = document.getElementById('reclassify-content');
  const sum = document.getElementById('reclassify-summary');
  if (!_swrCache.has('reclassify')) {
    el.innerHTML = '<p class="loading" role="status" aria-live="polite">Scanning Sonarr (first run resolves origins, ~15s)…</p>';
    if (sum) sum.textContent = '';
  }
  try {
    await _swrRun('reclassify', () => api('/api/library/reclassify/scan'), _renderReclassify, { noAutoReplace: true });
  } catch (e) {
    el.innerHTML = _errHtml(e, 'loadReclassify()');
  }
}

// One settings row: greyed when unchanged, struck-through → green when it's an issue.
export function _rcRow(from, to, changed) {
  if (!changed) return `<div class="fs-11 t3">${esc(from || '—')}</div>`;
  return `<div class="fs-11"><span class="t-danger" style="text-decoration:line-through">${esc(from || '—')}</span> <span class="t3">→</span> <span class="t-success b">${esc(to || '—')}</span></div>`;
}

export function reclassifyCard(it, mode) {
  const cu = it.current, ex = it.expected || {}, iss = it.issues || [];
  const poster = _posterImg(it.poster, 48, 72, false);
  const link = it.sonarr_link
    ? ` <a href="${escAttr(it.sonarr_link)}" target="_blank" rel="noopener" aria-label="Open ${escAttr(it.title)} in Sonarr" title="Open in Sonarr" class="t-amber fs-11" style="text-decoration:none">↗</a>` : '';
  const card = (inner) => `<div class="panel-item rc-card">${inner}</div>`;

  if (mode === 'uncertain') {
    // No confident origin — the admin chooses. Hidden checkbox carries both
    // candidate fixes; the direction chips select one.
    const fa = escAttr(JSON.stringify(it.fix_to_anime || {}));
    const ft = escAttr(JSON.stringify(it.fix_to_tv || {}));
    const dirBtn = (dir, txt) => `<button type="button" class="chip rc-dir" onclick="rcPickUncertain(this,'${dir}')">${txt}</button>`;
    return card(`<input type="checkbox" class="rc-cb" data-id="${it.sonarr_id}" data-title="${escAttr(it.title)}" data-fix="" data-fa='${fa}' data-ft='${ft}' hidden>
      ${poster}
      <div class="grow">
        <div class="b" style="font-size:12.5px;margin-bottom:3px">${esc(it.title)}${link}</div>
        <div class="fs-11 t3">${esc(cu.library)} · ${esc(cu.series_type || '')} · ${esc(cu.profile || '—')}</div>
        <div class="row mt-8" style="gap:5px">${dirBtn('tv', '→ TV')}${dirBtn('anime', '→ Anime')}${dirBtn('skip', 'skip')}</div>
      </div>`);
  }

  const body = _rcRow(cu.library, ex.library, iss.includes('root'))
             + _rcRow(cu.series_type, ex.series_type, iss.includes('type'))
             + _rcRow(cu.profile, ex.profile, iss.includes('profile'));
  const moveTag = (it.fix && it.fix.moveFiles)
    ? ` <span class="badge amber badge-sm" title="Sonarr queues a physical move of the files">move files</span>` : '';
  return card(`<input type="checkbox" class="rc-cb" data-id="${it.sonarr_id}" data-title="${escAttr(it.title)}" data-fix='${escAttr(JSON.stringify(it.fix || {}))}' onchange="updateReclassifyCount()" style="margin-top:3px;flex-shrink:0">
    ${poster}
    <div class="grow">
      <div class="b" style="font-size:12.5px;margin-bottom:3px">${esc(it.title)}${link}${moveTag}</div>
      ${body}
    </div>`);
}

export function rcPickUncertain(btn, dir) {
  const card = btn.closest('.rc-card');
  if (!card) return;
  const cb = card.querySelector('.rc-cb');
  card.querySelectorAll('.rc-dir').forEach(b => b.classList.remove('active'));
  if (dir === 'skip') {
    cb.checked = false;
    cb.dataset.fix = '';
  } else {
    btn.classList.add('active');
    cb.dataset.fix = (dir === 'anime') ? cb.dataset.fa : cb.dataset.ft;
    cb.checked = true;
  }
  updateReclassifyCount();
}

export function rcToggleSection(master) {
  const grid = master.closest('.list-head').nextElementSibling;
  if (grid) grid.querySelectorAll('.rc-cb').forEach(cb => { cb.checked = master.checked; });
  updateReclassifyCount();
}

// The selection bar at the bottom carries Apply; it shows only with a selection.
export function updateReclassifyCount() {
  const n = document.querySelectorAll('#reclassify-content .rc-cb:checked').length;
  const bar = document.getElementById('rc-select-bar');
  const btn = document.getElementById('reclassify-move-btn');
  if (bar) bar.classList.toggle('show', n > 0);
  const count = document.getElementById('rc-select-count');
  if (count) count.textContent = `${n} selected`;
  if (btn) { btn.textContent = 'Apply selected'; btn.disabled = n === 0; }
}
export function rcClearSelection() {
  document.querySelectorAll('#reclassify-content .rc-cb:checked').forEach(cb => { cb.checked = false; if (cb.dataset.fa !== undefined) cb.dataset.fix = ''; });
  document.querySelectorAll('#reclassify-content .rc-dir.active').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('#reclassify-content .list-head input[type=checkbox]').forEach(m => { m.checked = false; });
  updateReclassifyCount();
}

export async function moveReclassify(btn) {
  const cbs = [...document.querySelectorAll('#reclassify-content .rc-cb:checked')];
  const items = cbs.map(cb => { let f = {}; try { f = JSON.parse(cb.dataset.fix || '{}'); } catch (_) {} return { sonarr_id: +cb.dataset.id, title: cb.dataset.title, fix: f }; })
                   .filter(i => i.fix && i.fix.seriesType);
  if (!items.length) { toast('Nothing actionable selected — pick a direction on the uncertain cards.', 'amber'); return; }
  const moves = items.filter(i => i.fix.moveFiles).length;
  const res = await confirmDialog({
    title: 'Apply to Sonarr', danger: moves > 0, confirmLabel: `Apply ${items.length}`,
    body: `<p>Apply <b>${items.length}</b> change${items.length > 1 ? 's' : ''} to Sonarr.${moves ? ` <b>${moves}</b> will physically move files (queued in Sonarr).` : ''}</p><p class="t3 fs-12 mt-8">Nothing gets re-enriched.</p>`,
  });
  if (!res.ok) return;
  btnBusy(btn, `Applying ${items.length}…`);
  try {
    const r = await api('/api/library/reclassify/apply', 'POST',
      { items: items.map(i => ({ sonarr_id: i.sonarr_id, fix: i.fix })) });
    const fails = (r.results || []).filter(x => !x.ok);
    toast(`Applied ${r.applied || 0}${r.failed ? `, ${r.failed} failed` : ''}`, fails.length ? 'amber' : 'success', {ms: 6000});
    _swrInvalidate('reclassify'); // without this, noAutoReplace above would suppress this intentional re-render too
    await loadReclassify();   // re-scan so the lists reflect the new state
    if (fails.length) {
      // The failure list outlives a toast: a banner above the fresh scan.
      document.getElementById('reclassify-content')?.insertAdjacentHTML('afterbegin',
        `<div class="banner danger"><div class="banner-text"><b>${fails.length} failed</b>${fails.slice(0, 12).map(f => `<div class="mt-4">${esc(f.title || f.sonarr_id)}: ${esc(f.error || '?')}</div>`).join('')}</div></div>`);
    }
  } catch (e) {
    toast('Apply failed: ' + _errMsg(e), 'danger');
  }
  btnDone(btn);
  updateReclassifyCount();
}
