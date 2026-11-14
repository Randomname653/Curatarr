// ── SETUP WIZARD ──────────────────────────────────────────────────────────────
// Rebuilt after the fresh-install test of 2026-11-14: one plex.tv sign-in
// instead of a hand-copied token, the AI step detects on its own and says in
// plain words what will run, TMDB is marked as needed, a closing overview
// says what is set and what is missing, and finishing signs the owner in
// (no second "Sign in with Plex", no restart).
import { serverLabel, state } from './state.js';
import { refreshSpotifyPending, spotifyDropZone } from './spotify_import.js';
import { netflixDropZone, refreshNetflixPending } from './netflix_import.js';
import { EL, SETUP_STEPS, SVG_CHECK, SVG_WARN, _errHtml, _errMsg, act, actOn, btnBusy, btnDone, esc, focusDialog, loc, toast } from './ui.js';
import { api } from './api.js';
import { loadLibraryConfig } from './libraries.js';
import { loadHistoryStatus } from './history.js';
import { findJellyfinServers, jellyfinConnectHtml, stopJellyfinConnect } from './jellyfin_connect.js';
import { t, th } from './i18n.js';

const DOT_KEYS = {plex: 'setup.v2.dot_server', ollama: 'setup.v2.dot_ai', metadata: 'setup.v2.dot_metadata',
                  arr: 'settings.arr_apps', import: 'setup.v2.dot_import', done: 'setup.v2.dot_done'};

function isJellyfin() { return state.setupData.media_server_type === 'jellyfin'; }

// The wordmark at the head of the wizard: the same inline logo the sign-in
// screen carries, cloned once.
function _brandHeader() {
  const slot = document.getElementById('setup-logo');
  if (!slot || slot.firstElementChild) return;
  const logo = document.querySelector('#auth-overlay [data-brand-slot] .brand-logo');
  if (logo) {
    const c = logo.cloneNode(true);
    c.setAttribute('class', 'brand-logo setup-logo-svg');
    slot.appendChild(c);
  }
}

