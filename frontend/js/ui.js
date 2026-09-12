import { state } from './state.js';

export const API = '';
// Moved state.token to state
// Moved state.pendingDiscussContext to state
// Moved state.currentUser to state
// Moved state.pollInterval to state
let lastJobId = null;
// Moved state.libraryCfg to state
// Moved setupData to state
// Moved state.setupStep to state
export const SETUP_STEPS = ['plex','ollama','metadata','arr','import','done'];
export const CAT_LABELS = {music:'Music', movie:'Movies', show:'TV Shows', anime:'Anime'};
// Small inline status glyphs — reuse the sidebar's stroke-icon language
// (currentColor so they follow whatever text color already wraps them)
// instead of platform emoji, which render inconsistently across OSes.
export const SVG_WARN = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;flex-shrink:0"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';
export const SVG_CHECK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="vertical-align:-2px;flex-shrink:0"><polyline points="20 6 9 17 4 12"/></svg>';
export const SVG_X = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" style="vertical-align:-2px;flex-shrink:0"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
export const SVG_TRASH = '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/></svg>';
export const SVG_STAR = '<svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor" stroke="none" style="vertical-align:-1px;flex-shrink:0"><polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26"/></svg>';

// A failed api() call throws the raw response body as e.message -- for a
// FastAPI error that's a JSON blob like {"detail":"..."}. Pull the human
// part out instead of dumping that JSON straight into the UI.
export function _errMsg(e) {
  try {
    const j = JSON.parse(e?.message || '');
    if (j?.detail) return typeof j.detail === 'string' ? j.detail : JSON.stringify(j.detail);
  } catch {}
  return e?.message || 'Something went wrong';
}
// Standard "this view failed to load" block: reuses the .loading slot's
// layout but marks it as an error, and always offers a way back in instead
// of leaving the view stuck on a bare "Loading…" forever.
export function _errHtml(e, retryFn) {
  const retry = retryFn ? `<br><button class="btn btn-secondary btn-sm" style="margin-top:8px" onclick="${esc(retryFn)}">Retry</button>` : '';
  return `<p class="load-err">${SVG_WARN} Couldn't load — ${esc(_errMsg(e))}</p>${retry}`;
}

// Shared poster/cover-art thumbnail. Every card that puts a title next to
// a small image (library rows, add-new results, recs, deletion proposals)
// used to hand-roll this with slightly different sizes and a slightly
// different "no image" fallback (an empty box, a "?" box, or nothing at
// all, depending which one you were looking at). One helper now owns the
// sizing, the proxyImg() routing, and the music round-crop.
export function _posterImg(url, w, h, isMusic) {
  const radius = isMusic ? '50%' : '4px';
  if (!url) {
    return `<div style="width:${w}px;height:${h}px;background:var(--bg3);border-radius:${radius};flex-shrink:0;display:flex;align-items:center;justify-content:center;color:var(--text3);font-size:${Math.round(w*0.4)}px">?</div>`;
  }
  // TMDB carries its size as a path segment (.../t/p/w92/xyz.jpg) --
  // swapping just that segment gets a sharper (or smaller) variant of the
  // exact same image, whatever size the backend happens to send today.
  // The browser picks from `sizes` vs its real viewport/DPR -- no manual
  // screen-size branching built here, same URL trick works once the
  // backend's own w92->w500 fix (see recommendations.py) lands too.
  const sizeMatch = url.match(/\/(w\d+)\//);
  const srcset = sizeMatch
    ? ['w185', 'w342', 'w500', 'w780'].map(sz => `${esc(proxyImg(url.replace(sizeMatch[1], sz)))} ${sz.slice(1)}w`).join(', ')
    : '';
  const srcsetAttr = srcset ? ` srcset="${srcset}" sizes="${w}px"` : '';
  return `<img src="${esc(proxyImg(url))}"${srcsetAttr} alt="" style="width:${w}px;height:${h}px;object-fit:cover;border-radius:${radius};flex-shrink:0;background:var(--bg3)" onerror="this.style.display='none'">`;
}

// ── UI GRAMMAR HELPERS ──────────────────────────────────────────────────────
// One implementation per job — toast, modal, confirm, overflow menu, pager,
// button state, badge counts, empty states, times. Views compose these and
// build no overlay, alert() or prompt() of their own
// (tests/test_frontend_hygiene.py ratchets the old mechanisms to zero).

export function _mount(id) {
  let el = document.getElementById(id);
  if (!el) { el = document.createElement('div'); el.id = id; document.body.appendChild(el); }
  return el;
}

// toast(text, kind, {title, ms, actions:[{label, primary, onClick}]}) — bottom
// right, newest last, click dismisses. kind: info | success | danger | amber
// ('error' is accepted as danger). The same text is not repeated within 5 s.
// A toast with actions stays until one of them resolves (onClick may return
// false to keep it open).
const _toastRecent = new Map();
export function toast(text, kind = 'info', opts = {}) {
  const stack = _mount('toast-stack');
  if (kind === 'error') kind = 'danger';
  const key = `${kind}|${text}`, now = Date.now();
  if (!opts.actions && _toastRecent.get(key) > now - 5000) return null;
  _toastRecent.set(key, now);
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'danger' ? 'alert' : 'status');
  el.innerHTML = (opts.title ? `<div class="toast-title">${esc(opts.title)}</div>` : '') + `<div>${esc(text)}</div>` +
    (opts.actions?.length ? `<div class="toast-actions">${opts.actions.map((a, i) =>
      `<button type="button" class="btn btn-sm ${a.primary ? 'btn-primary' : 'btn-secondary'}" data-i="${i}">${esc(a.label)}</button>`).join('')}</div>` : '');
  const close = () => { if (el.parentNode) el.remove(); };
  if (opts.actions?.length) {
    el.classList.add('has-actions');   // a question is never evicted by later toasts
    el.querySelectorAll('.toast-actions button').forEach(b => b.onclick = ev => {
      ev.stopPropagation();
      const a = opts.actions[Number(b.dataset.i)];
      Promise.resolve(a.onClick?.(el)).then(r => { if (r !== false) close(); });
    });
  } else {
    el.onclick = close;
    setTimeout(close, opts.ms || (kind === 'danger' ? 6000 : 3500));
  }
  const plain = () => [...stack.children].filter(t => !t.classList.contains('has-actions'));
  while (plain().length >= 4) plain()[0].remove();
  stack.appendChild(el);
  return el;
}
const showToast = toast;

