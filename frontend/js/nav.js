// ── SIDEBAR COLLAPSE ─────────────────────────────────────────────────────────
import { loadSwipe } from './swipe.js';
import { REC_LANES, loadRecs, openRecLane, searchLibrary } from './recs.js';
import { state } from './state.js';
import { loadHistoryStatus } from './history.js';
import { loadTaskHistory } from './activity.js';
import { openSettingsPane } from './settings.js';
import { showKbTab } from './kb.js';
import { loadDeletions } from './deletions.js';
import { arrActiveTab, loadArrPage } from './arr.js';
import { loadReclassify } from './reclassify.js';
import { loadReport } from './report.js';
import { haptic, withTransition } from './ui.js';
export function toggleSidebar() {
  const collapsed = document.getElementById('sidebar').classList.toggle('collapsed');
  localStorage.setItem('curatarr_sidebar_collapsed', collapsed ? '1' : '0');
  document.getElementById('sb-collapse-toggle').title = collapsed ? 'Expand sidebar' : 'Collapse sidebar';
}
// Below 768px the sidebar is an off-canvas drawer (see the responsive
// media query) instead of the desktop expand/collapse rail. `force` lets
// callers state intent (nav click closes, hamburger toggles); harmless
// no-op above the breakpoint since the drawer classes have no visual
// effect there.
export function toggleMobileSidebar(force) {
  const open = typeof force === 'boolean' ? force : !document.getElementById('sidebar').classList.contains('mobile-open');
  document.getElementById('sidebar').classList.toggle('mobile-open', open);
  document.getElementById('sb-backdrop').classList.toggle('show', open);
}

// ── TOPBAR SEARCH ────────────────────────────────────────────────────────────
// Reuses Recommendations' own semantic search wholesale (same input id, same
// render target) instead of re-implementing result rendering here — jump to
// that view, hand it the query, let searchLibrary() do what it already does.
// loadRecs() and searchLibrary() both render into #recs-content -- without
// this, showView('recs') kicks off loadRecs() at the same time
// topbarSearch() is about to call searchLibrary(), and whichever request
// resolves last silently wins, sometimes clobbering the search results
// back to the plain recommendation list. Set right before showView() so
// its recs branch can skip firing loadRecs() at all.
let _skipNextRecsLoad = false;
export function topbarSearch(ev) {
  if (ev) ev.preventDefault();
  const box = document.getElementById('topbar-search');
  const q = box.value.trim();
  if (q.length < 2) return;
  _skipNextRecsLoad = true;
  showView('recs');
  const libInput = document.getElementById('lib-search');
  if (libInput) libInput.value = q;
  box.value = '';
  box.blur();
  searchLibrary();
}

// ── NAV ───────────────────────────────────────────────────────────────────────
// The URL hash names the view (#recs, #settings/users): views can be
// bookmarked and reloaded, and Back/Forward walk the views the owner opened.
// Sidebar entries are plain links; a link click changes the hash and
// routeFromHash() renders it. showView() called from code (a glance tile, a
// discussion jumping to chat) pushes the hash itself — pushState fires no
// hashchange, so nothing renders twice.
const ADMIN_VIEWS = ['deletions', 'curation', 'reclassify', 'report'];

// ── IN-VIEW TABS ──────────────────────────────────────────────────────────────
// A tab inside a view is part of the address the same way a Settings pane
// is: #<view>/<tab> (#deletions/movie, #enrich/attention, #arr-sonarr/anime).
// A view opts in with registerTabRoute(view, {open, current, tabs, fallback}):
//   open(tab)  shows that tab (also what showView runs for the view),
//   current()  the tab on screen now,
//   tabs()     the valid ids (omit it when open() validates them itself),
//   fallback   the default tab: an unknown tab opens it, and the hash leaves
//              it out (#deletions, not #deletions/null).
// The view's tab setter then calls tabToHash(view, tab). A plain #view keeps
// whatever tab was last open, as before.
// `var` and function declarations, not `const`: a view module may register
// from its own top level, which can run before this module's body (nav.js
// and the views import each other), where a const is still uninitialised.
var _tabRouteMap;
function _tabRoutes() { return (_tabRouteMap ??= {}); }
export function registerTabRoute(view, route) { _tabRoutes()[view] = route; }

// Replaced, not pushed, so Back leaves the view instead of stepping through
// its tabs. Only while that view is on screen: a background reload of a
// view's data must not rewrite the address of another.
export function tabToHash(view, tab) {
  if (!document.getElementById(view + '-view')?.classList.contains('active')) return;
  const r = _tabRoutes()[view];
  const want = '#' + view + (tab && tab !== r?.fallback ? '/' + encodeURIComponent(tab) : '');
  if (location.hash !== want) history.replaceState(null, '', want);
}

// undefined → the tab already open; a known tab → it; anything else → the default.
function _openTab(view, tab) {
  const r = _tabRoutes()[view];
  if (!r) return;
  if (tab === undefined || tab === '') tab = r.current();
  else if (r.tabs && !r.tabs().includes(tab)) tab = r.fallback;
  r.open(tab);
}