// ── step 1: the media server ────────────────────────────────────────────────
function serverChoice() {
  const kind = isJellyfin() ? 'jellyfin' : 'plex';
  const card = (value, name, sub) => `
    <label class="wiz-choice${kind === value ? ' on' : ''}">
      <input type="radio" name="s-media-server" value="${value}" ${kind === value ? 'checked' : ''} ${actOn('change', 'setupServerKind', EL)}>
      <span class="wiz-choice-body"><b>${name}</b><span class="t3 fs-12">${sub}</span></span>
    </label>`;
  return `<div class="wiz-choices" role="radiogroup" aria-label="${th('setup.media_server')}">
      ${card('plex', 'Plex', th('setup.v2.plex_sub'))}
      ${card('jellyfin', 'Jellyfin', th('setup.v2.jellyfin_sub'))}
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

export function setupServerKind(el) {
  collectStep();
  const v = el?.value || document.querySelector('input[name="s-media-server"]:checked')?.value;
  state.setupData.media_server_type = v === 'jellyfin' ? 'jellyfin' : 'plex';
  renderSetupStep(state.setupStep);
}

function plexServersHtml() {
  const d = state.setupData;
  const servers = d.plex_servers || [];
  if (!servers.length) {
    return `<div class="banner warn mt-12"><span class="banner-icon">${SVG_WARN}</span><span class="banner-text">${th('setup.v2.plex_no_server', {account: esc(d.plex_account || '')})}</span></div>
      <button class="btn btn-secondary btn-sm mt-8" ${act('plexConnectAgain')}>${th('setup.v2.plex_other_account')}</button>`;
  }
  const rows = servers.map(s => `
    <label class="wiz-choice${d.plex_server_id === s.server_id ? ' on' : ''}">
      <input type="radio" name="s-plex-server" value="${esc(s.server_id)}" ${d.plex_server_id === s.server_id ? 'checked' : ''} ${actOn('change', 'pickPlexServer', EL)}>
      <span class="wiz-choice-body"><b>${esc(s.name)}</b>
        <span class="fs-12 ${s.reachable ? 't3' : 't-amber'}">${s.reachable ? esc(s.url) : th('setup.v2.plex_unreachable')}</span></span>
    </label>`).join('');
  const picked = servers.find(s => s.server_id === d.plex_server_id);
  const needsAddress = picked && !picked.reachable;
  return `<p class="test-status ok">${th('setup.v2.plex_signed_in', {account: esc(d.plex_account || 'Plex')})}</p>
    <p class="fs-13 t2 mt-12 mb-8">${th(servers.length > 1 ? 'setup.v2.plex_pick' : 'setup.v2.plex_found')}</p>
    <div class="wiz-choices col">${rows}</div>
    ${needsAddress ? `<div class="form-group mt-12">
        <label for="s-plex-url">${th('setup.v2.plex_address')}</label>
        <input id="s-plex-url" type="url" placeholder="http://192.168.1.100:32400" value="${esc(d.plex_url || '')}">
        <div class="hint">${th('setup.v2.plex_address_hint')}</div>
      </div>` : ''}
    <button class="link-num fs-12 mt-8" ${act('plexConnectAgain')}>${th('setup.v2.plex_other_account')}</button>`;
}

function plexStepHtml() {
  const d = state.setupData;
  if (d.plex_ticket) return plexServersHtml();
  const waiting = d._plex_code ? `
    <div class="wiz-pin mt-12" role="status" aria-live="polite">
      <p class="fs-13 t2">${th('setup.v2.plex_waiting')}</p>
      <p class="fs-12 t3 mt-8">${th('setup.v2.plex_code_pre')} <a class="t-amber" href="https://plex.tv/link" target="_blank" rel="noopener noreferrer">plex.tv/link</a>:</p>
      <div class="setup-qc-code">${esc(d._plex_code.slice(0, 4))} ${esc(d._plex_code.slice(4))}</div>
      <small class="t3" id="plex-connect-status"></small>
    </div>` : '';
  return `<p class="fs-13 t2 mb-12">${th('setup.v2.plex_lead')}</p>
    <button class="btn btn-primary wiz-big" id="plex-connect-btn" ${act('startPlexConnect', EL)}>${th('auth.view.plex')}</button>
    ${waiting}
    <details class="mt-20">
      <summary class="fs-12 t2 pointer">${th('setup.v2.plex_manual')}</summary>
      <div class="form-group mt-8">
        <label for="s-plex-url">${th('setup.plex_url')}</label>
        <input id="s-plex-url" type="url" placeholder="http://192.168.1.100:32400" value="${esc(d.plex_url || '')}">
      </div>
      <div class="form-group">
        <label for="s-plex-token">${th('setup.plex_token')}</label>
        <input id="s-plex-token" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${esc(d.plex_token || '')}">
        <div class="hint">${th('setup.v2.plex_token_hint')} <a class="t-amber" href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noopener noreferrer">support.plex.tv</a></div>
      </div>
      <button class="btn btn-secondary btn-sm" ${act('testConn', 'plex')}>${th('setup.test_connection')}</button>
      <div id="test-plex-result"></div>
    </details>`;
}

let _plexPoll = null;
function stopPlexConnect() { if (_plexPoll) { clearInterval(_plexPoll); _plexPoll = null; } }

export async function startPlexConnect(btn) {
  // A tab opened now, inside the click, is not a blocked pop-up; it gets
  // plex.tv's address once the PIN is there.
  const tab = window.open('', '_blank');
  btnBusy(btn, t('setup.v2.plex_opening'));
  let r;
  try {
    r = await api('/api/setup/plex/connect', 'POST');
  } catch (e) {
    if (tab) tab.close();
    btnDone(btn);
    toast(t('auth.plex_start_failed', {error: _errMsg(e)}), 'danger');
    return;
  }
  if (tab) tab.location.href = r.auth_url;
  else window.open(r.auth_url, '_blank');
  state.setupData._plex_code = r.code;
  state.setupData._plex_login = {id: r.login_id, nonce: r.nonce};
  renderSetupStep(state.setupStep);
  const until = Date.now() + (r.expires_in || 900) * 1000;
  stopPlexConnect();
  _plexPoll = setInterval(async () => {
    const st = document.getElementById('plex-connect-status');
    if (Date.now() > until) {
      stopPlexConnect();
      state.setupData._plex_code = null;
      renderSetupStep(state.setupStep);
      toast(t('auth.plex_code_expired'), 'amber');
      return;
    }
    const login = state.setupData._plex_login;
    if (!login) { stopPlexConnect(); return; }
    try {
      const res = await api(`/api/setup/plex/connect/${encodeURIComponent(login.id)}`, 'GET', null, false,
                            {'X-Plex-Login-Nonce': login.nonce});
      if (res.status !== 'ok') return;
      stopPlexConnect();
      const d = state.setupData;
      d.plex_ticket = res.ticket;
      d.plex_account = res.account || '';
      d.plex_servers = res.servers || [];
      d._plex_code = null;
      d._plex_login = null;
      const reachable = d.plex_servers.filter(s => s.reachable);
      d.plex_server_id = (reachable.length === 1 ? reachable[0] : d.plex_servers.length === 1 ? d.plex_servers[0] : null)?.server_id || '';
      renderSetupStep(state.setupStep);
    } catch (e) {
      if (st) st.textContent = _errMsg(e);
      if (e.status === 403) stopPlexConnect();   // expired or foreign: polling will not help
    }
  }, 3000);
}

export function plexConnectAgain() {
  stopPlexConnect();
  Object.assign(state.setupData, {plex_ticket: '', plex_servers: null, plex_server_id: '',
                                  plex_account: '', _plex_code: null, _plex_login: null});
  renderSetupStep(state.setupStep);
}

export function pickPlexServer(el) {
  collectStep();
  state.setupData.plex_server_id = el?.value || '';
  renderSetupStep(state.setupStep);
}

// ── step 2: the AI ──────────────────────────────────────────────────────────
function aiStepHtml() {
  const d = state.setupData;
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.ai_lead')}</p>
    <div class="wiz-status">
      <div class="wiz-status-row"><span class="wiz-status-name">Ollama</span><span class="grow" id="test-ollama-result"><span class="test-status">${th('setup.testing')}</span></span></div>
      <div class="wiz-status-row"><span class="wiz-status-name">${th('setup.v2.gpu')}</span><span class="grow" id="gpu-result"><span class="test-status">${th('setup.probing_gpu')}</span></span></div>
    </div>
    <div class="wiz-plan mt-16" id="wiz-ai-plan" role="status" aria-live="polite"></div>
    <details class="mt-16" id="s-ai-advanced">
      <summary class="fs-12 t2 pointer">${th('setup.v2.ai_advanced')}</summary>
      <div class="form-group mt-12">
        <label for="s-ollama">${th('setup.ollama_endpoint')}</label>
        <div class="row">
          <input class="grow" id="s-ollama" type="url" placeholder="http://localhost:11434" value="${esc(d.ollama_endpoint || 'http://localhost:11434')}">
          <button class="btn btn-secondary btn-sm" ${act('testConn', 'ollama')}>${th('setup.detect_models')}</button>
        </div>
        <div class="hint">${th('setup.v2.ollama_hint')}</div>
      </div>
      <div class="form-group">
        <label for="s-vram">${th('setup.v2.vram')}</label>
        <select class="w-auto" id="s-vram" ${actOn('change', 'refreshModelRecs')}>
          <option value="">${th('setup.vram_manual')}</option>
          <option value="4">4 GB</option>
          <option value="8">8 GB</option>
          <option value="12">12 GB</option>
          <option value="16">16 GB</option>
          <option value="24">24 GB</option>
          <option value="32">32+ GB</option>
        </select>
        <div class="hint">${th('setup.vram_hint')}</div>
      </div>
      <div class="my-8" id="model-recs"></div>
      <div class="form-group">
        <label for="s-curator-model">${th('setup.curator_model')}</label>
        <select id="s-curator-model" ${actOn('change', 'onSetupCuratorChange')}><option value="gemma4:31b">gemma4:31b (${th('setup.recommended_docs')})</option></select>
      </div>
      <div class="form-group">
        <label for="s-summarizer-model">${th('setup.summarizer_model')}</label>
        <select id="s-summarizer-model" ${actOn('change', 'setupModelChanged')}><option value="granite4.1:8b">granite4.1:8b</option></select>
        <div class="hint">${th('setup.summarizer_hint')}</div>
      </div>
      <div class="form-group">
        <label for="s-embed-model">${th('setup.embed_model')}</label>
        <select id="s-embed-model" ${actOn('change', 'setupModelChanged')}><option value="nomic-embed-text-v2-moe">nomic-embed-text-v2-moe</option></select>
      </div>
      <details class="mt-12" id="s-profile-wrap">
        <summary class="fs-12 t2 pointer">${th('setup.profile_summary')}</summary>
        <div class="hint" id="s-profile-note"></div>
        <div class="form-group mt-8">
          <label for="s-llm-profile">${th('setup.profile')}</label>
          <select class="w-auto" id="s-llm-profile">
            <option value="large"${d.llm_profile === 'small' ? '' : ' selected'}>${th('setup.profile_large')}</option>
            <option value="small"${d.llm_profile === 'small' ? ' selected' : ''}>${th('setup.profile_small')}</option>
          </select>
          <div class="hint">${th('setup.profile_hint')}</div>
        </div>
        <div class="form-group">
          <label for="s-num-ctx">${th('setup.num_ctx')}</label>
          <input id="s-num-ctx" type="number" min="0" max="131072" step="1024" value="${d.curator_num_ctx ?? 0}">
        </div>
        <div class="form-group">
          <label for="s-num-gpu">${th('setup.num_gpu')}</label>
          <input id="s-num-gpu" type="number" min="-1" max="999" value="${d.curator_num_gpu ?? 99}">
          <div class="hint">${th('setup.num_gpu_hint')}</div>
        </div>
      </details>
      <div class="mt-12 form-group">
        <label class="inline-flex pointer">
          <input type="checkbox" id="s-pitcher-enable" ${actOn('change', 'togglePitcherWrap', EL)} ${d.enable_pitcher ? 'checked' : ''}>
          ${th('setup.pitcher')}
        </label>
        <div class="hint">${th('setup.v2.pitcher_hint')}</div>
        <div id="s-pitcher-wrap" class="mt-8"${d.enable_pitcher ? '' : ' hidden'}>
          <label for="s-pitcher-model">${th('setup.pitcher_model')}</label>
          <select id="s-pitcher-model" ${actOn('change', 'setupModelChanged')}><option value="qwen3.8:27b">qwen3.8:27b</option></select>
          <div id="pitcher-note" class="hint"></div>
        </div>
      </div>
      <div class="form-group mt-12">
        <label class="row fs-13">
          <input type="checkbox" id="s-cpu-lane" ${d.llm_cpu_lane === false ? '' : 'checked'}>
          ${th('setup.cpu_lane')}
        </label>
        <div class="hint">${th('setup.cpu_lane_hint')}</div>
        <label for="s-cpu-threads" class="mt-8">${th('setup.cpu_threads')}</label>
        <input id="s-cpu-threads" type="number" min="1" max="64" value="${d.llm_cpu_threads || 6}">
        <div class="hint">${th('setup.cpu_threads_hint')}</div>
      </div>
    </details>`;
}

