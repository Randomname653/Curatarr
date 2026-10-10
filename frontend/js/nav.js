// ── SIDEBAR COLLAPSE ─────────────────────────────────────────────────────────
import { loadRecs, searchLibrary } from './recs.js';
import { state } from './state.js';
import { loadHistoryStatus } from './history.js';
import { loadTaskHistory } from './activity.js';
import { openSettingsPane } from './settings.js';
import { showKbTab } from './kb.js';
import { loadDeletions } from './deletions.js';
import { loadArrPage } from './arr.js';
import { loadReclassify } from './reclassify.js';
import { loadReport } from './report.js';
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

export function routeFromHash(o = {}) {
  if (!state.currentUser) return;            // the login screen owns the page
  const [view, pane] = decodeURIComponent(location.hash.replace(/^#\/?/, '')).split('/');
  const name = view && document.getElementById(view + '-view') ? view : 'chat';
  const current = document.querySelector('.view.active')?.id;
  // Same view, same pane: nothing to do (a pushState echo, or a re-click).
  if (current === name + '-view' && (name !== 'settings' || !pane || _activePane() === pane)) return;
  showView(name, null, {push: false, focus: o.focus !== false, pane});
}

function _activePane() {
  return document.querySelector('.settings-pane:not([hidden])')?.dataset.pane;
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
  // Clear all views — remove active class AND reset any inline display styles
  document.querySelectorAll('.view').forEach(v => {
    v.classList.remove('active');
    v.style.display = '';  // clear inline style so CSS .view / .view.active takes over
  });
  const el = document.getElementById(name + '-view');
  if (el) el.classList.add('active');
  if (name==='history') loadHistoryStatus();
  if (name==='tasks') loadTaskHistory();
  if (name==='enrich') showKbTab(state._kbTab);
  if (name==='recs') { if (_skipNextRecsLoad) { _skipNextRecsLoad = false; } else { loadRecs(state.currentRecsCategory); } }
  if (name==='deletions') loadDeletions(state.currentDelCategory);
  // Pass 16c: Library Manager pages
  if (name==='arr-sonarr') loadArrPage('sonarr');
  if (name==='arr-radarr') loadArrPage('radarr');
  if (name==='arr-lidarr') loadArrPage('lidarr');
  if (name==='reclassify') loadReclassify();
  if (name==='report') loadReport();
  if (name==='settings') openSettingsPane(o.pane || _activePane() || 'account');
  // tasks view uses live SSE, no manual load needed
  // Keyboard and screen-reader users land on the new view's heading, not
  // back at the top of the sidebar. Only for navigation the owner asked for;
  // the first render after sign-in leaves focus where it is.
  if (o.focus) el?.querySelector('h1')?.focus({preventScroll: true});
}

// History's "Plex libraries" button: the mapping lives in Settings now.
export function showLibrariesForce() { showView('settings', null, {pane: 'plex-libraries'}); }

// Glance tiles in the chat switch the view (showView marks the sidebar entry).
export function goToView(view) {
  showView(view, null, {focus: true});
}
