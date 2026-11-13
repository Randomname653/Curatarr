// ── SETUP WIZARD ──────────────────────────────────────────────────────────────
import { serverLabel, state } from './state.js';
import { refreshSpotifyPending, spotifyDropZone } from './spotify_import.js';
import { EL, SETUP_STEPS, SVG_WARN, SVG_X, _errHtml, _errMsg, act, actOn, btnBusy, btnDone, esc, focusDialog, loc, toast } from './ui.js';
import { api } from './api.js';
import { loadLibraryConfig } from './libraries.js';
import { loadHistoryStatus } from './history.js';
import { findJellyfinServers, jellyfinConnectHtml, stopJellyfinConnect } from './jellyfin_connect.js';
import { t, th } from './i18n.js';
function serverChoice() {
  const kind = state.setupData.media_server_type === 'jellyfin' ? 'jellyfin' : 'plex';
  return `<div class="form-group">
      <label for="s-media-server">${th('setup.media_server')}</label>
      <select id="s-media-server" ${actOn('change', 'setupServerKind')}>
        <option value="plex"${kind === 'plex' ? ' selected' : ''}>Plex</option>
        <option value="jellyfin"${kind === 'jellyfin' ? ' selected' : ''}>Jellyfin</option>
      </select>
      <div class="hint">${th('setup.media_server_hint')}</div>
    </div>`;
}


// A finished Jellyfin connect: the key stays on the server under the
// ticket, /complete hands it back.
function setupJellyfinConnected(c) {
  state.setupData.jellyfin_url = c.url;
  state.setupData.jellyfin_ticket = c.ticket;
  state.setupData.jellyfin_api_key = '';
  state.setupData.jellyfin_connected = {server_name: c.server_name, version: c.version, admin: c.admin};
  renderSetupStep(state.setupStep);
}

export function jellyfinConnectAgain() {
  state.setupData.jellyfin_ticket = '';
  state.setupData.jellyfin_connected = null;
  renderSetupStep(state.setupStep);
}

export function setupServerKind() {
  collectStep();
  state.setupData.media_server_type = document.getElementById('s-media-server')?.value === 'jellyfin' ? 'jellyfin' : 'plex';
  renderSetupStep(state.setupStep);
}