// The plain-words plan above the advanced section: what runs, and what
// still has to be downloaded. Redrawn whenever a select or the card changes.
function renderAiPlan() {
  const el = document.getElementById('wiz-ai-plan');
  if (!el) return;
  const pick = id => document.getElementById(id)?.value || '';
  const installed = new Set(state.setupData._detected_models || []);
  const has = m => installed.has(m) || installed.has(m + ':latest');
  const models = [['setup.v2.role_curator', pick('s-curator-model')],
                  ['setup.v2.role_summarizer', pick('s-summarizer-model')],
                  ['setup.v2.role_embed', pick('s-embed-model')]];
  if (document.getElementById('s-pitcher-enable')?.checked) models.push(['setup.v2.role_pitcher', pick('s-pitcher-model')]);
  const missing = models.filter(([, m]) => m && !has(m));
  const rows = models.map(([role, m]) => `<div class="wiz-plan-row"><span class="t3">${th(role)}</span><span><b>${esc(m)}</b>
      <span class="fs-12 ${has(m) ? 't-success' : 't3'}">${has(m) ? th('setup.v2.installed') : th('setup.v2.will_download')}</span></span></div>`).join('');
  const card = state.setupData.gpu_name ? `${esc(state.setupData.gpu_name)} · ${state.setupData.vram_gb} GB`
    : state.setupData.vram_gb ? `${state.setupData.vram_gb} GB` : '';
  el.innerHTML = `<div class="fs-13 mb-8">${card ? th('setup.v2.plan_for', {card}) : th('setup.v2.plan')}</div>${rows}
    <div class="hint mt-8">${missing.length ? th('setup.v2.plan_download') : th('setup.v2.plan_ready')}</div>`;
}

