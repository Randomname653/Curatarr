// ── VERSION ──────────────────────────────────────────────────────────────────
// The build under the sidebar (GET /api/system/version): version plus the
// commit it was built from. For admins the same answer says whether upstream
// has published a newer build; then an amber "Update available" sits above
// the version and opens how to install it. The server asks GitHub at most
// once an hour; this re-reads the stored answer every 15 minutes.
import { api } from './api.js';
import { act, esc, openModal } from './ui.js';
import { state } from './state.js';

const REFRESH_MS = 15 * 60_000;
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
