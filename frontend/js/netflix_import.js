// ── NETFLIX HISTORY IMPORT (Settings → Users, next to Spotify) ───────────────
// Drop the full data export (the zip, or its ViewingActivity.csv) or a
// profile's NetflixViewingHistory.csv; every profile found is listed with a
// household member to attach it to; Import writes the plays as watch history
// (services/netflix_import). Profiles left on "skip" are not imported.
import { EL, EVENT, _errMsg, act, actOn, esc } from './ui.js';
import { api } from './api.js';
import { state } from './state.js';
import { dropzoneLeave } from './spotify_import.js';

let _users = [];

export function netflixDropZone() {
  return `
    <div class="dropzone" id="adm-nf-drop"
         ${actOn('dragover', 'dropzoneOver', EVENT, EL)}
         ${actOn('dragleave', 'dropzoneLeave', EL)}
         ${actOn('drop', 'handleNetflixDrop', EVENT)}
         ${act('openNetflixPicker')}>
      <div class="fs-13">Drop your Netflix viewing history here</div>
      <div class="mt-4 hint">The data export's zip (or its ViewingActivity.csv), or a profile's NetflixViewingHistory.csv — or click to browse</div>
    </div>
    <input hidden id="adm-nf-file" type="file" multiple accept=".csv,.zip" ${actOn('change', 'onNetflixFile', EL)}>
    <div class="mt-8" id="adm-nf-result"></div>
    <div id="adm-nf-pending" class="mt-8"></div>`;
}

export function handleNetflixDrop(ev) {
  ev.preventDefault();
  dropzoneLeave(ev.currentTarget);
  uploadNetflix(ev.dataTransfer.files);
}
export function openNetflixPicker() { document.getElementById('adm-nf-file').click(); }
export function onNetflixFile(el) { uploadNetflix(el.files); }

function _profileRow(name, plays, kind) {
  const label = kind === 'simple' ? `${name} (one profile, dates only)` : name;
  const opts = ['<option value="">skip</option>',
    ..._users.map(u => `<option value="${u.id}">${esc(u.plex_username)}</option>`)].join('');
  return `<div class="row mt-4">
      <span class="fs-12 grow">${esc(label)} <span class="t3">· ${plays} plays</span></span>
      <select class="input minw-160" data-nf-profile="${esc(name)}" aria-label="Household member for ${esc(name)}">${opts}</select>
    </div>`;
}

export function _netflixPendingHtml(d) {
  const files = d?.pending || [];
  if (!files.length) return '';
  const rows = [];
  for (const f of files) {
    for (const [name, plays] of Object.entries(f.profiles || {})) rows.push(_profileRow(name, plays, f.kind));
  }
  const dropped = files.reduce((n, f) => n + Object.values(f.dropped || {}).reduce((a, b) => a + b, 0), 0);
  return `<div class="hint">${files.length} file(s) waiting${dropped ? ` · ${dropped} trailers, previews and short starts left out` : ''}. Attach each profile to a household member:</div>
    ${rows.join('')}
    <div class="row mt-8">
      <button type="button" class="btn btn-primary btn-sm" ${act('runNetflixImport')}>Import</button>
      <button type="button" class="btn btn-secondary btn-sm" ${act('clearNetflixFiles')}>Remove the files</button>
    </div>`;
}

export async function refreshNetflixPending(users) {
  if (users) _users = users;
  const el = document.getElementById('adm-nf-pending');
  if (!el) return;
  try {
    el.innerHTML = _netflixPendingHtml(await api('/api/import/netflix/status'));
  } catch (e) {
    el.innerHTML = `<div class="test-status err">Couldn't check for uploaded files — ${esc(_errMsg(e))}</div>`;
  }
}

export async function uploadNetflix(files) {
  if (!files || !files.length) return;
  const el = document.getElementById('adm-nf-result');
  if (el) el.innerHTML = '<div class="test-status">Uploading…</div>';
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  // a multipart upload: api() sends JSON
  const headers = state.token ? {Authorization: 'Bearer ' + state.token} : {};
  try {
    const r = await fetch('/api/import/netflix/files', {method: 'POST', body: fd, headers});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'upload failed');
    if (el) el.innerHTML = [
      d.saved.length ? `<div class="test-status ok">${d.saved.length} file(s) accepted</div>` : '',
      ...d.rejected.map(x => `<div class="test-status err">${esc(x.name)}: ${esc(x.reason)}</div>`),
    ].join('');
  } catch (e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
  refreshNetflixPending();
}

export function _netflixAssign(selects) {
  const out = {};
  for (const s of selects) {
    const uid = parseInt(s.value);
    if (uid) out[s.dataset.nfProfile] = uid;
  }
  return out;
}

export async function runNetflixImport() {
  const el = document.getElementById('adm-nf-result');
  const assign = _netflixAssign(document.querySelectorAll('[data-nf-profile]'));
  if (!Object.keys(assign).length) {
    if (el) el.innerHTML = '<div class="test-status err">Attach at least one profile to a household member first.</div>';
    return;
  }
  try {
    await api('/api/import/netflix/run', 'POST', {assign});
    if (el) el.innerHTML = '<div class="test-status ok">Import started — watch the Activity view. Titles in the library are matched as the plays go in.</div>';
  } catch (e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
  refreshNetflixPending();
}

export async function clearNetflixFiles() {
  try { await api('/api/import/netflix/files', 'DELETE'); } catch (e) { /* the list below says what is left */ }
  refreshNetflixPending();
}