// ── step 3: metadata ────────────────────────────────────────────────────────
function metadataStepHtml() {
  const d = state.setupData;
  const link = (href, text) => `<a class="t-amber" href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.meta_lead')}</p>
    <div class="wiz-card">
      <div class="row"><b class="grow">TMDB</b><span class="badge badge-sm amber">${th('setup.v2.needed')}</span></div>
      <p class="fs-12 t2 mt-4">${th('setup.v2.tmdb_why')} ${link('https://www.themoviedb.org/settings/api', 'themoviedb.org/settings/api')}</p>
      <div class="row mt-8">
        <input class="grow" id="s-tmdb" aria-label="${th('setup.api_key_of', {service: 'TMDB'})}" placeholder="${th('setup.v2.tmdb_ph')}" value="${esc(d.tmdb_api_key || '')}">
        <button class="btn btn-secondary btn-sm" ${act('testConn', 'tmdb')}>${th('setup.v2.test')}</button>
      </div>
      <div id="test-tmdb-result"></div>
    </div>
    <div class="wiz-card mt-12">
      <div class="row"><b class="grow">OMDb</b><span class="badge badge-sm muted">${th('setup.v2.recommended')}</span></div>
      <p class="fs-12 t2 mt-4">${th('setup.v2.omdb_why')} ${link('https://www.omdbapi.com/apikey.aspx', 'omdbapi.com')}</p>
      <input class="mt-8" id="s-omdb" aria-label="${th('setup.api_key_of', {service: 'OMDb'})}" placeholder="xxxxxxxx" value="${esc(d.omdb_api_key || '')}">
    </div>
    <details class="mt-16">
      <summary class="fs-12 t2 pointer">${th('setup.v2.music_more')}</summary>
      <div class="form-group mt-12">
        <label for="s-lastfm">${th('setup.api_key_of', {service: 'Last.fm'})}</label>
        <input id="s-lastfm" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${esc(d.lastfm_api_key || '')}">
        <div class="hint">${th('setup.lastfm_hint')} ${link('https://www.last.fm/api/account/create', 'last.fm/api')}</div>
      </div>
      <div class="form-group">
        <label for="s-spotify-id">${th('setup.spotify_id')}</label>
        <input id="s-spotify-id" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${esc(d.spotify_client_id || '')}">
        <div class="hint">${th('setup.free_at')} ${link('https://developer.spotify.com/dashboard', 'developer.spotify.com')}${th('setup.spotify_hint')}</div>
      </div>
      <div class="form-group">
        <label for="s-spotify-secret">${th('setup.spotify_secret')}</label>
        <input id="s-spotify-secret" type="password" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${esc(d.spotify_client_secret || '')}">
        <button class="mt-6 btn btn-secondary btn-sm" ${act('testConn', 'spotify')}>${th('setup.test_service', {service: 'Spotify'})}</button>
        <div class="mt-4 fs-12" id="test-spotify-result"></div>
      </div>
      <div class="form-group">
        <label for="s-soulsync-url">${th('setup.soulsync_url')}</label>
        <input id="s-soulsync-url" type="url" placeholder="http://192.168.1.100:12279" value="${esc(d.soulsync_url || '')}">
        <div class="hint">${th('setup.v2.soulsync_hint')}</div>
      </div>
      <div class="form-group">
        <label for="s-soulsync-key">${th('setup.api_key_of', {service: 'SoulSync'})}</label>
        <input id="s-soulsync-key" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${esc(d.soulsync_api_key || '')}">
      </div>
      <div class="form-group">
        <label for="s-listenbrainz">${th('setup.listenbrainz')}</label>
        <input id="s-listenbrainz" type="password" placeholder="${th('setup.listenbrainz_ph')}" value="${esc(d.listenbrainz_token || '')}">
        <div class="hint">${th('setup.listenbrainz_hint')} ${link('https://listenbrainz.org/settings/', 'listenbrainz.org/settings')}${th('setup.listenbrainz_hint_post')}</div>
      </div>
    </details>`;
}

