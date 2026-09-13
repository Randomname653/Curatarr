// ── ADMIN ─────────────────────────────────────────────────────────────────────
import { _errHtml, _errMsg, btnBusy, btnDone, confirmDialog, esc, toast } from './ui.js';
import { _swrInvalidate, _swrRun, api } from './api.js';
import { refreshSpotifyPending, spotifyDropZone } from './spotify_import.js';
export function _renderUsers(users) {
  const targets = document.querySelectorAll('.user-list-target');
  const html = `<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Username</th><th>Role</th><th>Status</th><th class="t-right">Actions</th></tr></thead><tbody>
    ${users.map(u=>`<tr>
      <td>${esc(u.plex_username)}</td>
      <td><span class="badge ${u.is_admin?'amber':'muted'}">${u.is_admin?'admin':'user'}</span></td>
      <td><span class="badge ${u.is_active?'success':'danger'}">${u.is_active?'active':'disabled'}</span></td>
      <td class="t-right"><button type="button" class="btn btn-secondary btn-sm" onclick="toggleUser(${u.id},${!u.is_active},this)">${u.is_active?'Disable':'Enable'}</button></td>
    </tr>`).join('')}
  </tbody></table></div>`;
  targets.forEach(el => el.innerHTML = html);
}

export async function loadUsers(force = false) {
  // The same list is rendered into the standalone admin-view AND the
  // Settings → Users pane. Both targets carry .user-list-target.
  const targets = document.querySelectorAll('.user-list-target');
  if (!targets.length) return;
  if (force) _swrInvalidate('admin-users');
  try {
    const users = await _swrRun('admin-users', () => api('/api/users/'), _renderUsers);
    // Spotify-import dropdown/status: a side effect of visiting this view,
    // not of the user list itself changing -- runs every time regardless of
    // whether _renderUsers actually redrew the table above.
    const zone = document.getElementById('adm-sp-zone');
    if (zone && !zone.innerHTML) zone.innerHTML = spotifyDropZone('adm-sp');
    const sel = document.getElementById('adm-sp-user');
    if (sel) sel.innerHTML = users.map(u =>
      `<option value="${u.id}">${esc(u.plex_username)}</option>`).join('');
    refreshSpotifyPending('adm-sp');
  } catch (e) {
    targets.forEach(el => el.innerHTML = _errHtml(e, 'loadUsers()'));
  }
}
export async function toggleUser(id, active, btn) {
  if (!active) {
    const res = await confirmDialog({title: 'Disable this user', danger: true, confirmLabel: 'Disable',
      body: '<p>They are signed out on every device at once and cannot sign in again until re-enabled. Their watch history stays for re-attribution.</p>'});
    if (!res.ok) return;
  }
  btnBusy(btn);
  try {
    await api(`/api/users/${id}`, 'PATCH', {is_active: active});
    toast(active ? 'User enabled' : 'User disabled — their sessions are gone', 'success');
    loadUsers(true);
  } catch (e) { toast(_errMsg(e), 'danger'); btnDone(btn); }
}
