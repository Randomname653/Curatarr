// ── VERSION ──────────────────────────────────────────────────────────────────
// The build under the sidebar (GET /api/system/version): version plus the
// commit it was built from. For admins the same answer says whether upstream
// has published a newer build; then an amber "Update available" sits above
// the version and opens how to install it. The server asks GitHub at most
// every 6 h; this only re-reads the stored answer once an hour.
import { api } from './api.js';
import { _errMsg, act, esc, openModal, toast } from './ui.js';
import { state } from './state.js';
import { loadUnreadMessages } from './notifications.js';

const REFRESH_MS = 60 * 60_000;
let _last = null;

export async function loadVersion() {
  let v = null;
  try { v = await api('/api/system/version'); } catch { return; }
  _last = v;
  const el = document.getElementById('sb-version');
  if (el) {
    el.textContent = `v${v.version || '?'}${v.short ? ` · ${v.short}` : ''}`;
    el.title = v.sha ? `Build ${v.sha}${v.built ? `, built ${v.built}` : ''}` : 'Build unknown';
    el.hidden = false;
  }
  const btn = document.getElementById('sb-update');
  if (btn) btn.hidden = !(v.update && v.update.update_available);
  if (!state._versionTimer) state._versionTimer = setInterval(loadVersion, REFRESH_MS);
}

// The bell's "Update the PC" (services/notifications.py): the gaming PC's
// agent fast-forwards its checkout to main and restarts itself.
export async function updateGamingPc() {
  toast('Asking the gaming PC to update…', 'info');
  try {
    const r = await api('/api/hosts/pc/update', 'POST');
    toast(r.updated ? `The gaming PC updated to ${String(r.to || '').slice(0, 7)} and restarts its agent.`
                    : 'The gaming PC is already up to date.', 'success');
  } catch (e) {
    toast(`The gaming PC did not update — ${_errMsg(e)}`, 'danger', {ms: 9000});
  }
  loadUnreadMessages();
}

export function showUpdateInfo() {
  const v = _last || {};
  const u = v.update || {};
  const behind = u.behind_by ? `${u.behind_by} commit${u.behind_by === 1 ? '' : 's'} newer` : 'newer';
  openModal({
    title: 'Update available',
    size: 'narrow',
    body: `<p>A ${esc(behind)} Curatarr is published${u.latest_short ? ` (<code>${esc(u.latest_short)}</code>)` : ''}.
        This one runs <code>${esc(v.short || '?')}</code>.</p>
      <p class="t2 mt-8">${esc(u.how || '')}</p>
      <p class="t3 fs-12 mt-8"><a href="https://github.com/Randomname653/Curatarr/blob/main/CHANGELOG.md" target="_blank" rel="noopener">What changed</a></p>`,
    foot: `<button class="btn btn-primary w-full" ${act('closeModal')}>Got it</button>`,
  });
}
