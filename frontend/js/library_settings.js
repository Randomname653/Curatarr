// ── Pass 16b: Library Settings (admin only) ───────────────────────────────
import { api } from './api.js';
import { _errHtml, _errMsg, _showTestResult, btnBusy, btnDone, esc, escAttr, setStatus, toast, trackDirty } from './ui.js';
export async function loadLibrarySettings() {
  const shell = document.getElementById('library-settings-shell');
  if (!shell) return;
  shell.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const status = await api('/api/library/status');
    shell.innerHTML = '';
    for (const svc of ['sonarr', 'radarr', 'lidarr']) {
      shell.appendChild(renderArrCard(svc, status[svc] || {}));
    }
  } catch (e) {
    shell.innerHTML = _errHtml(e, 'loadLibrarySettings()');
  }
}

const ARR_LABELS = { sonarr: 'Sonarr (TV)', radarr: 'Radarr (Movies)', lidarr: 'Lidarr (Music)' };

export function renderArrCard(svc, info) {
  const card = document.createElement('section');
  card.className = 'section';
  const last = info.last_test || '';
  const state = !info.configured ? '<span class="badge amber badge-sm">not configured</span>'
    : last.startsWith('ok|') ? `<span class="badge success badge-sm">connected${last.split('|')[1] ? ' · ' + esc(last.split('|')[1]) : ''}</span>`
    : last.startsWith('fail|') ? `<span class="badge danger badge-sm" title="${escAttr(last.split('|')[1] || 'failed')}">connection failed</span>`
    : '<span class="badge muted badge-sm">not tested</span>';
  const port = svc === 'sonarr' ? '8989' : svc === 'radarr' ? '7878' : '8686';
  card.innerHTML = `
    <div class="section-head"><h3>${esc(ARR_LABELS[svc])} ${state}</h3></div>
    <div class="section-body">
      <div id="arr-form-${svc}" class="fs-12 mb-12" style="display:grid;grid-template-columns:120px 1fr;gap:6px 10px;align-items:center">
        <label for="arr-url-${svc}" class="t3">URL</label>
        <input type="text" id="arr-url-${svc}" class="input" aria-label="${esc(ARR_LABELS[svc])} URL" value="${esc(info.url || '')}" placeholder="http://localhost:${port}">
        <label for="arr-key-${svc}" class="t3">API key</label>
        <input type="password" id="arr-key-${svc}" class="input" aria-label="${esc(ARR_LABELS[svc])} API Key" placeholder="${info.has_key ? '(saved — leave blank to keep)' : 'paste API key'}">
      </div>
      <div class="row mb-12">
        <button type="button" class="btn btn-primary btn-sm" id="arr-save-${svc}" onclick="saveArrConfig('${svc}',this)">Save URL + key</button>
        <button type="button" class="btn btn-secondary btn-sm" onclick="testArr('${svc}',this)">Test connection</button>
        <span id="arr-msg-${svc}" class="status"></span>
      </div>
      <div id="arr-defaults-${svc}"${info.configured ? '' : ' hidden'} style="border-top:1px solid var(--border);padding-top:10px">
        ${info.configured ? `<div class="row"><span class="fs-11 t3">Root folder, quality profile and the other defaults come from the arr itself.</span><button type="button" class="btn btn-secondary btn-sm row-end" onclick="loadArrProfiles('${svc}',this)">Load profiles</button></div>` : ''}
      </div>
    </div>`;
  trackDirty(card.querySelector(`#arr-form-${svc}`), card.querySelector(`#arr-save-${svc}`));
  return card;
}

export async function testArr(svc, btn) {
  // Pass 16b.1: empty fields fall back to saved values. So "Test
  // connection" with blank inputs re-tests the existing config without
  // forcing the admin to re-paste the API key.
  const url = document.getElementById(`arr-url-${svc}`).value.trim();
  const key = document.getElementById(`arr-key-${svc}`).value.trim();
  const msg = document.getElementById(`arr-msg-${svc}`);
  btnBusy(btn, 'Testing…');
  setStatus(msg, 'testing…', 'busy');
  try {
    const body = { service: svc };
    if (url) body.url = url;
    if (key) body.api_key = key;
    // Pass 97: _showTestResult renders the privacy warning under the
    // result line so the user sees it even when the connection is fine.
    _showTestResult(msg, await api('/api/library/test', 'POST', body));
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); }
  btnDone(btn);
}