const SETUP_CONTENT = {
  // One media server per install (docs/JELLYFIN_DESIGN.md): Plex, or Jellyfin.
  plex: () => state.setupData.media_server_type === 'jellyfin' ? `
    <h2 class="setup-heading">${th('setup.connection', {server: 'Jellyfin'})}</h2>
    ${serverChoice()}
    ${jellyfinConnectHtml(state.setupData.jellyfin_url, state.setupData.jellyfin_ticket ? state.setupData.jellyfin_connected : null, setupJellyfinConnected)}
    ${state.setupData.jellyfin_ticket ? '' : `<details class="mt-12">
      <summary class="fs-12 t2 pointer">${th('setup.paste_key')}</summary>
      <div class="form-group mt-8">
        <label for="s-jellyfin-key">${th('setup.api_key_of', {service: 'Jellyfin'})}</label>
        <input id="s-jellyfin-key" type="password" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${esc(state.setupData.jellyfin_api_key||'')}">
        <div class="hint">${th('setup.jf_key_hint')}</div>
      </div>
      <button class="btn btn-secondary btn-sm" ${act('testConn', 'jellyfin')}>${th('setup.test_connection')}</button>
      <div id="test-jellyfin-result"></div>
    </details>`}` : `
    <h2 class="setup-heading">${th('setup.connection', {server: 'Plex'})}</h2>
    ${serverChoice()}
    <div class="form-group">
      <label for="s-plex-url">${th('setup.plex_url')}</label>
      <input id="s-plex-url" type="url" placeholder="http://192.168.1.100:32400" value="${state.setupData.plex_url||''}">
      <div class="hint">${th('setup.plex_url_hint')}</div>
    </div>
    <div class="form-group">
      <label for="s-plex-token">${th('setup.plex_token')}</label>
      <input id="s-plex-token" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${state.setupData.plex_token||''}">
      <div class="hint">${th('setup.plex_token_hint')}</div>
    </div>
    <button class="btn btn-secondary btn-sm" ${act('testConn', 'plex')}>${th('setup.test_connection')}</button>
    <div id="test-plex-result"></div>`,

  ollama: () => `
    <h2 class="setup-heading">${th('setup.ollama_heading')}</h2>
    <div class="form-group">
      <label for="s-ollama">${th('setup.ollama_endpoint')}</label>
      <input id="s-ollama" type="url" placeholder="http://localhost:11434" value="${state.setupData.ollama_endpoint||'http://localhost:11434'}">
    </div>
    <button class="btn btn-secondary btn-sm" ${act('testConn', 'ollama')}>${th('setup.detect_models')}</button>
    <div class="my-8" id="test-ollama-result"></div>
    <div class="mt-12 form-group">
      <label for="s-vram">GPU / VRAM</label>
      <div class="row">
        <button class="btn btn-secondary btn-sm" ${act('detectGpu')}>${th('setup.detect_gpu')}</button>
        <select class="w-auto" id="s-vram" ${actOn('change', 'refreshModelRecs')}>
          <option value="">${th('setup.vram_manual')}</option>
          <option value="8">8 GB</option>
          <option value="12">12 GB</option>
          <option value="16">16 GB</option>
          <option value="24">24 GB</option>
          <option value="32">32+ GB</option>
        </select>
      </div>
      <div class="mt-6" id="gpu-result"></div>
      <div class="hint">${th('setup.vram_hint')}</div>
    </div>
    <div class="my-8" id="model-recs"></div>
    <details class="mt-12" id="s-profile-wrap">
      <summary>${th('setup.profile_summary')}</summary>
      <div class="hint" id="s-profile-note"></div>
      <div class="form-group mt-8">
        <label for="s-llm-profile">${th('setup.profile')}</label>
        <select class="w-auto" id="s-llm-profile">
          <option value="large"${state.setupData.llm_profile === 'small' ? '' : ' selected'}>${th('setup.profile_large')}</option>
          <option value="small"${state.setupData.llm_profile === 'small' ? ' selected' : ''}>${th('setup.profile_small')}</option>
        </select>
        <div class="hint">${th('setup.profile_hint')}</div>
      </div>
      <div class="form-group">
        <label for="s-num-ctx">${th('setup.num_ctx')}</label>
        <input id="s-num-ctx" type="number" min="0" max="131072" step="1024" value="${state.setupData.curator_num_ctx ?? 0}">
      </div>
      <div class="form-group">
        <label for="s-num-gpu">${th('setup.num_gpu')}</label>
        <input id="s-num-gpu" type="number" min="-1" max="999" value="${state.setupData.curator_num_gpu ?? 99}">
        <div class="hint">${th('setup.num_gpu_hint')}</div>
      </div>
    </details>
    <div class="mt-12 form-group">
      <label for="s-curator-model">${th('setup.curator_model')}</label>
      <select id="s-curator-model" ${actOn('change', 'onSetupCuratorChange')}><option value="gemma4:31b">gemma4:31b (${th('setup.recommended_docs')})</option></select>
      <div class="hint">${th('setup.bake_hint')} <code>ollama create</code>.</div>
    </div>
    <div class="form-group">
      <label for="s-summarizer-model">${th('setup.summarizer_model')}</label>
      <select id="s-summarizer-model"><option value="granite4.1:8b">granite4.1:8b (${th('setup.recommended')})</option></select>
      <div class="hint">${th('setup.summarizer_hint')}</div>
    </div>
    <div class="form-group">
      <label for="s-embed-model">${th('setup.embed_model')}</label>
      <select id="s-embed-model"><option value="nomic-embed-text-v2-moe">nomic-embed-text-v2-moe (${th('setup.recommended')})</option></select>
    </div>
    <div class="mt-12 form-group">
      <label class="inline-flex pointer">
        <input type="checkbox" id="s-pitcher-enable" ${actOn('change', 'togglePitcherWrap', EL)} ${state.setupData.enable_pitcher?'checked':''}>
        ${th('setup.pitcher')}
      </label>
      <div class="hint">${th('setup.pitcher_hint')}</div>
      <div id="s-pitcher-wrap" class="mt-8"${state.setupData.enable_pitcher ? '' : ' hidden'}>
        <label for="s-pitcher-model">${th('setup.pitcher_model')}</label>
        <select id="s-pitcher-model"><option value="qwen3.8:27b">qwen3.8:27b (${th('setup.recommended_split')})</option></select>
        <div id="pitcher-note" class="hint"></div>
      </div>
    </div>
    <div class="form-group mt-12">
      <label class="row fs-13">
        <input type="checkbox" id="s-cpu-lane" ${state.setupData.llm_cpu_lane === false ? '' : 'checked'}>
        ${th('setup.cpu_lane')}
      </label>
      <div class="hint">${th('setup.cpu_lane_hint')}</div>
      <label for="s-cpu-threads" class="mt-8">${th('setup.cpu_threads')}</label>
      <input id="s-cpu-threads" type="number" min="1" max="64" value="${state.setupData.llm_cpu_threads || 6}">
      <div class="hint">${th('setup.cpu_threads_hint')}</div>
    </div>`,

  metadata: () => `
    <h2 class="setup-heading">${th('setup.metadata_heading')}</h2>
    <p class="fs-12 t2 mb-16">${th('setup.metadata_lead')}</p>
    <div class="form-group">
      <label for="s-tmdb">${th('setup.api_key_of', {service: 'TMDB'})} <span class="fs-10 badge muted">${th('setup.badge_movies_series')}</span></label>
      <input id="s-tmdb" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.tmdb_api_key||''}">
      <div class="hint">${th('setup.free_at')} <a class="t-amber" href="https://www.themoviedb.org/settings/api" target="_blank">themoviedb.org/settings/api</a>${th('setup.tmdb_hint')}</div>
    </div>
    <div class="form-group">
      <label for="s-omdb">${th('setup.api_key_of', {service: 'OMDb'})} <span class="fs-10 badge muted">${th('setup.badge_movies_series')}</span></label>
      <input id="s-omdb" placeholder="xxxxxxxx" value="${state.setupData.omdb_api_key||''}">
      <div class="hint">${th('setup.free_at')} <a class="t-amber" href="https://www.omdbapi.com/apikey.aspx" target="_blank">omdbapi.com</a>${th('setup.omdb_hint')}</div>
    </div>
    <div class="form-group">
      <label for="s-lastfm">${th('setup.api_key_of', {service: 'Last.fm'})} <span class="fs-10 badge muted">${th('setup.badge_music_only')}</span></label>
      <input id="s-lastfm" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.lastfm_api_key||''}">
      <div class="hint">${th('setup.lastfm_hint')}</div>
      <div class="used-for">${th('setup.skip_music')}</div>
    </div>
    <div class="form-group">
      <label for="s-spotify-id">${th('setup.spotify_id')} <span class="fs-10 badge muted">${th('setup.badge_music_only')}</span></label>
      <input id="s-spotify-id" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.spotify_client_id||''}">
      <div class="hint">${th('setup.free_at')} <a class="t-amber" href="https://developer.spotify.com/dashboard" target="_blank">developer.spotify.com</a>${th('setup.spotify_hint')}</div>
      <div class="used-for">${th('setup.skip_music')}</div>
    </div>
    <div class="form-group">
      <label for="s-spotify-secret">${th('setup.spotify_secret')}</label>
      <input id="s-spotify-secret" type="password" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.spotify_client_secret||''}">
      <button class="mt-6 btn btn-secondary btn-sm" ${act('testConn', 'spotify')}>${th('setup.test_service', {service: 'Spotify'})}</button>
      <div class="mt-4 fs-12" id="test-spotify-result"></div>
    </div>
    <div class="form-group">
      <label for="s-soulsync-url">${th('setup.soulsync_url')} <span class="fs-10 badge muted">${th('setup.badge_optional_music')}</span></label>
      <input id="s-soulsync-url" type="url" placeholder="http://192.168.1.100:12279" value="${state.setupData.soulsync_url||''}">
      <div class="hint">${th('setup.soulsync_hint')}</div>
    </div>
    <div class="form-group">
      <label for="s-soulsync-key">${th('setup.api_key_of', {service: 'SoulSync'})}</label>
      <input id="s-soulsync-key" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${state.setupData.soulsync_api_key||''}">
    </div>
    <div class="form-group">
      <label for="s-listenbrainz">${th('setup.listenbrainz')} <span class="fs-10 badge muted">${th('setup.badge_optional_music')}</span></label>
      <input id="s-listenbrainz" type="password" placeholder="${th('setup.listenbrainz_ph')}" value="${state.setupData.listenbrainz_token||''}">
      <div class="hint">${th('setup.listenbrainz_hint')} <a class="t-amber" href="https://listenbrainz.org/settings/" target="_blank">listenbrainz.org/settings</a>${th('setup.listenbrainz_hint_post')}</div>
    </div>`,

  arr: () => `
    <h2 class="setup-heading">${th('setup.arr_heading')}</h2>
    <p class="fs-12 t2 mb-16">${th('setup.arr_lead')}</p>
    ${[['Radarr', 'nav.movies', 7878], ['Sonarr', 'setup.arr_tv_anime', 8989], ['Lidarr', 'nav.music', 8686]].map(([name, whatKey, port])=>{
      const what = th(whatKey);
      const id = name.toLowerCase();
      return `<div class="boxed mb-12">
        <div class="inline-flex mb-10">
          <strong class="fs-13">${name}</strong>
          <span class="fs-10 badge muted">${what}</span>
        </div>
        <div class="mb-8 form-group">
          <label for="s-${id}-url">URL</label>
          <input id="s-${id}-url" type="url" placeholder="http://192.168.1.100:${port}" value="${state.setupData[id+'_url']||''}">
        </div>
        <div class="mb-8 form-group">
          <label for="s-${id}-key">${th('setup.api_key')}</label>
          <input id="s-${id}-key" type="password" placeholder="Settings → General → Security" value="${state.setupData[id+'_api_key']||''}">
        </div>
        <button class="btn btn-secondary btn-sm" ${act('testConn', id)}>${th('setup.test_service', {service: name})}</button>
        <div id="test-${id}-result"></div>
      </div>`;
    }).join('')}`,

  import: () => `
    <h2 class="setup-heading">${th('setup.import_heading')}</h2>
    <p class="fs-12 t2 mb-16">
      ${th('setup.import_p1')} <strong>${th('setup.import_s1')}</strong>
      ${th('setup.import_p2')} <strong>${th('setup.import_s2')}</strong>.
    </p>
    ${spotifyDropZone('su-sp')}
    <p class="fs-11 t3 mt-12">
      ${th('setup.import_later')}
    </p>`,

  done: () => `
    <div class="t-center py-20">
      <h2 class="fs-16 b t-amber mb-8">${th('setup.ready_heading')}</h2>
      <p class="setup-lead">
        ${th('setup.ready_pre')} <code>ollama create</code>${th('setup.ready_post')}
      </p>
      <button class="pad-12-32 fs-14 btn btn-primary" ${act('finishSetup')}>
        ${th('setup.save_start')}
      </button>
      <div class="mt-16 t2 fs-13" id="setup-saving"></div>
    </div>`,
};

