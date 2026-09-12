// ── SIDEBAR COLLAPSE ─────────────────────────────────────────────────────────
import { loadRecs, searchLibrary } from './recs.js';
import { state } from './state.js';
import { loadHistoryStatus } from './history.js';
import { loadTaskHistory } from './activity.js';
import { loadLibraryConfig } from './libraries.js';
import { loadUsers } from './admin.js';
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
  showView('recs', document.querySelector(".sb-item[onclick*=\"'recs'\"]"));
  const libInput = document.getElementById('lib-search');
  if (libInput) libInput.value = q;
  box.value = '';
  box.blur();
  searchLibrary();
}

// ── NAV ───────────────────────────────────────────────────────────────────────
export function showView(name, btn) {
  // Admin-only views — defense-in-depth on top of the hidden nav items + the
  // require_admin endpoints: never render the shell for a non-admin, even via a
  // stray programmatic call. (Library config + orphaned + deletions = curation.)
  if (!state.currentUser?.is_admin && ['deletions','curation','libraries','admin','reclassify','report'].includes(name)) return;
  toggleMobileSidebar(false); // picking a view closes the mobile drawer
  document.querySelectorAll('.sb-item').forEach(b=>b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  // Clear all views — remove active class AND reset any inline display styles
  document.querySelectorAll('.view').forEach(v => {
    v.classList.remove('active');
    v.style.display = '';  // clear inline style so CSS .view / .view.active takes over
  });
  const el = document.getElementById(name + '-view');
  if (el) el.classList.add('active');
  if (name==='history') loadHistoryStatus();
  if (name==='tasks') loadTaskHistory();
  if (name==='libraries') loadLibraryConfig();
  if (name==='admin') loadUsers();
  if (name==='enrich') showKbTab(state._kbTab);
  if (name==='recs') { if (_skipNextRecsLoad) { _skipNextRecsLoad = false; } else { loadRecs(state.currentRecsCategory); } }
  if (name==='deletions') loadDeletions(state.currentDelCategory);
  // Pass 16c: Library Manager pages
  if (name==='arr-sonarr') loadArrPage('sonarr');
  if (name==='arr-radarr') loadArrPage('radarr');
  if (name==='arr-lidarr') loadArrPage('lidarr');
  if (name==='reclassify') loadReclassify();
  if (name==='report') loadReport();
  // tasks view uses live SSE, no manual load needed
}