// openModal({title, body, foot, size: 'narrow'|'wide', danger, onClose}) → {el, close}
// One layer: opening a second modal replaces the first. Escape, the backdrop
// and the X all close it; the first control gets focus. body/foot are HTML.
// Moved state._modal to state
export function openModal(o = {}) {
  closeModal();
  const root = _mount('modal-root');
  root.innerHTML = `<div class="modal ${o.size || ''}${o.danger ? ' danger' : ''}" role="dialog" aria-modal="true" aria-label="${escAttr(o.title || '')}">
      <div class="modal-head"><h3>${esc(o.title || '')}</h3><button type="button" class="modal-close" aria-label="Close" onclick="closeModal()">×</button></div>
      <div class="modal-body">${o.body || ''}</div>
      ${o.foot ? `<div class="modal-foot">${o.foot}</div>` : ''}
    </div>`;
  root.classList.add('open');
  root.onclick = ev => { if (ev.target === root) closeModal(); };
  const el = root.firstElementChild;
  state._modal = {el, onClose: o.onClose};
  const first = el.querySelector('.modal-body input:not([type=hidden]), .modal-body select, .modal-body textarea, .modal-foot button:not([disabled])');
  if (first) setTimeout(() => first.focus(), 0);
  return {el, close: closeModal};
}
export function closeModal() {
  const root = document.getElementById('modal-root');
  const m = state._modal;
  state._modal = null;
  if (root) { root.classList.remove('open'); root.innerHTML = ''; }
  if (m?.onClose) { try { m.onClose(); } catch (_) {} }
}

// confirmDialog({title, body, confirmLabel, cancelLabel, danger, countdown,
//                reason: {label, placeholder, value}}) → Promise<{ok, reason}>
// Every destructive action goes through here. countdown: N keeps the confirm
// button disabled for N seconds ("Delete (3)") — the deliberate pause the
// delete flow always had. reason renders a free-text field whose value comes
// back trimmed (Curatarr learns from it).
export function confirmDialog(o = {}) {
  return new Promise(resolve => {
    let timer = null, done = false;
    const label = o.confirmLabel || 'Confirm';
    const finish = ok => {
      if (done) return;
      done = true;
      if (timer) clearInterval(timer);
      const reason = (document.getElementById('confirm-reason')?.value || '').trim();
      closeModal();
      resolve({ok, reason});
    };
    const reasonHtml = o.reason
      ? `<label class="stack mt-12 fs-12 t2" for="confirm-reason">${esc(o.reason.label || 'Reason (optional — Curatarr learns from it)')}
           <input id="confirm-reason" class="input" style="width:100%" placeholder="${escAttr(o.reason.placeholder || '')}" value="${escAttr(o.reason.value || '')}"></label>`
      : '';
    openModal({
      title: o.title || 'Are you sure?', size: 'narrow', danger: !!o.danger,
      body: `${o.body || ''}${reasonHtml}${o.countdown ? `<div class="confirm-count" id="confirm-count">${o.countdown}</div>` : ''}`,
      foot: `<button type="button" class="btn btn-secondary row-end" id="confirm-cancel">${esc(o.cancelLabel || 'Cancel')}</button>
             <button type="button" class="btn ${o.danger ? 'btn-danger' : 'btn-primary'}" id="confirm-ok"${o.countdown ? ' disabled' : ''}>${esc(label)}${o.countdown ? ` (${o.countdown})` : ''}</button>`,
      onClose: () => finish(false),
    });
    const okBtn = document.getElementById('confirm-ok');
    document.getElementById('confirm-cancel').onclick = () => finish(false);
    okBtn.onclick = () => finish(true);
    document.getElementById('confirm-reason')?.addEventListener('keydown', ev => { if (ev.key === 'Enter' && !okBtn.disabled) finish(true); });
    if (o.countdown) {
      let s = o.countdown;
      const cd = document.getElementById('confirm-count');
      timer = setInterval(() => {
        s -= 1;
        if (cd) cd.textContent = s > 0 ? String(s) : '';
        okBtn.textContent = s > 0 ? `${label} (${s})` : label;
        if (s <= 0) { clearInterval(timer); timer = null; okBtn.disabled = false; }
      }, 1000);
    }
  });
}

