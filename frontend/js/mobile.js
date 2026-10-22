// ── MOBILE & APP ─────────────────────────────────────────────────────────────
// Everything that makes the UI feel like an app on a phone, in one module:
// the service worker (installable, opens offline), the install button, the
// home-screen icon badge, online/offline state, and the touch gestures
// (edge-swipe drawer, pull-to-refresh, tap the active tab to scroll up).
// Layout itself is CSS (the ≤768px block in app.css); this file only adds
// behaviour, and every piece is a no-op where the browser lacks the API.
import { act, openModal, toast } from './ui.js';
import { showView, toggleMobileSidebar } from './nav.js';
import { state } from './state.js';

const PHONE_MQ = window.matchMedia('(max-width: 768px)');
const REDUCED_MQ = window.matchMedia('(prefers-reduced-motion: reduce)');
const PTR_ARM = 64;          // px of (damped) pull that arms a refresh
const EDGE_PX = 24;          // a drawer swipe has to start this close to the left edge
const SWIPE_PX = 56;         // horizontal travel that opens / closes the drawer
// Views where a downward pull means something else (the chat log scrolls
// from the bottom, the swipe card is dragged) — no pull-to-refresh there.
const NO_PTR = new Set(['chat-view', 'swipe-view']);

const _isPhone = () => PHONE_MQ.matches;
const _el = id => document.getElementById(id);
let _installEvt = null;

// Short taps of the vibration motor (Android; iOS has no web API for it).
// Silent under reduced motion — a buzz is motion too. The web API knows no
// strength, only duration, so the device setting (Settings → Account →
// This device) scales length: Light is one pulse at half length (a double
// pulse collapses to its longest half), Full plays the pattern as written.
const HAPTIC_KEY = 'curatarr_haptics';
const HAPTIC_LEVELS = ['off', 'light', 'full'];

export function hapticLevel() {
  try {
    const v = localStorage.getItem(HAPTIC_KEY);
    return HAPTIC_LEVELS.includes(v) ? v : 'light';
  } catch (_) {
    return 'light';
  }
}

export function haptic(pattern = 8) {
  if (REDUCED_MQ.matches || !navigator.vibrate) return;
  const level = hapticLevel();
  if (level === 'off') return;
  if (level === 'light') {
    const pulses = Array.isArray(pattern) ? pattern.filter((_, i) => i % 2 === 0) : [pattern];
    pattern = Math.max(4, Math.round(Math.max(...pulses) / 2));
  }
  try { navigator.vibrate(pattern); } catch (_) {}
}

// The radio group in Settings → Account; the new level buzzes once so the
// choice can be felt right away.
export function setHapticLevel(input) {
  const v = input?.value;
  if (!HAPTIC_LEVELS.includes(v)) return;
  try { localStorage.setItem(HAPTIC_KEY, v); } catch (_) { /* private mode: this session only */ }
  _syncHapticChoice();
  haptic(14);
}

function _syncHapticChoice() {
  const level = hapticLevel();
  for (const r of document.querySelectorAll('input[name="haptics"]')) {
    r.checked = r.value === level;
    r.closest('.chip')?.classList.toggle('active', r.checked);
  }
}

export function isStandalone() {
  return window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
}

function _isIos() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

// ── Service worker ──────────────────────────────────────────────────────────
// Browsers only run one in a secure context: https, or http://localhost.
// A plain-http LAN address still gets the responsive UI, just no install.
function _registerServiceWorker() {
  if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', {scope: '/'})
      .catch(e => console.warn('[pwa] service worker registration failed', e));
  });
}

// ── Install ─────────────────────────────────────────────────────────────────
function _bindInstall() {
  const btn = _el('pwa-install');
  window.addEventListener('beforeinstallprompt', e => {
    e.preventDefault();             // our own button, not the mini-infobar
    _installEvt = e;
    if (btn) btn.hidden = false;
  });
  window.addEventListener('appinstalled', () => {
    _installEvt = null;
    if (btn) btn.hidden = true;
    toast('Curatarr is installed — find it on your home screen.', 'success');
  });
  // iOS never fires beforeinstallprompt; Safari installs through Share.
  if (btn && _isIos() && !isStandalone()) btn.hidden = false;
}

