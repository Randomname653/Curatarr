// ── AUTH ──────────────────────────────────────────────────────────────────────
import { api } from './api.js';
import { state } from './state.js';
import { loadUnreadMessages } from './notifications.js';
import { showOnboarding } from './setup.js';
import { loadLibraryConfig } from './libraries.js';
import { loadHistoryStatus } from './history.js';
import { loadGlancePanel, loadLastPlayed, loadStarters } from './chat.js';
import { startTaskStream } from './activity.js';
import { startProcessMonitor } from './game.js';


export async function startPlexLogin() {
  const data = await api('/api/auth/plex/pin','POST');
  document.getElementById('pin-box').style.display='block';
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
  async function doPoll() {
    if (Date.now() > expiresAt) {
      // PIN expired client-side — stop polling and let the user re-trigger.
      clearInterval(state.pollInterval);
      state.pollInterval = null;
      const codeEl = document.getElementById('pin-code');
      if (codeEl) codeEl.textContent = 'PIN expired — click Sign in again';
      return;
    }
    try {
      const r = await api(`/api/auth/plex/poll/${data.pin_id}`);
      if (r.status==='ok') {
        clearInterval(state.pollInterval);
        state.pollInterval = null;
        state.token=r.token; localStorage.setItem('curatarr_token',state.token);
        // Restart stream with new state.token
        if (state.taskEventSource) { state.taskEventSource.close(); state.taskEventSource = null; state.taskStreamRetries = 0; }
        setUser(r.user); showApp();
      }
    } catch (e) { if (e.status === 429) schedulePoll(POLL_MS_BACKOFF); }
  }
  schedulePoll(POLL_MS);
}

export function setUser(u) {
  state.currentUser = u;
  document.getElementById('user-name').textContent = u.username || 'User';
  document.getElementById('user-avatar').textContent = (u.username||'?')[0].toUpperCase();
  if (u.is_admin) {
    // Show admin nav item in sidebar (needs flex), but NOT the admin view panel
    document.querySelectorAll('.sb-item.admin-only').forEach(el => el.style.display = 'flex');
    // Inline admin action rows (e.g. history maintenance) — let CSS decide layout
    document.querySelectorAll('.admin-action-row').forEach(el => el.style.display = '');
    // Admin view panel stays hidden until user navigates to it
  }
  document.getElementById('auth-overlay').classList.add('hidden');
  loadUnreadMessages();
  // Notifications arrive while the app is open (principles are captured in the
  // background AFTER a debate ends) — without a poll the bell stayed frozen at
  // its boot-time state and new shadow principles never surfaced for review.
  if (!window._notifPollTimer) {
    window._notifPollTimer = setInterval(loadUnreadMessages, 60_000);
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
  if (state.currentUser?.is_admin) {
    // Check if onboarding is needed
    const cfg = await api('/api/libraries/config').catch(()=>({configured:false, libraries:[]}));
    if (!cfg.configured || !cfg.libraries?.length) {
      showOnboarding('libraries');
      return;
    }

    // Check if initial sync has happened
    const hist = await api('/api/history/status').catch(()=>({watch_history_entries:0}));
    if (hist.watch_history_entries === 0) {
      showOnboarding('sync');
      return;
    }
  }

  loadLibraryConfig();
  loadHistoryStatus();
  loadGlancePanel();
  loadLastPlayed();
  loadStarters();
  startTaskStream();
  // Process monitor polls the admin-only /api/processes router — only start it
  // for admins so a normal user doesn't spam 403s in the background.
  if (state.currentUser?.is_admin) startProcessMonitor();
}