// menuHtml([{label, call, href, danger, title, attrs, sep}], label) — the
// "More" overflow behind a row's visible actions. `call` is the inline
// handler (rows are string templates; `this` is the menu item, so pass data
// through `attrs`, e.g. attrs: 'data-pid="7"'); `href` renders a link.
export function menuHtml(items, label = 'More') {
  const list = (items || []).filter(Boolean);
  if (!list.length) return '';
  return `<span class="menu"><button type="button" class="btn btn-secondary btn-sm" aria-haspopup="true" aria-expanded="false" onclick="toggleMenu(this)">${esc(label)} ▾</button><div class="menu-list" role="menu">${list.map(it =>
    it.sep ? '<div class="menu-sep"></div>'
    : it.href ? `<a class="menu-item${it.danger ? ' danger' : ''}" role="menuitem" href="${escAttr(it.href)}" target="_blank" rel="noopener"${it.title ? ` title="${escAttr(it.title)}"` : ''}>${esc(it.label)}</a>`
    : `<button type="button" class="menu-item${it.danger ? ' danger' : ''}" role="menuitem" onclick="${escAttr(it.call || '')}"${it.attrs ? ' ' + it.attrs : ''}${it.title ? ` title="${escAttr(it.title)}"` : ''}>${esc(it.label)}</button>`).join('')}</div></span>`;
}
export function toggleMenu(btn) {
  const menu = btn.closest('.menu');
  const wasOpen = menu.classList.contains('open');
  document.querySelectorAll('.menu.open').forEach(m => m.classList.remove('open'));
  if (!wasOpen) menu.classList.add('open');
  btn.setAttribute('aria-expanded', String(!wasOpen));
}

// pagerHtml({offset, limit, total, call}) — "1–50 of 812 · Prev · Next";
// `call` is the loader expression with {offset} as the placeholder, e.g.
// "loadKbItems('movie','not_found',{offset})". Nothing renders when
// everything fits on one page.
export function pagerHtml(p) {
  const total = Number(p.total || 0), limit = Number(p.limit || 50), offset = Number(p.offset || 0);
  if (offset === 0 && total <= limit) return '';
  const from = total ? offset + 1 : 0, to = Math.min(offset + limit, total);
  const go = o => escAttr(p.call.replace('{offset}', String(o)));
  return `<div class="pager"><span>${from.toLocaleString()}–${to.toLocaleString()} of ${total.toLocaleString()}</span><span class="row-end"></span>
    <button type="button" class="btn btn-secondary btn-sm"${offset <= 0 ? ' disabled' : ''} onclick="${go(Math.max(0, offset - limit))}">Prev</button>
    <button type="button" class="btn btn-secondary btn-sm"${to >= total ? ' disabled' : ''} onclick="${go(offset + limit)}">Next</button></div>`;
}

// btnBusy(btn, label) / btnDone(btn, label, {revertMs, keepDisabled}) — the
// pressed button tells its own story; everything beyond it goes to toast().
export function btnBusy(btn, label = '…') {
  if (!btn) return;
  if (btn.dataset.label === undefined) btn.dataset.label = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
}
export function btnDone(btn, label, o = {}) {
  if (!btn) return;
  btn.textContent = label ?? btn.dataset.label ?? btn.textContent;
  btn.disabled = !!o.keepDisabled;
  if (o.revertMs) setTimeout(() => { btn.textContent = btn.dataset.label ?? btn.textContent; btn.disabled = false; }, o.revertMs);
}

// setBadge(id, n) — count bubbles (#kb-badge, #tasks-badge, tab counts):
// hidden at 0, capped at 999+.
export function setBadge(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  n = Number(n) || 0;
  el.textContent = n > 999 ? '999+' : String(n);
  el.style.display = n > 0 ? 'inline' : 'none';
}