// ── step 4: arr apps ────────────────────────────────────────────────────────
function arrCard(name, whatKey, port) {
  const d = state.setupData;
  const id = name.toLowerCase();
  return `<div class="wiz-card mb-12">
      <div class="row mb-8"><b class="grow">${name}</b><span class="badge badge-sm muted">${th(whatKey)}</span></div>
      <div class="mb-8 form-group">
        <label for="s-${id}-url">URL</label>
        <input id="s-${id}-url" type="url" placeholder="http://192.168.1.100:${port}" value="${esc(d[id + '_url'] || '')}">
      </div>
      <div class="mb-8 form-group">
        <label for="s-${id}-key">${th('setup.api_key')}</label>
        <input id="s-${id}-key" type="password" placeholder="${th('setup.v2.arr_key_ph')}" value="${esc(d[id + '_api_key'] || '')}">
      </div>
      <button class="btn btn-secondary btn-sm" ${act('testConn', id)}>${th('setup.test_service', {service: name})}</button>
      <div id="test-${id}-result"></div>
    </div>`;
}

function arrStepHtml() {
  return `<p class="fs-13 t2 mb-8">${th('setup.v2.arr_lead', {server: isJellyfin() ? 'Jellyfin' : 'Plex'})}</p>
    <div class="banner warn mb-16"><span class="banner-icon">${SVG_WARN}</span><span class="banner-text">${th('setup.v2.arr_delete')}</span></div>
    ${arrCard('Radarr', 'nav.movies', 7878)}
    ${arrCard('Sonarr', 'setup.arr_tv_anime', 8989)}
    <details>
      <summary class="fs-12 t2 pointer">${th('setup.v2.lidarr_more')}</summary>
      <div class="mt-12">${arrCard('Lidarr', 'nav.music', 8686)}</div>
    </details>`;
}