export function renderSetupStep(step) {
  state.setupStep = step;
  const key = SETUP_STEPS[step];
  stopJellyfinConnect();
  document.getElementById('setup-body').innerHTML = SETUP_CONTENT[key]();
  const steps = SETUP_STEPS.length - 1;   // the closing summary is not a step
  document.getElementById('setup-step-label').textContent = step < steps ? t('setup.step_of', {n: step + 1, total: steps}) : t('setup.ready');
  document.getElementById('setup-back').style.visibility = step > 0 ? 'visible' : 'hidden';
  document.getElementById('setup-next').style.display = step === SETUP_STEPS.length-1 ? 'none' : 'inline-block';
  const firstDot = document.getElementById('step-dot-0');   // the media server's step, named after it
  if (firstDot) firstDot.textContent = state.setupData.media_server_type === 'jellyfin' ? 'Jellyfin' : 'Plex';
  SETUP_STEPS.forEach((s,i) => {
    const dot = document.getElementById(`step-dot-${i}`);
    if (dot) dot.className = 'setup-step-dot' + (i===step?' active':i<step?' done':'');
  });

  // Load Ollama models if on ollama step
  if (key === 'ollama' && state.setupData.ollama_endpoint) {
    testConn('ollama');
  }
  if (key === 'import') refreshSpotifyPending('su-sp');
  if (key === 'plex' && state.setupData.media_server_type === 'jellyfin' && !state.setupData.jellyfin_ticket) {
    findJellyfinServers();
  }
}

