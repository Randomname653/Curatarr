// ── Match picker (dialog): the arr's own lookup + TMDB / AniList candidates,
//    a free id row, and the negative pin. One dialog for the KB page and the
//    deletion cards; the category is resolved server-side, never invented
//    here. Pin closes it (toast + the caller's onDone), "Not this one" only
//    drops that candidate from the list so another can be picked.
import { _errHtml, _errMsg, btnBusy, btnDone, closeModal, emptyHtml, esc, escAttr, openModal, toast } from './ui.js';
import { api } from './api.js';
import { PIN_ID_KINDS } from './kb.js';
let _pickerCtx = null;
export function openMatchPicker(opts) {
  _pickerCtx = opts;
  openModal({
    title: `Fix match — ${opts.title || '?'}${opts.year ? ` (${opts.year})` : ''}`, size: 'wide',
    body: '<div id="match-picker-body"></div>',
    foot: `<span class="fs-11 t3 grow">The pin outranks every automatic match and survives rescans. "Not this one" excludes a candidate for good.</span>
           <button type="button" class="btn btn-secondary btn-sm" onclick="removeFixMatch(this)" title="Remove an existing pin — the item re-resolves automatically again">Unpin existing</button>`,
    onClose: () => { _pickerCtx = null; },
  });
  renderMatchPicker(document.getElementById('match-picker-body'), opts);
}

export async function renderMatchPicker(box, opts) {
  const {service, arrId, title, year, category} = opts;
  box.innerHTML = '<p class="loading" role="status" aria-live="polite">Searching the arr, TMDB and AniList…</p>';
  const q = new URLSearchParams({service, arr_id: String(arrId), title: title || ''});
  if (year) q.set('year', String(year));
  if (category) q.set('category', category);
  try {
    const r = await api(`/api/enrichment/match-candidates?${q.toString()}`);
    const cands = r.candidates || [];
    const cat = r.category || category || '';
    const ctx = `data-svc="${escAttr(service)}" data-id="${Number(arrId)}" data-cat="${escAttr(cat)}" data-title="${escAttr(title || '')}"`;
    const idsOf = c => PIN_ID_KINDS.reduce((o, k) => { if (c[k] != null && c[k] !== '') o[k] = c[k]; return o; }, {});
    const idLine = c => PIN_ID_KINDS.filter(k => c[k]).map(k => `${k.replace('_id', '')}:${esc(String(c[k]))}`).join(' · ');
    box.innerHTML = `
      ${cands.length ? cands.map(c => `
        <div class="panel-item">
          <div class="panel-item-head">
            <div class="panel-item-title">${esc(c.title || '?')}${c.year ? `<span class="t3">(${c.year})</span>` : ''}
              <span class="badge muted badge-sm">${esc(c.kind || '')}</span><span class="t3 fs-11">${idLine(c)}</span></div>
            <div class="panel-actions">
              <button type="button" class="btn btn-primary btn-sm" ${ctx} data-ids="${escAttr(JSON.stringify(idsOf(c)))}" onclick="pickerPin(this)">Pin</button>
              <button type="button" class="btn btn-secondary btn-sm" ${ctx} data-ids="${escAttr(JSON.stringify(idsOf(c)))}" onclick="pickerReject(this)" title="Never resolve to this candidate again">Not this one</button>
            </div>
          </div>
          ${(c.overview || c.disambiguation) ? `<div class="panel-item-sub">${esc(c.overview || c.disambiguation)}</div>` : ''}
        </div>`).join('') : emptyHtml(esc(r.error || 'No candidates found — pin an id directly below.'))}
      <div class="row mt-12">
        <span class="fs-12 t2">Pin an id directly:</span>
        <select class="input" aria-label="Id kind">${PIN_ID_KINDS.map(k => `<option value="${k}">${k}</option>`).join('')}</select>
        <input class="input" style="width:180px" aria-label="Id" placeholder="e.g. 603 or tt0133093" onkeydown="if(event.key==='Enter')this.nextElementSibling.click()">
        <button type="button" class="btn btn-secondary btn-sm" ${ctx} onclick="pickerFreePin(this)">Pin this id</button>
      </div>`;
  } catch (e) { box.innerHTML = _errHtml(e); }
}

export async function _pickerPost(btn, payload, doneText, keepOpen = false) {
  btnBusy(btn);
  try {
    const r = await api('/api/enrichment/match-override', 'POST', payload);
    if (!r.success) { toast(r.error || 'Failed', 'danger'); btnDone(btn); return; }
    toast(r.message || doneText, 'success');
    const done = _pickerCtx?.onDone;
    if (keepOpen) btn.closest('.panel-item')?.remove();   // the excluded candidate leaves the list
    else closeModal();
    if (done) done();
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
export function _pickerBase(btn) {
  const {svc, id, cat, title} = btn.dataset;
  const p = {service: svc, arr_id: Number(id), title: title || undefined};
  if (cat) p.category = cat;
  return p;
}
export function pickerPin(btn)    { return _pickerPost(btn, Object.assign(_pickerBase(btn), JSON.parse(btn.dataset.ids || '{}')), 'Pinned — re-enriches on the next run'); }
export function pickerReject(btn) { return _pickerPost(btn, Object.assign(_pickerBase(btn), {rejected: [JSON.parse(btn.dataset.ids || '{}')]}), 'Excluded for good', true); }
export function pickerFreePin(btn) {
  const wrap = btn.parentElement;
  const kind = wrap.querySelector('select').value, val = wrap.querySelector('input').value.trim();
  if (!val) { toast('Enter an id first.', 'amber'); wrap.querySelector('input').focus(); return; }
  const p = _pickerBase(btn); p[kind] = val;
  return _pickerPost(btn, p, 'Pinned — re-enriches on the next run');
}
export async function removeFixMatch(btn) {
  const c = _pickerCtx;
  if (!c) return;
  btnBusy(btn);
  try {
    const r = await api(`/api/enrichment/match-override/${encodeURIComponent(c.service)}/${Number(c.arrId)}`, 'DELETE');
    if (r.success) { toast(r.message || 'Pin removed — the item re-resolves on the next run', 'success'); closeModal(); if (c.onDone) c.onDone(); }
    else { toast(r.error || 'No pin found', 'amber'); btnDone(btn); }
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
