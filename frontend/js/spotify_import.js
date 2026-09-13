// ── SPOTIFY HISTORY IMPORT (shared by the setup step and the admin card) ──
import { state } from './state.js';
import { _errMsg, esc } from './ui.js';
import { api } from './api.js';
import { collectStep } from './setup.js';
export function spotifyDropZone(p) {
  return `
    <div id="${p}-drop" style="border:2px dashed var(--border);border-radius:var(--radius);padding:26px;text-align:center;cursor:pointer"
         ondragover="event.preventDefault();this.style.borderColor='var(--amber)'"
         ondragleave="this.style.borderColor='var(--border)'"
         ondrop="handleSpotifyDrop(event,'${p}')"
         onclick="document.getElementById('${p}-file').click()">
      <div style="font-size:13px">Drop your Spotify extended history here</div>
      <div class="hint" style="margin-top:4px">Streaming_History_Audio_*.json, endsong_*.json or the whole my_spotify_data.zip — or click to browse</div>
    </div>
    <input id="${p}-file" type="file" multiple accept=".json,.zip" style="display:none" onchange="uploadSpotify(this.files,'${p}')">
    <div id="${p}-result" style="margin-top:8px"></div>
    <div id="${p}-pending" class="hint" style="margin-top:4px"></div>`;
}

export function handleSpotifyDrop(ev, p) {
  ev.preventDefault();
  ev.currentTarget.style.borderColor = 'var(--border)';
  uploadSpotify(ev.dataTransfer.files, p);
}

export async function refreshSpotifyPending(p) {
  try {
    const headers = state.token ? {Authorization: 'Bearer ' + state.token} : {};
    const r = await fetch('/api/import/spotify/status', {headers});
    const d = await r.json();
    const el = document.getElementById(p + '-pending');
    if (el) el.textContent = d.pending?.length
      ? `${d.pending.length} file(s) waiting: ${d.pending.map(f => f.name).join(', ')}`
      : '';
    return d;
  } catch { return null; }
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
    el.innerHTML = `<span style="color:var(--danger)">Error: ${esc(_errMsg(e))}</span>`;
  }
}
