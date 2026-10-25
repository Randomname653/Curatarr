// ── AUTH ──────────────────────────────────────────────────────────────────────
import { api } from './api.js';
import { _errMsg, btnDone, setLocale, toast } from './ui.js';
import { state } from './state.js';
import { loadUnreadMessages } from './notifications.js';
import { showOnboarding } from './setup.js';
import { loadLibraryConfig } from './libraries.js';
import { loadHistoryStatus } from './history.js';
import { loadGlancePanel, loadLastPlayed, loadStarters, restoreChatHistory } from './chat.js';
import { startTaskStream, stopTaskStream } from './activity.js';
import { startProcessMonitor } from './game.js';
import { routeFromHash } from './nav.js';
import { loadVersion } from './version.js';


// The poll used to ignore every answer but ok / 429 / 403: a plex.tv outage
// or a server mid-restart left "Waiting for authentication…" up for the
// PIN's whole 15 minutes. Three misses in a row say so (and keep retrying).
const PLEX_POLL_MISSES = 3;
export function _plexPollNote(misses) {
  return misses >= PLEX_POLL_MISSES ? 'Plex sign-in is not answering — retrying' : 'Waiting for authentication…';
}

export async function startPlexLogin() {
  let data;
  try {
    data = await api('/api/auth/plex/pin','POST');
  } catch (e) {
    // The button used to do nothing at all on a failure here.
    toast(`Couldn't start the Plex sign-in — ${_errMsg(e)}`, 'danger');
    return;
  }
  document.getElementById('pin-box').hidden = false;
  document.getElementById('pin-code').textContent = data.code.slice(0,4)+' '+data.code.slice(4);
  window.open(data.auth_url,'_blank');
  if (state.pollInterval) clearInterval(state.pollInterval);
  // Plex PINs expire after 15 minutes — there's no point polling forever.
  const expiresAt = Date.now() + 15 * 60 * 1000;
  // Backend caps polling at 5/pin/60s, sliding window (auth.py's
  // MAX_POLLS_PER_MINUTE) -- 13s keeps every poll comfortably under that
  // on its own (12s is the exact breakeven). A 429 still widens the gap
  // further rather than hammering the same still-closed window again next
  // tick -- plex.tv itself can be slow enough that even a careful client
  // ends up bursty around a page reload or a second tab polling the same
  // pin_id.
  const POLL_MS = 13000;
  const POLL_MS_BACKOFF = 30000;
  const schedulePoll = (ms) => { clearInterval(state.pollInterval); state.pollInterval = setInterval(doPoll, ms); };
  let misses = 0;
  const codeText = data.code.slice(0,4)+' '+data.code.slice(4);
  const showMisses = () => {
    const status = document.getElementById('poll-status');
    if (status) { status.textContent = _plexPollNote(misses); return; }
    // No status line on this screen: the code's own slot carries the note,
    // and gets the code back once Plex answers again.
    const codeEl = document.getElementById('pin-code');
    if (codeEl) codeEl.textContent = misses >= PLEX_POLL_MISSES ? _plexPollNote(misses) : codeText;
  };
  showMisses();   // a fresh PIN starts from "Waiting…", not a previous attempt's note
  async function doPoll() {
    if (Date.now() > expiresAt) {
      // PIN expired client-side — stop polling and let the user re-trigger.
      clearInterval(state.pollInterval);
      state.pollInterval = null;
      const codeEl = document.getElementById('pin-code');
      if (codeEl) codeEl.textContent = 'Code expired — select Sign in with Plex again';
      return;
    }
    try {
      // The nonce binds the PIN to this browser: the server refuses a poll
      // for it from anyone else (auth.py _check_pin_binding). Sent as a
      // header so it never lands in an access log.
      const r = await api(`/api/auth/plex/poll/${data.pin_id}`, 'GET', null, false, {'X-Plex-Pin-Nonce': data.nonce});
      if (misses) { misses = 0; showMisses(); }
      if (r.status==='ok') {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
        state.token=r.token; localStorage.setItem('curatarr_token',state.token);
        // Restart stream with new state.token
        stopTaskStream();
        state.taskStreamRetries = 0;
        setUser(r.user); showApp();
      }
    } catch (e) {
      if (e.status === 429) schedulePoll(POLL_MS_BACKOFF);
      // 403 is final - the server no longer knows this PIN (restarted
      // mid-login, binding expired) or refused the account: polling on can
      // never succeed, so stop and say why.
      else if (e.status === 403) {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
        let why = 'Sign-in expired — select Sign in with Plex again';
        try { why = JSON.parse(e.message).detail || why; } catch { /* plain-text body */ }
        const codeEl = document.getElementById('pin-code');
        if (codeEl) codeEl.textContent = why;
      }
      // 5xx, a network error, anything else: keep polling, but say so.
      else if (++misses >= PLEX_POLL_MISSES) showMisses();
    }
  }
  schedulePoll(POLL_MS);
}