function _activePane() {
  return document.querySelector('.settings-pane:not([hidden])')?.dataset.pane;
}
const _domTabs = (sel, key) => () => [...document.querySelectorAll(sel)].map(b => b.dataset[key]).filter(Boolean);
registerTabRoute('settings', {open: openSettingsPane, current: () => _activePane() || 'account', tabs: _domTabs('.settings-tab[data-pane]', 'pane'), fallback: 'account'});
registerTabRoute('deletions', {open: tab => loadDeletions(tab || null), current: () => state.currentDelCategory, tabs: _domTabs('#deletions-view .cat-tab[data-cat]', 'cat'), fallback: null});
registerTabRoute('enrich', {open: showKbTab, current: () => state._kbTab, tabs: _domTabs('#kb-tabs .cat-tab[data-kbtab]', 'kbtab'), fallback: 'overview'});
// The arr tab list depends on the server's answer (Lidarr via Plex has its
// own), so loadArrPage validates the tab once it knows the list.
for (const svc of ['sonarr', 'radarr', 'lidarr']) {
  registerTabRoute('arr-' + svc, {open: tab => loadArrPage(svc, tab), current: () => arrActiveTab(svc), fallback: 'all'});
}
registerTabRoute('recs', {open: openRecLane, current: () => state.currentRecsLane || 'all', tabs: () => REC_LANES, fallback: 'all'});

export function routeFromHash(o = {}) {
  if (!state.currentUser) return;            // the login screen owns the page
  const [view, tab] = decodeURIComponent(location.hash.replace(/^#\/?/, '')).split('/');
  const name = view && document.getElementById(view + '-view') ? view : 'chat';
  const current = document.querySelector('.view.active')?.id;
  const route = _tabRoutes()[name];
  // Same view, same tab (or none named): nothing to do (a pushState echo, or a re-click).
  if (current === name + '-view' && (!tab || !route || route.current() === tab)) return;
  showView(name, null, {push: false, focus: o.focus !== false, tab});
}

export function showView(name, btn, o = {}) {
  // Admin-only views — defense-in-depth on top of the hidden nav section + the
  // require_admin endpoints: never render the shell for a non-admin. A link
  // or bookmark to one lands on Chat instead of a blank page.
  if (!state.currentUser?.is_admin && ADMIN_VIEWS.includes(name)) {
    history.replaceState(null, '', '#chat');
    name = 'chat';
  }
  if (o.push !== false && location.hash.replace(/^#\/?/, '').split('/')[0] !== name) {
    history.pushState(null, '', '#' + name);
  }
  toggleMobileSidebar(false); // picking a view closes the mobile drawer
  // The view's own sidebar entry lights up, whoever opened it (every entry
  // names its view in data-view), and tells assistive tech it is current.
  document.querySelectorAll('.sb-item').forEach(b => { b.classList.remove('active'); b.removeAttribute('aria-current'); });
  const item = document.querySelector(`.sb-item[data-view="${CSS.escape(name)}"]`);
  if (item) { item.classList.add('active'); item.setAttribute('aria-current', 'page'); }
  // The phone tab bar: the tab that lists this view, else "More" (the drawer).
  let tabbed = false;
  document.querySelectorAll('#mobile-tabbar .mt-item').forEach(t => {
    const on = (t.dataset.views || '').split(' ').includes(name);
    tabbed ||= on;
    t.classList.toggle('active', on);
    if (on) t.setAttribute('aria-current', 'page'); else t.removeAttribute('aria-current');
  });
  if (!tabbed) document.getElementById('mt-more')?.classList.add('active');
  // Clear all views — remove active class AND reset any inline display styles.
  // A real change of view crossfades (View Transitions where the browser has
  // them); re-showing the current one swaps in place.
  const el = document.getElementById(name + '-view');
  const swap = () => {
    document.querySelectorAll('.view').forEach(v => {
      v.classList.remove('active');
      v.style.display = '';  // clear inline style so CSS .view / .view.active takes over
    });
    if (el) el.classList.add('active');
  };
  const swapped = el && !el.classList.contains('active') ? withTransition(swap) : (swap(), Promise.resolve());
  if (btn) haptic('light');
  if (name==='history') loadHistoryStatus();
  if (name==='tasks') loadTaskHistory();
  if (name==='recs') { if (_skipNextRecsLoad) { _skipNextRecsLoad = false; } else { loadRecs(state.currentRecsCategory); } }
  if (name==='reclassify') loadReclassify();
  if (name==='report') loadReport();
  if (name==='swipe') loadSwipe();
  // Views with tabs (Settings, Deletions, Knowledge Base, the arr pages):
  // the tab the hash names, or the one already open. o.pane is the older
  // spelling showLibrariesForce uses.
  _openTab(name, o.tab ?? o.pane);
  // tasks view uses live SSE, no manual load needed
  // Keyboard and screen-reader users land on the new view's heading, not
  // back at the top of the sidebar. Only for navigation the owner asked for;
  // the first render after sign-in leaves focus where it is.
  // (after the swap: inside a view transition it lands a frame later)
  if (o.focus) swapped.then(() => el?.querySelector('h1')?.focus({preventScroll: true}));
}

// History's "Plex libraries" button: the mapping lives in Settings now.
export function showLibrariesForce() { showView('settings', null, {pane: 'plex-libraries'}); }

// Glance tiles in the chat switch the view (showView marks the sidebar entry).
export function goToView(view) {
  showView(view, null, {focus: true});
}
