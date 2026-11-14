// ── Match picker (dialog): the arr's own lookup + TMDB / AniList candidates,
//    a free id row, and the negative pin. One dialog for the KB page and the
//    deletion cards; the category is resolved server-side, never invented
//    here. Pin closes it (toast + the caller's onDone), "Not this one" only
//    drops that candidate from the list so another can be picked.
import { EL, EVENT, _errHtml, _errMsg, act, actOn, btnBusy, btnDone, closeModal, emptyHtml, esc, escAttr, openModal, toast, loadingHtml } from './ui.js';
import { api } from './api.js';
import { PIN_ID_KINDS } from './kb.js';
import { t, th } from './i18n.js';
let _pickerCtx = null;
export function openMatchPicker(opts) {
  _pickerCtx = opts;
  openModal({
    title: t('picker.title', {title: `${opts.title || '?'}${opts.year ? ` (${opts.year})` : ''}`}), size: 'wide',
    body: '<div id="match-picker-body"></div>',
    foot: `<span class="fs-11 t3 grow">${th('picker.foot')}</span>
           <button type="button" class="btn btn-secondary btn-sm" ${act('removeFixMatch', EL)} title="${th('picker.unpin_title')}">${th('picker.unpin')}</button>`,
    onClose: () => { _pickerCtx = null; },
  });
  renderMatchPicker(document.getElementById('match-picker-body'), opts);
}

export async function renderMatchPicker(box, opts) {
  const {service, arrId, title, year, category} = opts;
  box.innerHTML = loadingHtml(t('picker.searching'));
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
              <button type="button" class="btn btn-primary btn-sm" ${ctx} data-ids="${escAttr(JSON.stringify(idsOf(c)))}" ${act('pickerPin', EL)}>${th('picker.pin')}</button>
              <button type="button" class="btn btn-secondary btn-sm" ${ctx} data-ids="${escAttr(JSON.stringify(idsOf(c)))}" ${act('pickerReject', EL)} title="${th('picker.reject_title')}">${th('picker.reject')}</button>
            </div>
          </div>
          ${(c.overview || c.disambiguation) ? `<div class="panel-item-sub">${esc(c.overview || c.disambiguation)}</div>` : ''}
        </div>`).join('') : emptyHtml(r.error ? esc(r.error) : th('picker.no_candidates'))}
      <div class="row mt-12">
        <span class="fs-12 t2">${th('picker.pin_direct')}</span>
        <select class="input" aria-label="${th('picker.id_kind')}">${PIN_ID_KINDS.map(k => `<option value="${k}">${k}</option>`).join('')}</select>
        <input class="w-180 input" aria-label="${th('picker.id')}" placeholder="${th('picker.id_placeholder')}" ${actOn('keydown', 'freePinOnEnter', EVENT, EL)}>
        <button type="button" class="btn btn-secondary btn-sm" ${ctx} ${act('pickerFreePin', EL)}>${th('picker.pin_this')}</button>
      </div>`;
  } catch (e) { box.innerHTML = _errHtml(e); }
}

export async function _pickerPost(btn, payload, doneText, keepOpen = false) {
  btnBusy(btn);
  try {
    const r = await api('/api/enrichment/match-override', 'POST', payload);
    if (!r.success) { toast(r.error || t('picker.save_failed'), 'danger'); btnDone(btn); return; }
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
export function pickerPin(btn)    { return _pickerPost(btn, Object.assign(_pickerBase(btn), JSON.parse(btn.dataset.ids || '{}')), t('picker.pinned')); }
export function pickerReject(btn) { return _pickerPost(btn, Object.assign(_pickerBase(btn), {rejected: [JSON.parse(btn.dataset.ids || '{}')]}), t('picker.excluded'), true); }
export function pickerFreePin(btn) {
  const wrap = btn.parentElement;
  const kind = wrap.querySelector('select').value, val = wrap.querySelector('input').value.trim();
  if (!val) { toast(t('picker.enter_id'), 'amber'); wrap.querySelector('input').focus(); return; }
  const p = _pickerBase(btn); p[kind] = val;
  return _pickerPost(btn, p, t('picker.pinned'));
}
export async function removeFixMatch(btn) {
  const c = _pickerCtx;
  if (!c) return;
  btnBusy(btn);
  try {
    const r = await api(`/api/enrichment/match-override/${encodeURIComponent(c.service)}/${Number(c.arrId)}`, 'DELETE');
    if (r.success) { toast(r.message || t('picker.unpinned'), 'success'); closeModal(); if (c.onDone) c.onDone(); }
    else { toast(r.error || t('picker.no_pin'), 'amber'); btnDone(btn); }
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}

// Enter in the free-id field presses the "Pin this id" button next to it.
export function freePinOnEnter(event, el) { if (event.key === 'Enter') el.nextElementSibling.click(); }