// The code is shown split for reading ("ABCD EFGH"); plex.tv/link wants it whole.
export async function copyPlexCode(btn) {
  const code = (document.getElementById('pin-code')?.textContent || '').replace(/\s+/g, '');
  try {
    await navigator.clipboard.writeText(code);
    btnDone(btn, 'Copied', {revertMs: 2000});
  } catch {
    toast("Couldn't copy — select the code and copy it by hand.", 'amber');
  }
}

// The user's own clock: the Wrap-Up's night-owl hours and calendars run on
// the zone this browser reports (services/user_time). Sent when it differs
// from the stored one; a failure only means the server's clock for now.
function syncTimezone(u) {
  let tz = '';
  try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch { return; }
  if (!tz || tz === u.timezone) return;
  api('/api/users/me/timezone', 'POST', {timezone: tz})
    .catch(e => console.warn('[tz] time zone not stored - the server clock stands in', tz, e));
}

export function setUser(u) {
  state.currentUser = u;
  setLocale(u.locale);
  syncTimezone(u);
  document.getElementById('user-name').textContent = u.username || 'User';
  document.getElementById('user-avatar').textContent = (u.username||'?')[0].toUpperCase();
  if (u.is_admin) {
    // Admin-only chrome: the sidebar's Curate section, the admin Settings
    // tabs, inline admin actions (e.g. Force sync) — let CSS decide layout
    document.querySelectorAll('.admin-action-row').forEach(el => el.hidden = false);
    // ...and what a member gets in their place (the phone tab bar's History)
    document.querySelectorAll('.member-only-row').forEach(el => el.hidden = true);
  }
  document.getElementById('auth-overlay').classList.add('hidden');
  loadUnreadMessages();
  loadVersion();
  // Notifications arrive while the app is open (principles are captured in the
  // background AFTER a debate ends) — without a poll the bell stayed frozen at
  // its boot-time state and new shadow principles never surfaced for review.
  if (!state._notifPollTimer) {
    state._notifPollTimer = setInterval(loadUnreadMessages, 60_000);
  }
}

export async function showApp() {
  document.getElementById('auth-overlay').classList.add('hidden');

  // Library + sync onboarding is the ADMIN's first-run setup. A secondary
  // (non-admin) user must never get trapped there: their data is set up
  // automatically on login (re-attribution + taste vector in the background),
  // and a brand-new user with no watch history yet — e.g. nothing of theirs
  // survives in Plex's retention window — should still land straight in the
  // app. Their taste profile builds as they watch and chat.
  // A failed check is NOT a first run: one transient error here used to drop
  // a configured admin into onboarding (whose Save rewrites the library
  // mapping). Unknown means "say so and open the app".
  if (state.currentUser?.is_admin) {
    // Check if onboarding is needed
    let cfg = null;
    try { cfg = await api('/api/libraries/config'); }
    catch (e) { toast(`Couldn't check the library setup — ${_errMsg(e)}. Opening the app anyway.`, 'amber', {ms: 8000}); }
    if (cfg && (!cfg.configured || !cfg.libraries?.length)) {
      showOnboarding('libraries');
      return;
    }

    // Check if initial sync has happened (only meaningful once the libraries are known)
    let hist = null;
    if (cfg) {
      try { hist = await api('/api/history/status'); }
      catch (e) { toast(`Couldn't check the watch-history sync — ${_errMsg(e)}. Opening the app anyway.`, 'amber', {ms: 8000}); }
    }
    if (hist && hist.watch_history_entries === 0) {
      showOnboarding('sync');
      return;
    }
  }

  // A bookmarked or reloaded #view opens where the owner left off.
  routeFromHash({focus: false});
  // The Plex-libraries pane and its /discover call are admin-only.
  if (state.currentUser?.is_admin) loadLibraryConfig();
  loadHistoryStatus();
  restoreChatHistory();
  loadGlancePanel();
  loadLastPlayed();
  loadStarters();
  startTaskStream();
  // Process monitor polls the admin-only /api/processes router — only start it
  // for admins so a normal user doesn't spam 403s in the background.
  if (state.currentUser?.is_admin) startProcessMonitor();
}