// emptyHtml(html, ctaLabel, ctaCall, {good}) — one sentence (HTML, the caller
// escapes), at most one action. good: the empty state is the happy case.
export function emptyHtml(html, ctaLabel, ctaCall, o = {}) {
  return `<div class="empty${o.good ? ' good' : ''}" role="status"><p>${html}</p>${ctaLabel ? `<button type="button" class="btn btn-secondary btn-sm" onclick="${escAttr(ctaCall || '')}">${esc(ctaLabel)}</button>` : ''}</div>`;
}

// _fmtRel(iso) → "in 3 d" / "2 h ago"; pair it with _fmtAbs(iso) in title=.
export function _fmtRel(iso) {
  if (!iso) return '';
  const t = new Date(iso).getTime();
  if (isNaN(t)) return '';
  const d = t - Date.now(), a = Math.abs(d);
  if (a < 60e3) return 'now';
  const [n, u] = a < 3600e3 ? [Math.round(a / 60e3), 'min'] : a < 86400e3 ? [Math.round(a / 3600e3), 'h'] : [Math.round(a / 86400e3), 'd'];
  return d > 0 ? `in ${n} ${u}` : `${n} ${u} ago`;
}
export function _fmtAbs(iso) { const dt = new Date(iso); return isNaN(dt.getTime()) ? '' : dt.toLocaleString(); }

// setStatus(el, text, kind) — the inline status next to a Save / Test button
// (kind: ok | err | busy | none). Extra classes on the element survive.
export function setStatus(el, text, kind) {
  if (!el) return;
  el.classList.remove('ok', 'err', 'busy');
  if (kind) el.classList.add(kind);
  el.textContent = text;
}

// trackDirty(form, saveBtn) — the Save button of a card is disabled until a
// field in its form changes (Sonarr's "No changes / Save changes" rule); the
// caller re-renders after a save, which re-arms it.
export function trackDirty(form, saveBtn) {
  if (!form || !saveBtn) return;
  saveBtn.disabled = true;
  const arm = () => { saveBtn.disabled = false; };
  form.addEventListener('input', arm);
  form.addEventListener('change', arm);
}

// One shape for every "Test connection" result: connected + version, or the
// error, plus the privacy warning the arr test may carry.
export function _showTestResult(span, r) {
  if (!span) return;
  setStatus(span, r.ok ? `connected${r.version ? ' · ' + r.version : ''}` : (r.error || 'failed'), r.ok ? 'ok' : 'err');
  if (r.privacy_warning) span.insertAdjacentHTML('beforeend', `<div class="fs-11 t-amber mt-4">${SVG_WARN} ${esc(r.privacy_warning)}</div>`);
}

// ── UTILS ─────────────────────────────────────────────────────────────────────
export function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}

// Pass 97: image proxy front-end helper.
//
// Rewrites any non-same-origin http(s) URL to /api/image/proxy?src=<encoded>.
// Same-origin and non-http(s) URLs (data:, blob:, relative paths) are
// returned unchanged.
//
// FAIL-CLOSED design (audit follow-up): we used to keep a hostname
// whitelist here too and pass unknown hosts through raw. That was
// fail-OPEN — if the backend ever returned an image URL from a host
// the frontend hadn't been updated to know about, the browser would
// connect directly and leak the user's IP + Referer. Flipping to
// "always proxy" means an unknown host triggers a backend 403 instead
// of a silent leak. The existing ``<img onerror>`` hides the broken
// image, so the worst-case UX is a missing poster — never a leak.
//
// The backend (src/routers/image_proxy.py) remains the single source
// of truth for the host whitelist.
export function proxyImg(rawUrl) {
  if (!rawUrl) return '';
  try {
    const u = new URL(rawUrl, window.location.origin);
    // Only proxy http(s); skip data URIs, blob URLs, etc.
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return rawUrl;
    // Same-origin / relative — never proxy.
    if (u.origin === window.location.origin) return rawUrl;
    // Everything else → through the proxy (fail-closed).
    return '/api/image/proxy?src=' + encodeURIComponent(rawUrl);
  } catch (_) {
    return rawUrl;   // unparseable → return as-is, let <img onerror> hide it
  }
}

// HTML-attribute-safe escape: also handles " and ' (and backtick) so the
// value can be safely interpolated into a data-* attribute or any other
// quoted attribute context. Use this instead of `esc(...).replace(/"/g,'&quot;')`
// chains, and instead of `JSON.stringify(...).replace(/"/g,'&quot;')` for
// embedding objects (encode the JSON string with escAttr instead).
export function escAttr(s){
  return esc(s)
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/`/g, '&#96;');
}