// ── step 5: viewing and listening history ───────────────────────────────────
function importStepHtml() {
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.import_lead')}</p>
    <div class="wiz-card">
      <b>Spotify</b>
      <p class="fs-12 t2 mt-4 mb-8">${th('setup.import_p1')} <strong>${th('setup.import_s1')}</strong> ${th('setup.import_p2')} <strong>${th('setup.import_s2')}</strong>.</p>
      ${spotifyDropZone('su-sp')}
    </div>
    <div class="wiz-card mt-12">
      <b>Netflix</b>
      <p class="fs-12 t2 mt-4 mb-8">${th('setup.v2.netflix_how')} <a class="t-amber" href="https://www.netflix.com/account/getmyinfo" target="_blank" rel="noopener noreferrer">netflix.com/account/getmyinfo</a></p>
      ${netflixDropZone()}
    </div>
    <p class="fs-12 t3 mt-12">${th('setup.v2.import_later')}</p>`;
}

// ── the overview ────────────────────────────────────────────────────────────
function summaryHtml() {
  const d = state.setupData;
  const line = (ok, label, text, warnText) => `<div class="wiz-sum-row">
      <span class="${ok ? 't-success' : 't-amber'}">${ok ? SVG_CHECK : SVG_WARN}</span>
      <span class="wiz-sum-label">${label}</span>
      <span class="grow fs-13 ${ok ? 't2' : 't-amber'}">${ok ? text : warnText}</span></div>`;
  const server = isJellyfin()
    ? (d.jellyfin_connected?.server_name || d.jellyfin_url || '')
    : ((d.plex_servers || []).find(s => s.server_id === d.plex_server_id)?.name || d.plex_url || '');
  const arrs = ['radarr', 'sonarr', 'lidarr'].filter(a => d[a + '_url'] && d[a + '_api_key'])
    .map(a => a[0].toUpperCase() + a.slice(1));
  return `<div class="wiz-sum">
      ${line(!!server, isJellyfin() ? 'Jellyfin' : 'Plex', esc(server), th('setup.v2.sum_no_server'))}
      ${line(true, th('setup.v2.dot_ai'), esc(d.base_curator_model || 'gemma4:31b') + (d.vram_gb ? ` · ${d.vram_gb} GB` : ''), '')}
      ${line(!!d.tmdb_api_key, 'TMDB', th('setup.v2.sum_set'), th('setup.v2.sum_no_tmdb'))}
      ${line(!!d.omdb_api_key, 'OMDb', th('setup.v2.sum_set'), th('setup.v2.sum_no_omdb'))}
      ${line(arrs.length > 0, th('settings.arr_apps'), esc(arrs.join(', ')), th('setup.v2.sum_no_arr'))}
    </div>
    <p class="fs-12 t3 mt-16">${th('setup.v2.sum_next')}</p>
    <button class="btn btn-primary wiz-big mt-16" id="setup-finish" ${act('finishSetup')}>${th('setup.v2.finish')}</button>
    <div class="mt-12 t2 fs-13" id="setup-saving" role="status" aria-live="polite"></div>`;
}

const SETUP_CONTENT = {
  plex: () => `<h2 class="setup-heading">${th('setup.v2.server_heading')}</h2>${serverChoice()}<div class="mt-20">${isJellyfin()
    ? `${jellyfinConnectHtml(state.setupData.jellyfin_url, state.setupData.jellyfin_ticket ? state.setupData.jellyfin_connected : null, setupJellyfinConnected)}
      ${state.setupData.jellyfin_ticket ? '' : `<details class="mt-12">
        <summary class="fs-12 t2 pointer">${th('setup.paste_key')}</summary>
        <div class="form-group mt-8">
          <label for="s-jellyfin-key">${th('setup.api_key_of', {service: 'Jellyfin'})}</label>
          <input id="s-jellyfin-key" type="password" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${esc(state.setupData.jellyfin_api_key || '')}">
          <div class="hint">${th('setup.jf_key_hint')}</div>
        </div>
        <button class="btn btn-secondary btn-sm" ${act('testConn', 'jellyfin')}>${th('setup.test_connection')}</button>
        <div id="test-jellyfin-result"></div>
      </details>`}`
    : plexStepHtml()}</div>`,
  ollama: () => `<h2 class="setup-heading">${th('setup.v2.ai_heading')}</h2>${aiStepHtml()}`,
  metadata: () => `<h2 class="setup-heading">${th('setup.v2.meta_heading')}</h2>${metadataStepHtml()}`,
  arr: () => `<h2 class="setup-heading">${th('settings.arr_apps')}</h2>${arrStepHtml()}`,
  import: () => `<h2 class="setup-heading">${th('setup.v2.import_heading')}</h2>${importStepHtml()}`,
  done: () => `<h2 class="setup-heading">${th('setup.v2.sum_heading')}</h2>${summaryHtml()}`,
};

export function renderSetupStep(step) {
  _brandHeader();
  state.setupStep = step;
  const key = SETUP_STEPS[step];
  stopJellyfinConnect();
  if (key !== 'plex') stopPlexConnect();
  document.getElementById('setup-body').innerHTML = SETUP_CONTENT[key]();
  const dots = document.getElementById('setup-steps');
  if (dots) {
    dots.innerHTML = SETUP_STEPS.map((s, i) => `<div class="setup-step-dot${i === step ? ' active' : i < step ? ' done' : ''}"${i === step ? ' aria-current="step"' : ''}>${th(DOT_KEYS[s])}</div>`).join('');
  }
  const steps = SETUP_STEPS.length - 1;   // the closing overview is not a step
  document.getElementById('setup-step-label').textContent = step < steps ? t('setup.step_of', {n: step + 1, total: steps}) : '';
  document.getElementById('setup-back').classList.toggle('invisible', step === 0);
  document.getElementById('setup-next').hidden = step === SETUP_STEPS.length - 1;

  if (key === 'ollama') {
    // Detection runs by itself: the endpoint's models and this machine's card.
    testConn('ollama');
    detectGpu();
  }
  if (key === 'import') { refreshSpotifyPending('su-sp'); refreshNetflixPending(); }
  if (key === 'plex' && isJellyfin() && !state.setupData.jellyfin_ticket) findJellyfinServers();
  document.getElementById('setup-body').scrollTop = 0;
}

export function collectStep() {
  const key = SETUP_STEPS[state.setupStep];
  const val = id => document.getElementById(id)?.value?.trim() || '';
  const d = state.setupData;
  if (key === 'plex') {
    if (document.getElementById('s-plex-url')) d.plex_url = val('s-plex-url');
    if (document.getElementById('s-plex-token')) d.plex_token = val('s-plex-token');
    if (document.getElementById('s-jellyfin-url')) d.jellyfin_url = val('s-jellyfin-url');
    if (document.getElementById('s-jellyfin-key')) d.jellyfin_api_key = val('s-jellyfin-key');
  } else if (key === 'ollama') {
    d.ollama_endpoint = val('s-ollama') || d.ollama_endpoint || 'http://localhost:11434';
    d.base_curator_model = document.getElementById('s-curator-model')?.value || 'gemma4:31b';
    d.base_summarizer_model = document.getElementById('s-summarizer-model')?.value || 'granite4.1:8b';
    d.embedding_model = document.getElementById('s-embed-model')?.value || 'nomic-embed-text-v2-moe';
    state.setupData.enable_pitcher = !!document.getElementById('s-pitcher-enable')?.checked;
    d.base_pitcher_model = document.getElementById('s-pitcher-model')?.value || 'qwen3.8:27b';
    d.vram_gb = parseFloat(document.getElementById('s-vram')?.value) || d.vram_gb || null;
    state.setupData.llm_cpu_lane = !!document.getElementById('s-cpu-lane')?.checked;
    state.setupData.llm_cpu_threads = parseInt(document.getElementById('s-cpu-threads')?.value, 10) || 6;
    const profile = document.getElementById('s-llm-profile')?.value;
    if (profile) d.llm_profile = profile;
    const ctx = parseInt(document.getElementById('s-num-ctx')?.value, 10);
    if (Number.isFinite(ctx)) d.curator_num_ctx = ctx;
    const gpu = parseInt(document.getElementById('s-num-gpu')?.value, 10);
    if (Number.isFinite(gpu)) d.curator_num_gpu = gpu;
  } else if (key === 'metadata') {
    for (const [id, field] of [['s-listenbrainz', 'listenbrainz_token'], ['s-tmdb', 'tmdb_api_key'],
                               ['s-omdb', 'omdb_api_key'], ['s-soulsync-url', 'soulsync_url'],
                               ['s-soulsync-key', 'soulsync_api_key'], ['s-lastfm', 'lastfm_api_key'],
                               ['s-spotify-id', 'spotify_client_id'], ['s-spotify-secret', 'spotify_client_secret']]) {
      if (document.getElementById(id)) d[field] = val(id);
    }
  } else if (key === 'arr') {
    for (const a of ['radarr', 'sonarr', 'lidarr']) {
      if (document.getElementById(`s-${a}-url`)) d[a + '_url'] = val(`s-${a}-url`);
      if (document.getElementById(`s-${a}-key`)) d[a + '_api_key'] = val(`s-${a}-key`);
    }
  }
}

// Why the media-server step cannot be left yet, or '' when it can.
function serverMissing() {
  const d = state.setupData;
  if (isJellyfin()) return (d.jellyfin_ticket || (d.jellyfin_url && d.jellyfin_api_key)) ? '' : t('setup.v2.need_jellyfin');
  if (d.plex_ticket) {
    const s = (d.plex_servers || []).find(x => x.server_id === d.plex_server_id);
    if (!s) return t('setup.v2.need_pick');
    return s.reachable || d.plex_url ? '' : t('setup.v2.need_address');
  }
  return d.plex_url && d.plex_token ? '' : t('setup.v2.need_plex');
}

export function setupNav(dir) {
  collectStep();
  if (dir > 0 && SETUP_STEPS[state.setupStep] === 'plex') {
    const why = serverMissing();
    if (why) { toast(why, 'amber'); return; }
  }
  renderSetupStep(Math.max(0, Math.min(SETUP_STEPS.length - 1, state.setupStep + dir)));
}

export async function testConn(service) {
  const resultEl = document.getElementById(`test-${service}-result`);
  if (resultEl) resultEl.innerHTML = `<div class="test-status">${th('setup.testing')}</div>`;

  let url = '', token_ = '', apiKey = '';
  const v = id => document.getElementById(id)?.value;
  if (service === 'plex') { url = v('s-plex-url'); token_ = v('s-plex-token'); }
  else if (service === 'jellyfin') { url = v('s-jellyfin-url'); apiKey = v('s-jellyfin-key'); }
  else if (service === 'ollama') { url = v('s-ollama') || state.setupData.ollama_endpoint || 'http://localhost:11434'; }
  else if (['radarr', 'sonarr', 'lidarr'].includes(service)) { url = v(`s-${service}-url`); apiKey = v(`s-${service}-key`); }
  else if (service === 'tmdb') { apiKey = v('s-tmdb'); }
  else if (service === 'lastfm') { apiKey = v('s-lastfm'); }
  else if (service === 'spotify') {
    try {
      const r = await api('/api/setup/test', 'POST', {service, client_id: v('s-spotify-id'), client_secret: v('s-spotify-secret')});
      if (resultEl) resultEl.innerHTML = r.ok
        ? `<div class="test-status ok">${th('setup.spotify_ok')}</div>`
        : `<div class="test-status err">${esc(r.error)}</div>`;
    } catch (e) { if (resultEl) resultEl.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`; }
    return;
  }

  try {
    const r = await api('/api/setup/test', 'POST', {service, url, token: token_, api_key: apiKey});
    // Pass 97: backend may attach ``privacy_warning`` when the endpoint
    // doesn't look private (RFC1918 / loopback / .local). Informational only.
    const warnBlock = r.privacy_warning
      ? `<div class="t-amber mt-4 test-status">${SVG_WARN} ${esc(r.privacy_warning)}</div>`
      : '';
    let okText = `${th('setup.connected')}${r.server_name ? ' — ' + esc(r.server_name) : r.version ? ' v' + esc(r.version) : ''}`;
    if (service === 'ollama' && r.ok) okText = th('setup.v2.ollama_found', {url: esc(url), n: (r.models || []).length});
    if (resultEl) resultEl.innerHTML = (r.ok
      ? `<div class="test-status ok">${okText}</div>`
      : `<div class="test-status err">${service === 'ollama' ? th('setup.v2.ollama_missing', {url: esc(url)}) + ' ' : ''}${esc(r.error)}</div>`) + warnBlock;
    if (service === 'ollama') {
      // The catalog-driven renderer merges the bench's picks with what is installed.
      state.setupData._detected_models = r.ok ? (r.models || []) : [];
      refreshModelRecs();
      if (!r.ok) document.getElementById('s-ai-advanced')?.setAttribute('open', '');
    }
  } catch (e) {
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
      if (el) el.innerHTML = `<div class="test-status err">${th('setup.v2.gpu_none')}</div>`;
      document.getElementById('s-ai-advanced')?.setAttribute('open', '');
      renderAiPlan();
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
    renderRecSelect('s-curator-model', r.curator, r.installed, 'chat');
    renderRecSelect('s-summarizer-model', r.summarizer, r.installed, 'chat');
    renderRecSelect('s-embed-model', r.embedding, r.installed, 'embed');
    renderRecSelect('s-pitcher-model', r.pitcher, r.installed, 'chat');
    const pn = document.getElementById('pitcher-note');
    if (pn) pn.textContent = r.pitcher_note || '';
    if (panel) panel.innerHTML = (r.floor_note
      ? `<div class="t-amber test-status">${SVG_WARN} ${esc(r.floor_note)}</div>`
      : '') + renderHardwarePackage(r.package);
    state.setupPackage = r.package || null;
    onSetupCuratorChange();
    renderAiPlan();
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
  renderAiPlan();
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

// role 'embed' lists only embedding models, 'chat' everything else: every list
// used to offer every installed model, the embedder as a curator and the
// 31b chat model as an embedder, plus Curatarr's own baked tags.
export function renderRecSelect(id, entries, installed, role) {
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
  const fitsRole = m => !role || (role === 'embed' ? /embed/i.test(m) : !/embed/i.test(m) && !/^curatarr-/.test(m));
  const extra = (installed || []).filter(m => !catalogNames.includes(m) && fitsRole(m))
    .map(m => `<option value="${esc(m)}">${esc(m)} — ${th('setup.untested')}</option>`);
  sel.innerHTML = opts.concat(extra).join('');
  // Keep the user's pick unless it no longer fits; the list is
  // preference-ordered (fitting + installed first), so index 0 is the call.
  const kept = cur && [...sel.options].find(o => o.value === cur && !o.dataset.unfit);
  if (kept) sel.value = cur;
}

// ── ONBOARDING ────────────────────────────────────────────────────────────────
let onboardingStep = null;
let onboardingEntries = 0;

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
        <table class="tbl tbl-cards"><thead><tr><th>${th('setup.ob_library')}</th><th>${th('setup.v2.ob_holds')}</th><th>${th('setup.ob_category')}</th></tr></thead><tbody>
        ${state.discoverSections.map(s=>`<tr>
          <td class="cell-primary fw-500">${esc(s.title)}</td>
          <td class="t3 fs-12">${th(`setup.v2.kind.${['movie', 'show', 'artist'].includes(s.type) ? s.type : 'other'}`)}</td>
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
      <button hidden class="w-full p-12 btn btn-primary" id="ob-sync-btn" ${act('startOnboardingSync')}>
        ${th('setup.ob_start_sync')}
      </button>`;
    startOnboardingSync();   // the sync starts by itself: one click fewer
  }

  else if (step === 'done') {
    title.textContent = t('setup.v2.ob_done_title');
    sub.textContent = t('setup.v2.ob_done_sub');
    body.innerHTML = `
      <div class="wiz-sum">
        <div class="wiz-sum-row"><span class="t-success">${SVG_CHECK}</span><span class="grow fs-13">${th('setup.v2.ob_done_synced', {n: (onboardingEntries || 0).toLocaleString(loc())})}</span></div>
        <div class="wiz-sum-row"><span class="t-amber">${SVG_WARN}</span><span class="grow fs-13">${th('setup.v2.ob_done_enrich')}</span></div>
        <div class="wiz-sum-row"><span class="t3">${SVG_CHECK}</span><span class="grow fs-13 t2">${th('setup.v2.ob_done_where')}</span></div>
      </div>
      <div class="mt-16" id="ob-model-status"></div>
      <div class="row mt-16">
        <button class="btn btn-secondary" ${act('runOnboardingSpeedCheck', EL)}>${th('setup.v2.ob_speed')}</button>
        <button class="btn btn-primary grow" ${act('hideOnboarding')}>${th('setup.ob_open')}</button>
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
  if (btn) { btn.hidden = true; btn.disabled = true; btn.textContent = t('setup.syncing'); }

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
    let polls = 0;
    const giveUp = why => {
      _stopOnboardingPoll();
      if (lbl) lbl.textContent = why;
      if (btn) { btn.hidden = false; btn.disabled = false; btn.textContent = t('common.retry'); }
    };

    _onboardingPoll = setInterval(async () => {
      // If onboarding moved on or the user navigated, stop hitting the API.
      if (onboardingStep !== 'sync') { _stopOnboardingPoll(); return; }
      if (Date.now() > deadline) {
        giveUp(t('setup.sync_timeout'));
        return;
      }
      try {
        // The bar ends with the sync itself. It used to wait for a taste
        // profile, which needs hours of enrichment on a fresh install:
        // 14 minutes at 60 % (fresh-install test 2026-11-14).
        const s = await api('/api/history/sync-state');
        fails = 0;
        const entries = s.entries || 0;
        onboardingEntries = entries;
        lbl.textContent = entries ? t('setup.entries_synced', {x: entries.toLocaleString(loc())}) : t('setup.fetching');
        polls += 1;
        bar.style.width = `${Math.min(90, 10 + polls * 4)}%`;
        if (!s.running && polls > 1) {
          bar.style.width = '100%';
          lbl.textContent = t('setup.entries_done', {x: entries.toLocaleString(loc())});
          _stopOnboardingPoll();
          setTimeout(() => {
            renderOnboardingStep('done');
            onboardingStep = 'done';
          }, 1200);
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
    if (btn) { btn.hidden = false; btn.disabled = false; btn.textContent = t('common.retry'); }
  }
}

// The wizard built the models already; this only measures them: a model
// that does not quite fit the card runs part-on-CPU and the whole app just
// feels broken-slow. Caught here, with the calls production makes, rather
// than in the first real conversation.
export async function runOnboardingSpeedCheck(btn) {
  const el = document.getElementById('ob-model-status');
  btnBusy(btn, t('setup.mb_running'));
  let mb = null, mbErr = null;
  try {
    mb = await api('/api/setup/minibench', 'POST',
                   {vram_gb: state.setupData?.vram_gb ?? null, gpu_name: state.setupData?.gpu_name ?? null});
  } catch (e) { mbErr = _errMsg(e); }
  if (el) el.innerHTML = renderMinibench(mb, mbErr);
  btnDone(btn);
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
export function togglePitcherWrap(el) { document.getElementById('s-pitcher-wrap').hidden = !el.checked; renderAiPlan(); }

// A select in the advanced section changed: the plan above follows it.
export function setupModelChanged() { renderAiPlan(); }
