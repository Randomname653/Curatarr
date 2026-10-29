// ── The brand in the page ─────────────────────────────────────────────────────
// Which icon this device shows (modern or classic: Settings → Account → This
// device), the chat's "thinking" ring, and the pulse of the logo's diamond
// while a model is at work. The pieces are inline SVG from index.html
// (#logo, the login wordmark, <template> clones), coloured by the --brand-*
// tokens in app.css.

const ICON_KEY = 'curatarr_icon';
export const ICON_STYLES = ['modern', 'classic'];
const FAVICON = {modern: '/icons/favicon.svg', classic: '/icons/favicon-classic.svg'};

export function iconStyle() {
  try {
    const v = localStorage.getItem(ICON_KEY);
    return ICON_STYLES.includes(v) ? v : 'modern';
  } catch (_) {
    return 'modern';
  }
}

// Each slot's own (modern) wordmark, kept so Modern can come back without a reload.
const _modern = new WeakMap();

export function applyIconStyle() {
  const style = iconStyle();
  const fav = document.querySelector('link[rel="icon"]');
  if (fav) fav.href = FAVICON[style];
  const classic = document.getElementById('brand-logo-classic')?.content.firstElementChild;
  for (const slot of document.querySelectorAll('[data-brand-slot]')) {
    const cur = slot.querySelector('.brand-logo');
    if (!cur) continue;
    if (!_modern.has(slot)) _modern.set(slot, cur.cloneNode(true));
    const own = _modern.get(slot);
    const next = (style === 'classic' && classic ? classic : own).cloneNode(true);
    next.setAttribute('class', own.getAttribute('class'));
    cur.replaceWith(next);
  }
  for (const r of document.querySelectorAll('input[name="icon-style"]')) r.checked = r.value === style;
}

export function setIconStyle(input) {
  const v = input?.value;
  if (!ICON_STYLES.includes(v)) return;
  try { localStorage.setItem(ICON_KEY, v); } catch (_) { /* private mode: this page only */ }
  applyIconStyle();
}

// The mark as a decorative element (shutdown screen, the wrap-up's
// watermark; the text beside it names Curatarr) or as markup for a template
// string.
export function brandMark(cls) {
  const t = document.getElementById('brand-mark')?.content.firstElementChild;
  const m = t ? t.cloneNode(true) : document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  if (cls) m.classList.add(cls);
  m.setAttribute('aria-hidden', 'true');
  m.removeAttribute('role');
  m.removeAttribute('aria-label');
  return m;
}
export function brandMarkHtml(cls) {
  return brandMark(cls).outerHTML;
}

// The classic ring as the chat's placeholder: its four segments light up in
// turn, one per pillar of the judge, around the diamond.
export function thinkingRing() {
  const el = document.createElement('div');
  el.className = 'thinking-ring';
  el.setAttribute('role', 'img');
  el.setAttribute('aria-label', 'Thinking');
  const ring = document.getElementById('brand-ring')?.content.firstElementChild;
  if (ring) el.appendChild(ring.cloneNode(true));
  return el;
}

// A model at work: a reply streaming in this tab, or the server's own report
// (enrichment, a curator call) from the 30 s status poll. While either holds,
// the logo's diamond pulses ruby.
let _local = 0;
let _server = false;
function _syncBusy() {
  document.body.classList.toggle('model-busy', _local > 0 || _server);
}
export function modelWorkStarted() { _local += 1; _syncBusy(); }
export function modelWorkEnded() { _local = Math.max(0, _local - 1); _syncBusy(); }
export function setServerModelBusy(on) { _server = !!on; _syncBusy(); }