export async function saveArrConfig(svc, btn) {
  const url = document.getElementById(`arr-url-${svc}`).value.trim();
  const key = document.getElementById(`arr-key-${svc}`).value.trim();
  const msg = document.getElementById(`arr-msg-${svc}`);
  if (!url || !key) { setStatus(msg, 'URL + key required', 'err'); return; }
  btnBusy(btn, 'Saving…');
  setStatus(msg, 'saving…', 'busy');
  try {
    await api('/api/library/configure', 'POST', { service: svc, url, api_key: key });
    setStatus(msg, 'saved + connected', 'ok');
    toast(`${ARR_LABELS[svc]} saved and connected`, 'success');
    // Reload the whole pane so the defaults section becomes visible
    setTimeout(loadLibrarySettings, 600);
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}

export async function loadArrProfiles(svc, btn) {
  const target = document.getElementById(`arr-defaults-${svc}`);
  btnBusy(btn, 'Loading…');
  target.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading profiles…</p>';
  try {
    const [profiles, defaults] = await Promise.all([
      api(`/api/library/profiles/${svc}`),
      api('/api/library/defaults'),
    ]);
    const cur = (defaults || {})[svc] || {};
    const rootFolderOptions = (profiles.root_folders || []).map(rf =>
      `<option value="${esc(rf.path)}" ${cur.root_folder_path === rf.path ? 'selected' : ''}>${esc(rf.path)}</option>`
    ).join('');
    const qualityOptions = (profiles.quality_profiles || []).map(q =>
      `<option value="${q.id}" ${cur.quality_profile_id === q.id ? 'selected' : ''}>${esc(q.name)}</option>`
    ).join('');
    let extra = '';
    if (svc === 'sonarr') {
      // Pass 16b.1: language profiles dropped — Sonarr v4 doesn't have them.
      extra += `
        <label for="arr-series-type-${svc}" class="t3">Series type</label>
        <select id="arr-series-type-${svc}" class="input">
          <option value="standard" ${cur.series_type === 'standard' ? 'selected' : ''}>Standard</option>
          <option value="anime" ${cur.series_type === 'anime' ? 'selected' : ''}>Anime</option>
          <option value="daily" ${cur.series_type === 'daily' ? 'selected' : ''}>Daily</option>
        </select>
      `;
    }
    if (svc === 'lidarr' && profiles.metadata_profiles) {
      extra += `
        <label for="arr-meta-${svc}" class="t3">Metadata profile</label>
        <select id="arr-meta-${svc}" class="input">
          ${(profiles.metadata_profiles || []).map(m => `<option value="${m.id}" ${cur.metadata_profile_id === m.id ? 'selected' : ''}>${esc(m.name)}</option>`).join('')}
        </select>
        <label for="arr-monitor-${svc}" class="t3">Monitor option</label>
        <select id="arr-monitor-${svc}" class="input">
          ${['all','future','missing','existing','first','latest','none'].map(m => `<option value="${m}" ${cur.monitor_option === m ? 'selected' : ''}>${m}</option>`).join('')}
        </select>
      `;
    }
    target.innerHTML = `
      <div id="arr-defaults-form-${svc}" class="fs-12 mb-8" style="display:grid;grid-template-columns:140px 1fr;gap:6px 10px;align-items:center">
        <label for="arr-root-${svc}" class="t3">Root folder</label>
        <select id="arr-root-${svc}" class="input">
          ${rootFolderOptions || '<option value="">(none configured in arr)</option>'}
        </select>
        <label for="arr-quality-${svc}" class="t3">Quality profile</label>
        <select id="arr-quality-${svc}" class="input">
          ${qualityOptions || '<option value="">(none configured)</option>'}
        </select>
        ${extra}
      </div>
      <div class="row">
        <button type="button" class="btn btn-primary btn-sm" id="arr-defaults-save-${svc}" onclick="saveArrDefaults('${svc}',this)">Save defaults</button>
        <span id="arr-defaults-msg-${svc}" class="status"></span>
      </div>
    `;
    trackDirty(document.getElementById(`arr-defaults-form-${svc}`), document.getElementById(`arr-defaults-save-${svc}`));
  } catch (e) {
    target.innerHTML = _errHtml(e);
    btnDone(btn);
  }
}

export async function saveArrDefaults(svc, btn) {
  const root  = document.getElementById(`arr-root-${svc}`)?.value || '';
  const qual  = parseInt(document.getElementById(`arr-quality-${svc}`)?.value || '0', 10);
  const meta  = parseInt(document.getElementById(`arr-meta-${svc}`)?.value || '0', 10);
  const mon   = document.getElementById(`arr-monitor-${svc}`)?.value || '';
  const stype = document.getElementById(`arr-series-type-${svc}`)?.value || '';
  const msg   = document.getElementById(`arr-defaults-msg-${svc}`);
  btnBusy(btn, 'Saving…');
  setStatus(msg, 'saving…', 'busy');
  const body = {};
  if (root) body.root_folder_path = root;
  if (qual) body.quality_profile_id = qual;
  if (svc === 'sonarr' && stype) body.series_type = stype;
  if (svc === 'lidarr' && meta) body.metadata_profile_id = meta;
  if (mon) body.monitor_option = mon;
  try {
    await api(`/api/library/defaults/${svc}`, 'PUT', body);
    setStatus(msg, 'saved', 'ok');
    toast(`${ARR_LABELS[svc]} defaults saved`, 'success');
    btnDone(btn, null, {keepDisabled: true});
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}













// Two recommendation lanes: 'library' (owned, unwatched) and 'discovery' (not
// owned, taste-fit). 'all' stacks both. The lane is a client-side view filter
// over the cached recs — the scheduler always caches BOTH lanes.
// Moved state.currentRecsLane to state