export function collectStep() {
  const key = SETUP_STEPS[state.setupStep];
  if (key === 'plex') {
    const kind = document.getElementById('s-media-server')?.value;
    if (kind) state.setupData.media_server_type = kind;
    if (document.getElementById('s-plex-url')) {
      state.setupData.plex_url = document.getElementById('s-plex-url').value.trim();
      state.setupData.plex_token = document.getElementById('s-plex-token')?.value?.trim() || '';
    }
    if (document.getElementById('s-jellyfin-url')) {
      state.setupData.jellyfin_url = document.getElementById('s-jellyfin-url').value.trim();
      state.setupData.jellyfin_api_key = document.getElementById('s-jellyfin-key')?.value?.trim() || '';
    }
  } else if (key === 'ollama') {
    state.setupData.ollama_endpoint = document.getElementById('s-ollama')?.value?.trim() || 'http://localhost:11434';
    state.setupData.base_curator_model = document.getElementById('s-curator-model')?.value || 'gemma4:31b';
    state.setupData.base_summarizer_model = document.getElementById('s-summarizer-model')?.value || 'granite4.1:8b';
    state.setupData.embedding_model = document.getElementById('s-embed-model')?.value || 'nomic-embed-text-v2-moe';
    state.setupData.enable_pitcher = !!document.getElementById('s-pitcher-enable')?.checked;
    state.setupData.base_pitcher_model = document.getElementById('s-pitcher-model')?.value || 'qwen3.8:27b';
    state.setupData.vram_gb = parseFloat(document.getElementById('s-vram')?.value) || state.setupData.vram_gb || null;
    state.setupData.llm_cpu_lane = !!document.getElementById('s-cpu-lane')?.checked;
    state.setupData.llm_cpu_threads = parseInt(document.getElementById('s-cpu-threads')?.value, 10) || 6;
    const profile = document.getElementById('s-llm-profile')?.value;
    if (profile) state.setupData.llm_profile = profile;
    const ctx = parseInt(document.getElementById('s-num-ctx')?.value, 10);
    if (Number.isFinite(ctx)) state.setupData.curator_num_ctx = ctx;
    const gpu = parseInt(document.getElementById('s-num-gpu')?.value, 10);
    if (Number.isFinite(gpu)) state.setupData.curator_num_gpu = gpu;
  } else if (key === 'metadata') {
    state.setupData.listenbrainz_token = document.getElementById('s-listenbrainz')?.value?.trim() || '';
    state.setupData.tmdb_api_key = document.getElementById('s-tmdb')?.value?.trim() || '';
    state.setupData.omdb_api_key = document.getElementById('s-omdb')?.value?.trim() || '';
    state.setupData.soulsync_url = document.getElementById('s-soulsync-url')?.value?.trim() || '';
    state.setupData.soulsync_api_key = document.getElementById('s-soulsync-key')?.value?.trim() || '';
    state.setupData.lastfm_api_key = document.getElementById('s-lastfm')?.value?.trim() || '';
    state.setupData.spotify_client_id = document.getElementById('s-spotify-id')?.value?.trim() || '';
    state.setupData.spotify_client_secret = document.getElementById('s-spotify-secret')?.value?.trim() || '';
  } else if (key === 'arr') {
    state.setupData.radarr_url = document.getElementById('s-radarr-url')?.value?.trim() || '';
    state.setupData.radarr_api_key = document.getElementById('s-radarr-key')?.value?.trim() || '';
    state.setupData.sonarr_url = document.getElementById('s-sonarr-url')?.value?.trim() || '';
    state.setupData.sonarr_api_key = document.getElementById('s-sonarr-key')?.value?.trim() || '';
    state.setupData.lidarr_url = document.getElementById('s-lidarr-url')?.value?.trim() || '';
    state.setupData.lidarr_api_key = document.getElementById('s-lidarr-key')?.value?.trim() || '';
  }
}

export function setupNav(dir) {
  collectStep();
  renderSetupStep(Math.max(0, Math.min(SETUP_STEPS.length-1, state.setupStep + dir)));
}