export async function installApp() {
  if (_installEvt) {
    // A prompt event is single-use; the browser fires a fresh one later
    // if the owner declined, and that brings the button back.
    const e = _installEvt;
    _installEvt = null;
    _el('pwa-install').hidden = true;
    e.prompt();
    await e.userChoice.catch(() => null);
    return;
  }
  openModal({
    title: 'Add Curatarr to your home screen',
    size: 'narrow',
    body: `<ol class="install-steps">
        <li>Tap <b>Share</b> <svg class="icon-inline" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg> in Safari's toolbar.</li>
        <li>Choose <b>Add to Home Screen</b>.</li>
        <li>Tap <b>Add</b> — Curatarr opens full screen from its icon.</li>
      </ol>`,
    foot: `<button class="btn btn-primary w-full" ${act('closeModal')}>Got it</button>`,
  });
}

// ── Topbar search on phones ─────────────────────────────────────────────────
export function toggleTopbarSearch(force) {
  const open = typeof force === 'boolean' ? force : !document.body.classList.contains('search-open');
  document.body.classList.toggle('search-open', open);
  _el('tb-search-btn')?.setAttribute('aria-expanded', String(open));
  if (open) _el('topbar-search')?.focus();
}
function _bindSearchFold() {
  const form = _el('topbar-search-form');
  if (!form) return;
  form.addEventListener('focusout', () => setTimeout(() => {
    if (!form.contains(document.activeElement)) toggleTopbarSearch(false);
  }, 0));
  form.addEventListener('keydown', e => { if (e.key === 'Escape') { _el('topbar-search').blur(); } });
}

// ── App-icon badge + the "More" dot ─────────────────────────────────────────
// The bell's count goes onto the installed app's icon (Badging API); the
// sidebar's own badges, hidden behind the drawer on a phone, light a dot on
// the tab bar's More.
function _bindBadges() {
  const bell = _el('msg-badge');
  if (bell && 'setAppBadge' in navigator) {
    new MutationObserver(() => {
      const n = bell.classList.contains('show') ? parseInt(bell.textContent, 10) || 0 : 0;
      (n ? navigator.setAppBadge(n) : navigator.clearAppBadge()).catch(() => {});
    }).observe(bell, {attributes: true, attributeFilter: ['class'], childList: true, characterData: true, subtree: true});
  }
  const dot = _el('mt-more-dot');
  const watched = ['kb-badge', 'tasks-badge', 'settings-badge', 'sb-deletions-pulse'].map(_el).filter(Boolean);
  if (!dot || !watched.length) return;
  const sync = () => { dot.hidden = watched.every(b => b.hidden); };
  const mo = new MutationObserver(sync);
  watched.forEach(b => mo.observe(b, {attributes: true, attributeFilter: ['hidden']}));
  sync();
}

// ── Online / offline ────────────────────────────────────────────────────────
function _bindConnectivity() {
  const set = online => document.body.classList.toggle('is-offline', !online);
  window.addEventListener('offline', () => {
    set(false);
    toast("You're offline — Curatarr picks up again when the network is back.", 'amber');
  });
  window.addEventListener('online', () => { set(true); toast('Back online.', 'success'); });
  set(navigator.onLine !== false);
}

// ── Gestures ────────────────────────────────────────────────────────────────
// Swipe from the left edge opens the drawer, swiping it left closes it.
function _bindDrawerSwipe() {
  let x0 = null, y0 = 0, closing = false;
  document.addEventListener('touchstart', e => {
    if (!_isPhone() || e.touches.length !== 1 || state._modal) { x0 = null; return; }
    const t = e.touches[0];
    closing = _el('sidebar').classList.contains('mobile-open');
    // The swipe card has its own horizontal drag; it must not open the drawer too.
    const onCard = !!e.target.closest?.('.swipe-card');
    x0 = (closing || (t.clientX <= EDGE_PX && !onCard)) ? t.clientX : null;
    y0 = t.clientY;
  }, {passive: true});
  document.addEventListener('touchend', e => {
    if (x0 === null) return;
    const t = e.changedTouches[0];
    const dx = t.clientX - x0, dy = Math.abs(t.clientY - y0);
    x0 = null;
    if (dy > Math.abs(dx)) return;                 // mostly vertical: a scroll
    if (!closing && dx > SWIPE_PX) { toggleMobileSidebar(true); haptic(); }
    else if (closing && dx < -SWIPE_PX) { toggleMobileSidebar(false); haptic(); }
  }, {passive: true});
}

