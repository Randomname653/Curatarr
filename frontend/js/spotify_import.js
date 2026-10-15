// ── SPOTIFY HISTORY IMPORT (shared by the setup step and the admin card) ──
import { state } from './state.js';
import { EL, EVENT, _errMsg, act, actOn, esc } from './ui.js';
import { api } from './api.js';
import { collectStep } from './setup.js';
export function spotifyDropZone(p) {
  return `
    <div class="dropzone" id="${p}-drop"
         ${actOn('dragover', 'dropzoneOver', EVENT, EL)}
         ${actOn('dragleave', 'dropzoneLeave', EL)}
         ${actOn('drop', 'handleSpotifyDrop', EVENT, p)}
         ${act('openSpotifyPicker', p)}>
      <div class="fs-13">Drop your Spotify extended history here</div>
      <div class="mt-4 hint">Streaming_History_Audio_*.json, endsong_*.json or the whole my_spotify_data.zip — or click to browse</div>
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
    return {text: `Couldn't check for uploaded files — ${why}`, err: true};
  }
  return {text: d?.pending?.length ? `${d.pending.length} file(s) waiting: ${d.pending.map(f => f.name).join(', ')}` : '', err: false};
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
    show({text: `Couldn't check for uploaded files — ${_errMsg(e)}`, err: true});
    return null;
  }
}

export async function uploadSpotify(files, p) {
  if (!files || !files.length) return;
  const el = document.getElementById(p + '-result');
  if (el) el.innerHTML = '<div class="test-status">Uploading…</div>';
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  const headers = state.token ? {Authorization: 'Bearer ' + state.token} : {};
  try {
    const r = await fetch('/api/import/spotify/files', {method: 'POST', body: fd, headers});
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'upload failed');
    if (el) el.innerHTML = [
      d.saved.length ? `<div class="test-status ok">${d.saved.length} file(s) accepted</div>` : '',
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
  if (!uid) { if (el) el.innerHTML = '<div class="test-status err">pick a user first</div>'; return; }
  try {
    await api('/api/import/spotify/run', 'POST', {user_id: uid});
    if (el) el.innerHTML = '<div class="test-status ok">Import started — watch the Activity view. The music matcher resolves the tracks afterwards.</div>';
  } catch (e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

export async function finishSetup() {
  collectStep();
  const el = document.getElementById('setup-saving');
  el.textContent = 'Saving configuration…';
  try {
    const r = await api('/api/setup/complete', 'POST', state.setupData);
    el.innerHTML = 'Saved! Building Ollama models in background… <strong>Please close and reopen Curatarr.</strong>';
    setTimeout(()=>location.reload(), 5000);
  } catch(e) {
    el.innerHTML = `<span class="t-danger">Error: ${esc(_errMsg(e))}</span>`;
  }
}

// Drop zone of the Spotify import: highlight while a file is dragged over it,
// reset when it leaves; a click opens the hidden file input, whose change
// uploads. The inline handlers did all four in place.
export function dropzoneOver(event, el) { event.preventDefault(); el.style.borderColor = 'var(--amber)'; }
export function dropzoneLeave(el) { el.style.borderColor = 'var(--border)'; }
export function openSpotifyPicker(p) { document.getElementById(p + '-file').click(); }
export function onSpotifyFile(el, p) { uploadSpotify(el.files, p); }