export async function testConn(service) {
  const resultEl = document.getElementById(`test-${service}-result`);
  if (resultEl) resultEl.innerHTML = `<div class="test-status">${th('setup.testing')}</div>`;

  let url='', token_='', apiKey='';
  if (service==='plex') { url=document.getElementById('s-plex-url')?.value; token_=document.getElementById('s-plex-token')?.value; }
  else if (service==='jellyfin') { url=document.getElementById('s-jellyfin-url')?.value; apiKey=document.getElementById('s-jellyfin-key')?.value; }
  else if (service==='ollama') { url=document.getElementById('s-ollama')?.value || state.setupData.ollama_endpoint; }
  else if (service==='radarr') { url=document.getElementById('s-radarr-url')?.value; apiKey=document.getElementById('s-radarr-key')?.value; }
  else if (service==='sonarr') { url=document.getElementById('s-sonarr-url')?.value; apiKey=document.getElementById('s-sonarr-key')?.value; }
  else if (service==='lidarr') { url=document.getElementById('s-lidarr-url')?.value; apiKey=document.getElementById('s-lidarr-key')?.value; }
  else if (service==='tmdb') { apiKey=document.getElementById('s-tmdb')?.value; }
  else if (service==='lastfm') { apiKey=document.getElementById('s-lastfm')?.value; }
  else if (service==='spotify') {
    const clientId = document.getElementById('s-spotify-id')?.value;
    const clientSecret = document.getElementById('s-spotify-secret')?.value;
    try {
      const r = await api('/api/setup/test', 'POST', {service, client_id: clientId, client_secret: clientSecret});
      if (resultEl) resultEl.innerHTML = r.ok
        ? `<div class="test-status ok">${th('setup.spotify_ok')}</div>`
        : `<div class="test-status err">${esc(r.error)}</div>`;
    } catch(e) { if (resultEl) resultEl.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`; }
    return;
  }

  try {
    const r = await api('/api/setup/test', 'POST', {service, url, token: token_, api_key: apiKey});
    // Pass 97: backend may attach ``privacy_warning`` when the endpoint
    // doesn't look private (RFC1918 / loopback / .local). Render it as a
    // yellow banner under the connection result. Doesn't block the user
    // from continuing — informational only.
    const warnBlock = r.privacy_warning
      ? `<div class="t-amber mt-4 test-status">${SVG_WARN} ${esc(r.privacy_warning)}</div>`
  : '';
    if (resultEl) resultEl.innerHTML = (r.ok
      ? `<div class="test-status ok">${th('setup.connected')}${r.server_name ? ' — '+esc(r.server_name) : r.version ? ' v'+esc(r.version) : ''}</div>`
      : `<div class="test-status err">${esc(r.error)}</div>`) + warnBlock;

    // Feed the recommendation panel. This used to overwrite the three
    // selects with the RAW installed-model list, wiping the recommended
    // defaults; the catalog-driven renderer merges bench-verified
    // recommendations with what's installed instead.
    if (service==='ollama' && r.ok && r.models) {
      state.setupData._detected_models = r.models;
      refreshModelRecs();
    }
  } catch(e) {
    if (resultEl) resultEl.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

// ── GPU DETECTION + MODEL RECOMMENDATIONS (setup wizard, Ollama step) ────────
export async function detectGpu() {
  const el = document.getElementById('gpu-result');
  if (el) el.innerHTML = `<div class="test-status">${th('setup.probing_gpu')}</div>`;
  try {
    const r = await api('/api/setup/gpu');
    if (!r.ok) {
      if (el) el.innerHTML = `<div class="test-status err">${esc(r.error)}</div>`;
      return;
    }
    const names = r.gpus.map(g => `${g.name} (${g.vram_gb} GB)`).join(', ');
    if (el) el.innerHTML = `<div class="test-status ok">${esc(names)}</div>`
      + (r.busy_note ? `<div class="t-amber test-status">${SVG_WARN} ${esc(r.busy_note)}</div>` : '');
    state.setupData.vram_gb = r.vram_gb;
    state.setupData.gpu_name = r.name || null;
    state.setupData.ram_gb = r.ram_gb ?? null;
    const sel = document.getElementById('s-vram');
    if (sel) {
      [...sel.options].filter(o => o.dataset.detected).forEach(o => o.remove());
      const opt = document.createElement('option');
      opt.value = r.vram_gb; opt.textContent = t('setup.gb_detected', {gb: r.vram_gb});
      opt.dataset.detected = '1'; opt.selected = true;
      sel.appendChild(opt);
    }
    refreshModelRecs();
  } catch(e) {
    if (el) el.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

export async function refreshModelRecs() {
  const vsel = document.getElementById('s-vram');
  if (vsel && vsel.value) state.setupData.vram_gb = parseFloat(vsel.value);
  const endpoint = document.getElementById('s-ollama')?.value?.trim() || state.setupData.ollama_endpoint || '';
  const panel = document.getElementById('model-recs');
  try {
    const r = await api('/api/setup/recommend', 'POST',
                        {ollama_endpoint: endpoint, vram_gb: state.setupData.vram_gb ?? null,
                         gpu_name: state.setupData.gpu_name ?? null,
                         ram_gb: state.setupData.ram_gb ?? null});
    renderRecSelect('s-curator-model', r.curator, r.installed);
    renderRecSelect('s-summarizer-model', r.summarizer, r.installed);
    renderRecSelect('s-embed-model', r.embedding, r.installed);
    renderRecSelect('s-pitcher-model', r.pitcher, r.installed);
    const pn = document.getElementById('pitcher-note');
    if (pn) pn.textContent = r.pitcher_note || '';
    if (panel) panel.innerHTML = (r.floor_note
      ? `<div class="t-amber test-status">${SVG_WARN} ${esc(r.floor_note)}</div>`
      : '') + renderHardwarePackage(r.package);
    state.setupPackage = r.package || null;
    onSetupCuratorChange();
  } catch(e) {
    if (panel) panel.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

// The bench package's curator profile goes into the wizard's profile fields,
// which the setup saves (LLM_PROFILE / CURATOR_NUM_CTX / CURATOR_NUM_GPU).
// Until 2026-11-01 they were only shown as .env lines to copy by hand.
// The bench's values for the curator the select shows now: a model it measured
// at this card size gets its own profile, any other keeps what the fields say.
export function onSetupCuratorChange() {
  const pkg = state.setupPackage;
  const model = document.getElementById('s-curator-model')?.value || '';
  const note = document.getElementById('s-profile-note');
  if (!pkg?.by_model) { if (note) note.textContent = ''; return; }
  const found = pkg.by_model[model] || pkg.by_model[model.replace(/:latest$/, '')];
  if (found) _applyPackageProfile(found);
  if (note) {
    note.textContent = found
      ? t('setup.bench_values', {model, gb: pkg.tier_gb})
      : t('setup.bench_unmeasured', {model: model || t('setup.this_model'), gb: pkg.tier_gb});
  }
}

export function _applyPackageProfile(rec) {
  if (!rec) return;
  const values = {'s-llm-profile': rec.profile, 's-num-ctx': rec.num_ctx, 's-num-gpu': rec.num_gpu};
  for (const [id, v] of Object.entries(values)) {
    const el = document.getElementById(id);
    if (el && v !== undefined && v !== null) el.value = String(v);
  }
  if (rec.profile) state.setupData.llm_profile = rec.profile;
  if (Number.isFinite(rec.num_ctx)) state.setupData.curator_num_ctx = rec.num_ctx;
  if (Number.isFinite(rec.num_gpu)) state.setupData.curator_num_gpu = rec.num_gpu;
}

// The hardware-matrix bench's measured package for this card: which curator,
// with which profile / context / GPU layers, and how fast it ran. The profile
// is saved with the setup; the Ollama server settings it names are not
// Curatarr's to write and are shown as lines to set there.
export function renderHardwarePackage(pkg) {
  if (!pkg) return '';
  const r = pkg.recommended;
  if (!r) return pkg.note ? `<div class="t3 fs-12 test-status">${esc(pkg.note)}</div>` : '';
  const ex = r.expected || {};
  const speed = pkg.estimate
    ? th('setup.speed_estimate', {tok: pkg.estimate.gen_tok_s, basis: pkg.estimate.basis})
    : th('setup.speed_measured', {tok: ex.gen_tok_s ?? '?', gpu: (r.measured_on || {}).gpu || t('setup.bench_card')});
  const q = r.quality || {};
  const quality = q.score25 != null ? `${t('setup.quality', {score: q.score25})}${q.provisional ? ` ${t('setup.provisional')}` : ''}`
    : t('setup.quality_none');
  const env = [`LLM_PROFILE=${r.profile}`, `CURATOR_NUM_CTX=${r.num_ctx}`,
               `CURATOR_NUM_GPU=${r.num_gpu}`];
  const srv = Object.entries(r.server || {})
    .filter(([k]) => k === 'OLLAMA_KV_CACHE_TYPE' || k === 'OLLAMA_FLASH_ATTENTION')
    .map(([k, v]) => `${k}=${v}`);
  // An offloaded package keeps most of its model in system RAM; when this
  // host has too little, the server already picked the alternative that fits.
  const rf = pkg.ram_fallback;
  const ramNote = !rf ? ''
    : rf.instead_of
      ? `<div class="t-amber test-status">${SVG_WARN} ${th('setup.ram_instead', {model: rf.instead_of, need: rf.needs_gb, host: rf.host_gb, rec: r.model})}</div>`
      : `<div class="t-amber test-status">${SVG_WARN} ${th('setup.ram_short', {need: rf.needs_gb, host: rf.host_gb})}</div>`;
  // How this curator argues, on one fictional example (docs/CURATOR_EXAMPLES.md has all of them).
  const sample = pkg.example;
  const example = !sample ? '' : `<details class="fs-12"><summary>${th('setup.example_summary', {model: r.model})}</summary>`
    + `<div class="t3">${esc(sample.title)} · ${th('setup.owner')} ${esc(sample.owner)}. ${esc(sample.setup)}</div>`
    + `<div><b>${esc(sample.verdict_words || sample.verdict)}</b> (${esc(sample.protecting_pillar)}): ${esc(sample.reason)}</div>`
    + (sample.note ? `<blockquote class="t2">${esc(sample.note)}</blockquote>` : '')
    + `<div class="t3">${th('setup.all_examples')}</div></details>`;
  return ramNote + `<div class="test-status">${th('setup.bench_result', {gb: pkg.tier_gb})} <b>${esc(r.model)}</b>`
    + `${r.installed ? ` (${th('setup.installed')})` : ''} — ${speed}; ${th('setup.judge_per_title', {s: ex.judge_s ?? '?'})}`
    + ` ${esc(quality)}${ex.gpu_share_pct < 100 ? `; ${th('setup.gpu_share', {pct: ex.gpu_share_pct})}` : ''}.</div>`
    + `<div class="t3 fs-12">${th('setup.saved_with', {env: env.join(' · ')})}`
    + (srv.length ? ` ${th('setup.set_on_ollama', {srv: srv.join(' · ')})}` : '') + `</div>` + example;
}

export function renderRecSelect(id, entries, installed) {
  const sel = document.getElementById(id);
  if (!sel || !entries) return;
  const cur = sel.value;
  const catalogNames = entries.map(e => e.model);
  const opts = entries.map(e => {
    const tags = [];
    if (e.installed) tags.push(t('setup.installed_no_download'));
    // need_gb is the bench's MEASURED footprint at the role's context when it
    // has one; otherwise the weight file + headroom, said to be an estimate.
    if (e.fits === false) tags.push(`${t('setup.needs_gb', {gb: Math.round(e.need_gb)})} ${t(e.fit_basis === 'measured' ? 'setup.measured' : 'setup.estimate')}`);
    else if (e.tight) tags.push(t('setup.tight_fit'));
    const suffix = tags.length ? ` · ${tags.join(' · ')}` : '';
    const unfit = e.fits === false ? 'data-unfit="1"' : '';
    return `<option value="${esc(e.model)}" ${unfit}>${esc(e.model)} — ${esc(e.label)}${esc(suffix)}</option>`;
  });
  // Everything else on the server stays reachable — labeled honestly.
  const extra = (installed || []).filter(m => !catalogNames.includes(m))
    .map(m => `<option value="${esc(m)}">${esc(m)} — ${th('setup.untested')}</option>`);
  sel.innerHTML = opts.concat(extra).join('');
  // Keep the user's pick unless it no longer fits; the list is
  // preference-ordered (fitting + installed first), so index 0 is the call.
  const kept = cur && [...sel.options].find(o => o.value === cur && !o.dataset.unfit);
  if (kept) sel.value = cur;
}

// ── ONBOARDING ────────────────────────────────────────────────────────────────
let onboardingStep = null;

export async function showOnboarding(step) {
  onboardingStep = step;
  const overlay = document.getElementById('onboarding-overlay');
  overlay.classList.remove('hidden');
  // a dialog: focus goes in once the step has rendered (app.js traps Tab there)
  renderOnboardingStep(step).then(() => focusDialog(overlay));
}

export function hideOnboarding() {
  document.getElementById('onboarding-overlay').classList.add('hidden');
  loadLibraryConfig();
  loadHistoryStatus();
}

export async function renderOnboardingStep(step) {
  const body = document.getElementById('onboarding-body');
  const title = document.getElementById('onboarding-title');
  const sub = document.getElementById('onboarding-sub');

  if (step === 'libraries') {
    title.textContent = t('setup.ob_libs_title', {server: serverLabel()});
    sub.textContent = t('setup.ob_libs_sub', {server: serverLabel()});

    // A failed discover used to read as "no libraries": an empty table whose
    // Save & Continue stored an empty mapping. It is an error with a Retry.
    let disc;
    try { disc = await api('/api/libraries/discover'); }
    catch (e) {
      state.discoverSections = [];
      body.innerHTML = `${_errHtml(e, act('retryOnboardingStep'))}
        <p class="fs-12 t3 mt-8">${th('setup.ob_unreachable', {server: serverLabel()})}</p>`;
      return;
    }
    state.discoverSections = disc.sections || [];

    body.innerHTML = `
      <div class="mb-20 tbl-wrap">
        <table class="tbl tbl-cards"><thead><tr><th>${th('setup.ob_library')}</th><th>${th('setup.ob_server_type', {server: serverLabel()})}</th><th>${th('setup.ob_category')}</th></tr></thead><tbody>
        ${state.discoverSections.map(s=>`<tr>
          <td class="cell-primary fw-500">${esc(s.title)}</td>
          <td class="t3 fs-12">${esc(s.type)}</td>
          <td><select class="field-sm" id="ob-libcat-${s.key}" aria-label="${th('setup.ob_category_aria')}">
            ${['music','movie','show','anime','ignore'].map(o=>`<option value="${o}" ${(s.suggested_category)===o?'selected':''}>${th(`setup.cat.${o}`)}</option>`).join('')}
          </select></td>
        </tr>`).join('')}
        </tbody></table>
      </div>
      <button class="w-full p-12 btn btn-primary" ${act('saveOnboardingLibraries', EL)}>
        ${th('setup.ob_save_continue')}
      </button>`;
  }

  else if (step === 'sync') {
    title.textContent = t('setup.ob_sync_title');
    sub.textContent = t('setup.ob_sync_sub', {server: serverLabel()});
    body.innerHTML = `
      <div class="mb-20" id="ob-sync-status">
        <div class="mb-8 progress-bar"><div class="w-0 progress-fill" id="ob-sync-bar"></div></div>
        <div class="fs-12 t2" id="ob-sync-label" role="status" aria-live="polite">${th('setup.ob_waiting')}</div>
      </div>
      <button class="w-full p-12 btn btn-primary" id="ob-sync-btn" ${act('startOnboardingSync')}>
        ${th('setup.ob_start_sync')}
      </button>`;
  }

  else if (step === 'models') {
    title.textContent = t('setup.ob_models_title');
    sub.textContent = t('setup.ob_models_sub');
    body.innerHTML = `
      <p class="fs-13 t2 mb-16">
        ${th('setup.ob_creates')} <code>curatarr-curator</code> ${th('common.and')} <code>curatarr-summarizer</code>
        ${th('setup.ob_creates_post')}
      </p>
      <div class="mb-20" id="ob-model-status"></div>
      <button class="w-full p-12 btn btn-primary" ${act('buildOnboardingModels')}>
        ${th('setup.ob_build')}
      </button>
      <button class="w-full p-10 mt-8 btn btn-secondary" ${act('hideOnboarding')}>
        ${th('setup.ob_skip')}
      </button>`;
  }

  else if (step === 'done') {
    title.textContent = t('setup.ob_done_title');
    sub.textContent = t('setup.ob_done_sub');
    body.innerHTML = `
      <div class="t-center py-20">
        <p class="fs-13 t2 lh-17 mb-24">
          ${th('setup.ob_done_p1')}<br>
          ${th('setup.ob_done_p2')}
        </p>
        <button class="pad-12-32 fs-14 btn btn-primary" ${act('hideOnboarding')}>
          ${th('setup.ob_open')}
        </button>
      </div>`;
  }
}

// Retry for an onboarding step that failed to load.
export function retryOnboardingStep() { return renderOnboardingStep(onboardingStep || 'libraries'); }

export async function saveOnboardingLibraries(btn) {
  const libraries = state.discoverSections.map(s=>({
    key: s.key, title: s.title, plex_type: s.type,
    category: document.getElementById(`ob-libcat-${s.key}`)?.value || s.suggested_category,
  }));
  btnBusy(btn, t('setup.saving'));
  try {
    await api('/api/libraries/configure','POST',{libraries});
  } catch (e) {
    // Used to be an unhandled rejection: the button did nothing, silently.
    toast(t('setup.ob_save_failed', {error: _errMsg(e)}), 'danger');
    btnDone(btn);
    return;
  }
  state.libraryCfg = libraries;
  renderOnboardingStep('sync');
  onboardingStep = 'sync';
}

let _onboardingPoll = null;
const ONBOARDING_POLL_MAX_FAILS = 5;
export function _stopOnboardingPoll() {
  if (_onboardingPoll) { clearInterval(_onboardingPoll); _onboardingPoll = null; }
}

export async function startOnboardingSync() {
  const btn = document.getElementById('ob-sync-btn');
  const lbl = document.getElementById('ob-sync-label');
  const bar = document.getElementById('ob-sync-bar');
  if (btn) { btn.disabled = true; btn.textContent = t('setup.syncing'); }

  // If a previous attempt was running, kill it before we start the new one.
  _stopOnboardingPoll();

  try {
    await api('/api/history/sync?force=true','POST');
    lbl.textContent = t('setup.fetching');

    // Sync should finish well within 30 minutes — past that, give up so we
    // don't keep hitting /api/history/status forever if onboarding is left
    // open in a stale tab.
    const deadline = Date.now() + 30 * 60 * 1000;
    // The poll used to swallow every error for the whole 30 minutes and then
    // stop without a word, the button stuck on "Syncing…". Five misses in a
    // row, or the deadline, now say so and give the button back as Retry.
    let fails = 0;
    const giveUp = why => {
      _stopOnboardingPoll();
      if (lbl) lbl.textContent = why;
      if (btn) { btn.disabled = false; btn.textContent = t('common.retry'); }
    };

    _onboardingPoll = setInterval(async () => {
      // If onboarding moved on or the user navigated, stop hitting the API.
      if (onboardingStep !== 'sync') { _stopOnboardingPoll(); return; }
      if (Date.now() > deadline) {
        giveUp(t('setup.sync_timeout'));
        return;
      }
      try {
        const s = await api('/api/history/status');
        fails = 0;
        const entries = s.watch_history_entries || 0;
        lbl.textContent = t('setup.entries_synced', {x: entries.toLocaleString(loc())});
        if (entries > 0) bar.style.width = '60%';
        if (s.taste_vector?.computed) {
          bar.style.width = '100%';
          lbl.textContent = t('setup.entries_done', {x: entries.toLocaleString(loc())});
          _stopOnboardingPoll();
          setTimeout(() => {
            renderOnboardingStep('models');
            onboardingStep = 'models';
          }, 1500);
        }
      } catch (e) {
        if (++fails >= ONBOARDING_POLL_MAX_FAILS) {
          giveUp(t('setup.sync_lost', {error: _errMsg(e)}));
        }
      }
    }, 3000);
  } catch(e) {
    _stopOnboardingPoll();
    if (lbl) lbl.textContent = t('setup.error', {error: _errMsg(e)});
    if (btn) { btn.disabled = false; btn.textContent = t('common.retry'); }
  }
}

export async function buildOnboardingModels() {
  const el = document.getElementById('ob-model-status');
  el.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('setup.building')}</p>`;

  try {
    const r = await api('/api/setup/build-models','POST');
    if (r.curator && r.summarizer) {
      const okLine = `<p class="t-success">${th('setup.models_created')}</p>`;
      el.innerHTML = okLine + `<p class="loading" role="status" aria-live="polite">${th('setup.mb_running')}</p>`;
      // A model that doesn't quite fit VRAM runs silently part-on-CPU and
      // the whole app just feels broken-slow. Catch that HERE, not on the
      // user's first real conversation - with the calls production makes.
      let mb = null, mbErr = null;
      try {
        mb = await api('/api/setup/minibench', 'POST',
                       {vram_gb: state.setupData?.vram_gb ?? null, gpu_name: state.setupData?.gpu_name ?? null});
      } catch (e) { mbErr = _errMsg(e); }
      el.innerHTML = okLine + renderMinibench(mb, mbErr);
      setTimeout(() => { renderOnboardingStep('done'); onboardingStep = 'done'; },
                 !mb || !mb.ok || mb.verdict !== 'ok' ? 12000 : 4000);
    } else {
      let msg = '';
      if (!r.curator) msg += `${SVG_X} ${th('setup.model_failed', {model: 'curatarr-curator'})}<br>`;
      if (!r.summarizer) msg += `${SVG_X} ${th('setup.model_failed', {model: 'curatarr-summarizer'})}<br>`;
      msg += `<br>${th('setup.pull_base')}`;
      el.innerHTML = `<div class="t-danger fs-13">${msg}</div>`;
    }
  } catch(e) {
    el.innerHTML = `<div class="t-danger">${esc(_errMsg(e))}</div>`;
  }
}

// The mini-bench result: what this machine did with the curator, next to
// what the hardware bench measured for the card. "Unknown" is never shown as
// "fits" - a check that could not run says so.
export function renderMinibench(mb, err) {
  if (err || !mb || !mb.ok) {
    return `<p class="t-amber">${SVG_WARN} ${th('setup.mb_failed', {error: err || mb?.error || t('setup.no_answer')})}</p>`;
  }
  const w = mb.warmup || {};
  const gen = mb.gen_tok_s ?? w.tokens_per_s;
  const pkg = mb.package;
  const exp = pkg?.expected || {};
  const bench = pkg
    ? ` · ${th('setup.mb_bench', {s: exp.judge_s ?? '?', tok: (pkg.estimate?.gen_tok_s ?? exp.gen_tok_s) ?? '?', gb: pkg.tier_gb})}`
    : '';
  const line = th('setup.mb_line', {s: mb.judge_s_median ?? '?', tok: gen ?? '?'})
    + (w.load_s ? ` · ${th('setup.mb_load', {s: w.load_s})}` : '') + bench;
  const srv = Object.entries(pkg?.server || {}).map(([k, v]) => `${k}=${v}`).join(' · ');
  const kv = srv ? `<div class="t3 fs-12">${th('setup.mb_kv', {srv})}</div>` : '';
  const reason = mb.reason ? ` — ${esc(mb.reason)}` : '';
  if (mb.verdict === 'ok')
    return `<p class="t-success">${th('setup.mb_passed')} ${line}${reason}</p>${kv}`;
  const advice = ['spill', 'slow', 'unreliable'].includes(mb.verdict) ? t(`setup.mb_advice.${mb.verdict}`) : '';
  return `<p class="t-amber">${SVG_WARN} ${th('setup.mb_label')} ${esc(mb.verdict)}${reason}. ${esc(advice)}</p><div class="t3 fs-12">${line}</div>${kv}`;
}

export async function logout() {
  // Server first: bumping the user's token version revokes THIS token on
  // every device, not just the copy in this tab's localStorage.
  try { await api('/api/auth/logout', 'POST'); } catch {}
  state.token=''; localStorage.removeItem('curatarr_token'); location.reload();
}

// The Pitcher checkbox shows or hides the model picker beneath it.
export function togglePitcherWrap(el) { document.getElementById('s-pitcher-wrap').hidden = !el.checked; }