// Pull down at the top of a view to reload it (showView re-runs its loader).
function _bindPullToRefresh() {
  const ptr = _el('ptr');
  if (!ptr) return;
  let y0 = null, x0 = 0, pull = 0, armed = false, scroller = null;
  const reset = () => {
    y0 = null; pull = 0; armed = false;
    ptr.classList.remove('show', 'armed');
    ptr.style.removeProperty('--pull');
  };
  document.addEventListener('touchstart', e => {
    if (!_isPhone() || e.touches.length !== 1 || state._modal || ptr.classList.contains('spinning')) return;
    const view = document.querySelector('.view.active');
    scroller = e.target.closest?.('.view-body');
    if (!view || NO_PTR.has(view.id) || !scroller || !view.contains(scroller) || scroller.scrollTop > 0) return;
    y0 = e.touches[0].clientY; x0 = e.touches[0].clientX;
  }, {passive: true});
  document.addEventListener('touchmove', e => {
    if (y0 === null) return;
    const dy = e.touches[0].clientY - y0, dx = Math.abs(e.touches[0].clientX - x0);
    if (dy <= 0 || dx > dy || scroller.scrollTop > 0) { reset(); return; }
    pull = Math.min(dy * 0.5, 110);              // damped: the indicator trails the finger
    ptr.style.setProperty('--pull', String(pull));
    ptr.classList.add('show');
    if (!armed && pull >= PTR_ARM) haptic(6);
    armed = pull >= PTR_ARM;
    ptr.classList.toggle('armed', armed);
  }, {passive: true});
  const end = () => {
    if (y0 === null) return;
    if (!armed) { reset(); return; }
    const view = document.querySelector('.view.active')?.id?.replace(/-view$/, '');
    reset();
    ptr.classList.add('show', 'spinning');
    haptic(12);
    if (view) showView(view, null, {push: false});
    setTimeout(() => ptr.classList.remove('show', 'spinning'), 750);
  };
  document.addEventListener('touchend', end, {passive: true});
  document.addEventListener('touchcancel', reset, {passive: true});
}

// Tapping the tab you are already on scrolls its view back to the top.
function _bindTabBar() {
  const bar = _el('mobile-tabbar');
  if (!bar) return;
  bar.addEventListener('click', e => {
    const item = e.target.closest('.mt-item');
    if (!item) return;
    haptic(5);
    if (item.tagName === 'A' && item.classList.contains('active')) {
      const view = document.querySelector('.view.active');
      view?.querySelector('.view-body')?.scrollTo({top: 0, behavior: REDUCED_MQ.matches ? 'auto' : 'smooth'});
    }
  });
}

// Chat input grows with what is typed (up to the CSS max-height).
function _bindAutogrow() {
  document.addEventListener('input', e => {
    const t = e.target;
    if (t?.id !== 'chat-input') return;
    t.style.height = 'auto';
    t.style.height = Math.min(t.scrollHeight + 2, 160) + 'px';
  });
}

// The desktop placeholder wraps to three lines in a phone-width pill.
const CHAT_PLACEHOLDER_PHONE = 'Ask Curatarr anything…';
function _syncChatPlaceholder() {
  const inp = _el('chat-input');
  if (!inp) return;
  inp.dataset.placeholderWide ??= inp.placeholder;
  inp.placeholder = _isPhone() ? CHAT_PLACEHOLDER_PHONE : inp.dataset.placeholderWide;
}

// Leaving the phone layout (rotating a tablet, resizing a window) must not
// strand an open drawer or a folded-out search.
function _bindBreakpoint() {
  PHONE_MQ.addEventListener?.('change', ev => {
    _syncChatPlaceholder();
    if (!ev.matches) { toggleMobileSidebar(false); toggleTopbarSearch(false); }
  });
}

export function initMobile() {
  if (isStandalone()) document.body.classList.add('standalone');
  _registerServiceWorker();
  _bindInstall();
  _bindSearchFold();
  _bindBadges();
  _bindConnectivity();
  _bindDrawerSwipe();
  _bindPullToRefresh();
  _bindTabBar();
  _bindAutogrow();
  _bindBreakpoint();
  _syncChatPlaceholder();
  _syncHapticChoice();
}
