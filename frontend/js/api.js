// ── API ───────────────────────────────────────────────────────────────────────
import { state } from './state.js';
import { API, confirmDialog } from './ui.js';
export async function api(path, method='GET', body=null, _retried=false) {
  const opts = {method, headers:{'Content-Type':'application/json'}};
  if (state.token) opts.headers['Authorization']='Bearer '+state.token;
  // First-run wizard from ANOTHER device: the server prints a one-time
  // setup code to its console and requires it on every setup call until
  // the first admin exists (a browser on the server itself is exempt).
  const setupCode = sessionStorage.getItem('curatarr_setup_code');
  if (setupCode && path.startsWith('/api/setup/')) opts.headers['X-Setup-Code'] = setupCode;
  if (body) opts.body = JSON.stringify(body);
  const r = await fetch(API+path, opts);
  if (r.status === 401 && path.startsWith('/api/setup/') && !_retried) {
    const msg = await r.clone().text();
    if (/setup code/i.test(msg)) {
      const ask = await confirmDialog({
        title: 'Setup code required', confirmLabel: 'Continue',
        body: '<p>This Curatarr is being set up from another device. Enter the one-time setup code printed in the Curatarr console window (start.bat) or log file.</p>',
        reason: {label: 'Setup code'},
      });
      const entered = ask.ok ? ask.reason : '';
      if (entered && entered.trim()) {
        sessionStorage.setItem('curatarr_setup_code', entered.trim().toUpperCase());
        return api(path, method, body, true);
      }
    }
  }
  // TokenRefreshMiddleware (src/middleware.py) silently reissues a JWT past
  // 1/7th of its 7-day life on ANY response for a still-valid state.token -- ok or
  // not, so this reads before the ok-check below, not after. A client that
  // ignores the header just keeps its fixed 7-day expiry; picking it up
  // makes a session under active use effectively immortal.
  const freshToken = r.headers.get('x-curatarr-refreshed-token');
  if (freshToken) { state.token = freshToken; localStorage.setItem('curatarr_token', freshToken); }
  if (!r.ok) { const err = new Error(await r.text()); err.status = r.status; throw err; }
  return r.json();
}

// ── SWR VIEW CACHE ───────────────────────────────────────────────────────────
// Stale-while-revalidate for view loads: render the last-known payload for a
// key INSTANTLY from this in-memory Map (module-level only, deliberately
// never localStorage -- this is a hot cache, not persisted state), then
// refetch in the background and only re-render if the payload actually
// changed (a JSON-string compare is enough, nothing here needs deep-equal).
//
// Deliberately NOT wired into every view -- only report/tasks/admin-users
// and the history-stats tile, plus libraries/reclassify in a restricted
// mode (see noAutoReplace below). recs and deletions already have their own
// purpose-built caching (loadRecs' stale-category guard + self-poll,
// deletions' state._delProposalsAll tab cache) that a second generic layer here
// would fight rather than help; enrich mixes two self-perpetuating poll
// loops across 5 endpoints where the backend's own new 10-30s TTL memo
// already solves the expensive-recompute problem this exists for; arr-*
// pages resolve their own promise before their real tab content has even
// loaded (renderArrTab/renderSynopsisBrowser run un-awaited) so there's
// nothing stable to snapshot at the loadArrPage() level, and the endpoint
// already carries a server-side 15-minute cache of its own.
export const _swrCache = new Map();

// key: cache key. fetchFn: () => Promise<data>. renderFn: (data) => void
// (writes DOM, looks up its own target element(s) -- same convention as
// every other _render*() helper in this file). Returns the data that ended
// up cached (fresh on success, the stale value on a swallowed background
// failure) so callers that need it for follow-on logic don't have to
// re-read the cache themselves.
//
// opts.noAutoReplace: for views whose DOM carries user-editable state a
// JSON diff can't see (an unsaved <select> in libraries, unsaved
// checkboxes in reclassify) -- once there's anything cached for this key,
// leave the DOM alone FOR GOOD, not just after this particular call. The
// naive version of this (skip only the post-fetch re-render) still let a
// plain re-visit -- navigate away, come back, nothing saved -- blow away
// an in-progress edit, because the cache-hit render at the top of this
// function ran unconditionally regardless of the flag. Only an explicit
// _swrInvalidate() (called by the view's own mutating action right before
// its own reload) clears the cache and lets a fresh render through again.
export async function _swrRun(key, fetchFn, renderFn, opts = {}) {
  const cached = _swrCache.get(key);
  const hadCache = cached !== undefined;
  if (hadCache && opts.noAutoReplace) return cached;

  if (hadCache) renderFn(cached);
  let fresh;
  try { fresh = await fetchFn(); }
  catch (e) { if (!hadCache) throw e; return cached; } // stale-but-shown beats an error screen
  const changed = !hadCache || JSON.stringify(fresh) !== JSON.stringify(cached);
  _swrCache.set(key, fresh);
  if (!hadCache || changed) renderFn(fresh);
  return fresh;
}
export function _swrInvalidate(key) { _swrCache.delete(key); }
