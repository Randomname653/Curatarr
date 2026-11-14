// ── SPOTIFY HISTORY IMPORT (shared by the setup step and the admin card) ──
import { state } from './state.js';
import { EL, EVENT, _errMsg, act, actOn, esc, toast } from './ui.js';
import { api } from './api.js';
import { collectStep } from './setup.js';
import { t, th } from './i18n.js';
// compact: the setup wizard's card, one short line (the how-to is in
// docs/USAGE.md); Settings keeps the file names.
export function spotifyDropZone(p, compact = false) {
  return `
    <div class="dropzone" id="${p}-drop"
         ${actOn('dragover', 'dropzoneOver', EVENT, EL)}
         ${actOn('dragleave', 'dropzoneLeave', EL)}
         ${actOn('drop', 'handleSpotifyDrop', EVENT, p)}
         ${act('openSpotifyPicker', p)}>
      <div class="fs-13">${compact ? th('setup.v2.drop_here') : th('spotify.drop')}</div>
      <div class="mt-4 hint">${compact ? th('setup.v2.drop_spotify') : th('spotify.drop_hint')}</div>
    </div>
    <input hidden id="${p}-file" type="file" multiple accept=".json,.zip" ${actOn('change', 'onSpotifyFile', EL, p)}>
    <div class="mt-8" id="${p}-result"></div>
    <div id="${p}-pending" class="mt-4 hint"></div>`;
}

export function handleSpotifyDrop(ev, p) {
  ev.preventDefault();
  ev.currentTarget.style.borderColor = 'var(--border)';
  uploadSpotify(ev.dataTransfer.files, p);
}

// r.ok was never checked: a 403 or a 500 body has no `pending`, so it read
// as "no files waiting" (an empty line). A failed check now says so.
export function _spotifyPendingText(ok, d, status) {
  if (!ok) {
    const why = (d && typeof d.detail === 'string' && d.detail) || `HTTP ${status}`;
    return {text: t('spotify.check_failed', {error: why}), err: true};
  }
  return {text: d?.pending?.length ? t('spotify.waiting', {n: d.pending.length, list: d.pending.map(f => f.name).join(', ')}) : '', err: false};
}

export async function refreshSpotifyPending(p) {
  const el = document.getElementById(p + '-pending');
  const show = ({text, err}) => {
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('t-danger', err);
  };
  try {
    const headers = state.token ? {Authorization: 'Bearer ' + state.token} : {};
    const r = await fetch('/api/import/spotify/status', {headers});
    let d = null;
    try { d = await r.json(); } catch { /* a non-JSON error page */ }
    show(_spotifyPendingText(r.ok, d, r.status));
    return r.ok ? d : null;
  } catch (e) {
    show({text: t('spotify.check_failed', {error: _errMsg(e)}), err: true});
    return null;
  }
}

export async function uploadSpotify(files, p) {
  if (!files || !files.length) return;
  const el = document.getElementById(p + '-result');
  if (el) el.innerHTML = `<div class="test-status">${th('spotify.uploading')}</div>`;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const headers = state.token ? {Authorization: 'Bearer ' + state.token} : {};
  try {
    const r = await fetch('/api/import/spotify/files', {method: 'POST', body: fd, headers});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || t('spotify.upload_failed'));
    if (el) el.innerHTML = [
      d.saved.length ? `<div class="test-status ok">${th('spotify.accepted', {n: d.saved.length})}</div>` : '',
      ...d.rejected.map(x => `<div class="test-status err">${esc(x.name)}: ${esc(x.reason)}</div>`),
    ].join('');
  } catch (e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
  refreshSpotifyPending(p);
}

export async function runSpotifyImport(p) {
  const uid = parseInt(document.getElementById(p + '-user')?.value);
  const el = document.getElementById(p + '-result');
  if (!uid) { if (el) el.innerHTML = `<div class="test-status err">${th('spotify.pick_user')}</div>`; return; }
  try {
    await api('/api/import/spotify/run', 'POST', {user_id: uid});
    if (el) el.innerHTML = `<div class="test-status ok">${th('spotify.started')}</div>`;
  } catch (e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

// The wizard's finish: the server writes the settings and takes them over
// at once (no restart); a plex.tv sign-in in the first step also signs the
// owner in, so the page opens straight on the library mapping.
export async function finishSetup() {
  collectStep();
  const el = document.getElementById('setup-saving');
  const btn = document.getElementById('setup-finish');
  if (btn) btn.disabled = true;
  el.textContent = t('spotify.setup_saving');
  const body = {...state.setupData};
  for (const k of Object.keys(body)) if (k.startsWith('_')) delete body[k];
  try {
    const r = await api('/api/setup/complete', 'POST', body);
    if (r.login?.token) {
      state.token = r.login.token;
      try { localStorage.setItem('curatarr_token', r.login.token); } catch (_) { /* private mode: this tab only */ }
    } else if (r.login_error) {
      toast(r.login_error, 'amber', {ms: 10000});
    }
    el.textContent = t('spotify.setup_saved');
    setTimeout(() => location.reload(), 800);
  } catch(e) {
    if (btn) btn.disabled = false;
    el.innerHTML = `<span class="t-danger">${th('spotify.setup_error', {error: _errMsg(e)})}</span>`;
  }
}

// Drop zone of the Spotify import: highlight while a file is dragged over it,
// reset when it leaves; a click opens the hidden file input, whose change
// uploads. The inline handlers did all four in place.
export function dropzoneOver(event, el) { event.preventDefault(); el.style.borderColor = 'var(--amber)'; }
export function dropzoneLeave(el) { el.style.borderColor = 'var(--border)'; }
export function openSpotifyPicker(p) { document.getElementById(p + '-file').click(); }
export function onSpotifyFile(el, p) { uploadSpotify(el.files, p); }
