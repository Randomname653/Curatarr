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
import { api, authHeaders, setupCode } from './api.js';
import { findJellyfinServers, jellyfinConnectHtml, stopJellyfinConnect } from './jellyfin_connect.js';
import { t, th } from './i18n.js';

const DOT_KEYS = {plex: 'setup.v2.dot_server', ollama: 'setup.v2.dot_ai', metadata: 'setup.v2.dot_metadata',
                  arr: 'settings.arr_apps', import: 'setup.v2.dot_import', done: 'setup.v2.dot_done'};

function isJellyfin() { return state.setupData.media_server_type === 'jellyfin'; }

// Logos and program icons, fetched live through the server (none ships with
// Curatarr; the CSP allows no outside images): <img data-logo="tmdb"> or
// data-icon="zen.exe", filled after each render, hidden until they load.
const _pics = new Map();
function logoImg(key, lan = '') {
  return `<img class="wiz-logo" alt="" hidden data-logo="${esc(key)}"${lan ? ` data-lan="${esc(lan)}"` : ''}>`;
}
async function _picUrl(path) {
  if (_pics.has(path)) return _pics.get(path);
  const p = (async () => {
    try {
      const r = await fetch(path, {headers: authHeaders(path)});
      if (r.ok) return URL.createObjectURL(await r.blob());
    } catch (_) { /* below: forgotten, so the next render asks again */ }
    _pics.delete(path);
    return null;
  })();
  _pics.set(path, p);
  return p;
}
function hydratePics(root = document) {
  for (const img of root.querySelectorAll('img[data-logo], img[data-icon]')) {
    const path = img.dataset.logo
      ? `/api/setup/logo/${encodeURIComponent(img.dataset.logo)}${img.dataset.lan ? `?lan=${encodeURIComponent(img.dataset.lan)}` : ''}`
      : `/api/setup/program-icon?name=${encodeURIComponent(img.dataset.icon)}`;
    _picUrl(path).then(u => { if (u) { img.src = u; img.hidden = false; } });
  }
}

// The wordmark at the head of the wizard: the same inline logo the sign-in
// screen carries, cloned once.
function _brandHeader(id = 'setup-logo') {
  const slot = document.getElementById(id);
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
      ${logoImg(value)}<span class="wiz-choice-body"><b>${name}</b><span class="t3 fs-12">${sub}</span></span>
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
  // No code to type: the PIN is a strong one, which plex.tv/link does not take.
  const waiting = d._plex_code ? `
    <div class="wiz-pin mt-12" role="status" aria-live="polite">
      <div class="progress-bar indeterminate"><div class="progress-fill"></div></div>
      <p class="fs-13 t2 mt-12">${th('setup.v2.plex_waiting')}</p>
      <small class="t3" id="plex-connect-status"></small>
    </div>` : '';
  return `<p class="fs-13 t2 mb-12">${th('setup.v2.plex_lead')}</p>
    <button class="btn btn-primary wiz-big" id="plex-connect-btn" ${act('startPlexConnect', EL)}>${th('auth.view.plex')}</button>
    ${waiting}
    <details class="mt-16">
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
// Everything on one page, laid out rather than folded away (Don,
// 2026-11-14: "alles zu verstecken ist nicht die Lösung"): the two things
// detected, the plan with a model per role to pick from, two switches for
// sharing the card, the open programs, and the expert values in a card.
function aiStepHtml() {
  const d = state.setupData;
  const sel = (id, def, label, on, defText = def) => `<select class="field-sm wiz-model" id="${id}" aria-label="${label}" ${on}><option value="${def}">${defText}</option></select>`;
  const changed = actOn('change', 'setupModelChanged');
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.ai_lead')}</p>
    <div class="wiz-status">
      <div class="wiz-status-row"><span class="wiz-status-name">${logoImg('ollama')}Ollama</span>
        <span class="grow" id="test-ollama-result"><span class="test-status">${th('setup.testing')}</span></span>
        <button class="link-num fs-12" ${act('toggleOllamaEdit')}>${th('setup.v2.change')}</button></div>
      <div class="wiz-status-row" id="s-ollama-edit" hidden>
        <input class="grow" id="s-ollama" type="url" aria-label="${th('setup.ollama_endpoint')}" placeholder="http://localhost:11434" value="${esc(d.ollama_endpoint || 'http://localhost:11434')}">
        <button class="btn btn-secondary btn-sm" ${act('testConn', 'ollama')}>${th('setup.v2.check')}</button>
      </div>
      <div class="row" id="wiz-ollama-found"></div>
      <div class="wiz-status-row"><span class="wiz-status-name">${th('setup.v2.gpu')}</span>
        <span class="grow" id="gpu-result"><span class="test-status">${th('setup.probing_gpu')}</span></span>
        <select class="field-sm w-auto" id="s-vram" aria-label="${th('setup.v2.vram')}" ${actOn('change', 'refreshModelRecs')}>
          <option value="">${th('setup.v2.vram_auto')}</option>
          ${[4, 8, 12, 16, 24, 32].map(g => `<option value="${g}">${g === 32 ? '32+' : g} GB</option>`).join('')}
        </select></div>
    </div>
    <div class="wiz-plan mt-16" role="group" aria-labelledby="wiz-plan-head">
      <div class="fs-13 mb-8" id="wiz-plan-head">${th('setup.v2.plan')}</div>
      <div class="wiz-plan-row"><span class="t3">${th('setup.v2.role_curator')}</span><span class="row">${sel('s-curator-model', 'gemma4:31b', th('setup.curator_model'), actOn('change', 'onSetupCuratorChange'), `gemma4:31b (${th('setup.recommended_docs')})`)}<span class="wiz-have" data-have="s-curator-model"></span></span></div>
      <div class="wiz-plan-row"><span class="t3">${th('setup.v2.role_summarizer')}</span><span class="row">${sel('s-summarizer-model', 'granite4.1:8b', th('setup.summarizer_model'), changed)}<span class="wiz-have" data-have="s-summarizer-model"></span></span></div>
      <div class="wiz-plan-row"><span class="t3">${th('setup.v2.role_embed')}</span><span class="row">${sel('s-embed-model', 'nomic-embed-text-v2-moe', th('setup.embed_model'), changed)}<span class="wiz-have" data-have="s-embed-model"></span></span></div>
      <div class="hint mt-8" id="wiz-plan-note"></div>
      <div class="mt-8" id="model-recs"></div>
    </div>
    <div class="wiz-grid mt-12">
      <div class="wiz-card">
        <label class="row pointer"><input type="checkbox" class="switch" id="s-cpu-lane" ${d.llm_cpu_lane === false ? '' : 'checked'}><b class="grow">${th('setup.v2.lane_title')}</b></label>
        <p class="fs-12 t2 mt-6">${th('setup.v2.lane_hint')}</p>
        <label class="row fs-12 t3 mt-8" for="s-cpu-threads">${th('setup.cpu_threads')}
          <input class="field-sm w-auto" id="s-cpu-threads" type="number" min="1" max="64" value="${d.llm_cpu_threads || 6}"></label>
      </div>
      <div class="wiz-card">
        <label class="row pointer"><input type="checkbox" class="switch" id="s-pitcher-enable" ${actOn('change', 'togglePitcherWrap', EL)} ${d.enable_pitcher ? 'checked' : ''}><b class="grow">${th('setup.v2.judge_title')}</b></label>
        <p class="fs-12 t2 mt-6">${th('setup.v2.pitcher_hint')}</p>
        <div id="s-pitcher-wrap" class="row mt-8"${d.enable_pitcher ? '' : ' hidden'}>${sel('s-pitcher-model', 'qwen3.8:27b', th('setup.pitcher_model'), changed)}<span class="wiz-have" data-have="s-pitcher-model"></span></div>
        <div id="pitcher-note" class="hint"></div>
      </div>
    </div>
    <div class="wiz-card mt-12" id="wiz-programs">
      <div class="row"><b class="grow">${th('setup.v2.games_title')}</b><button class="link-num fs-12" ${act('loadSetupPrograms')}>${th('setup.v2.check_again')}</button></div>
      <p class="fs-12 t2 mt-4">${th('setup.v2.games_hint')}</p>
      <div class="wiz-programs mt-8" id="wiz-program-list"><span class="t3 fs-12">${th('setup.testing')}</span></div>
    </div>
    <div class="wiz-card mt-12">
      <b>${th('setup.v2.expert_title')}</b>
      <p class="fs-12 t2 mt-4" id="s-profile-note">${th('setup.v2.expert_hint')}</p>
      <div class="wiz-grid3 mt-8">
        <label class="fs-12 t3">${th('setup.profile')}
          <select class="field-sm" id="s-llm-profile">
            <option value="large"${d.llm_profile === 'small' ? '' : ' selected'}>${th('setup.profile_large')}</option>
            <option value="small"${d.llm_profile === 'small' ? ' selected' : ''}>${th('setup.profile_small')}</option>
          </select></label>
        <label class="fs-12 t3">${th('setup.num_ctx')}
          <input class="field-sm" id="s-num-ctx" type="number" min="0" max="131072" step="1024" value="${d.curator_num_ctx ?? 0}"></label>
        <label class="fs-12 t3">${th('setup.num_gpu')}
          <input class="field-sm" id="s-num-gpu" type="number" min="-1" max="999" value="${d.curator_num_gpu ?? 99}"></label>
      </div>
    </div>`;
}

// The address set does not answer (in a container localhost is the
// container itself): Ollama is looked for next to the media server, at the
// page's host and on the Docker host, and each one found is one click.
async function findOllama() {
  const el = document.getElementById('wiz-ollama-found');
  if (!el) return;
  el.innerHTML = `<span class="fs-12 t3">${th('setup.v2.ollama_looking')}</span>`;
  const d = state.setupData;
  const server = isJellyfin() ? d.jellyfin_url : ((d.plex_servers || []).find(x => x.server_id === d.plex_server_id)?.url || d.plex_url);
  let near = '';
  try { near = server ? new URL(server).hostname : ''; } catch (_) { /* not a URL: ask without it */ }
  let found = [];
  try { found = (await api(`/api/setup/ollama/find?near=${encodeURIComponent(near)}`)).found || []; }
  catch (_) { found = []; }
  el.innerHTML = found.length
    ? `<span class="fs-12 t2">${th('setup.v2.ollama_found_here')}</span> ` + found.map(f =>
        `<button class="btn btn-secondary btn-sm" ${act('useOllama', f.url)}>${esc(f.url)} · ${th('setup.v2.n_models', {n: f.models})}</button>`).join(' ')
    : `<span class="fs-12 t3">${th('setup.v2.ollama_none')}</span>`;
}

export function useOllama(url) {
  const input = document.getElementById('s-ollama');
  if (input) input.value = url;
  state.setupData.ollama_endpoint = url;
  document.getElementById('wiz-ollama-found').innerHTML = '';
  testConn('ollama');
}

export function toggleOllamaEdit() {
  const row = document.getElementById('s-ollama-edit');
  if (row) { row.hidden = !row.hidden; if (!row.hidden) document.getElementById('s-ollama')?.focus(); }
}

// Whether each picked model is installed, and what that means for the finish.
function renderAiPlan() {
  const installed = new Set(state.setupData._detected_models || []);
  const has = m => installed.has(m) || installed.has(m + ':latest');
  let missing = 0;
  for (const el of document.querySelectorAll('.wiz-have')) {
    const sel = document.getElementById(el.dataset.have);
    if (!sel || sel.closest('[hidden]')) { el.textContent = ''; continue; }
    const ok = has(sel.value);
    if (!ok) missing += 1;
    el.className = `wiz-have fs-12 ${ok ? 't-success' : 't3'}`;
    el.textContent = ok ? t('setup.v2.installed') : t('setup.v2.will_download');
  }
  const head = document.getElementById('wiz-plan-head');
  const d = state.setupData;
  const card = d.gpu_name ? `${d.gpu_name} · ${d.vram_gb} GB` : d.vram_gb ? `${d.vram_gb} GB` : '';
  if (head) head.textContent = card ? t('setup.v2.plan_for', {card}) : t('setup.v2.plan');
  const note = document.getElementById('wiz-plan-note');
  if (note) note.textContent = missing ? t('setup.v2.plan_download') : t('setup.v2.plan_ready');
}

// The open programs: closing the games is asked for; what is left open is
// remembered as no game when the setup finishes (untick a game).
export async function loadSetupPrograms() {
  const el = document.getElementById('wiz-program-list');
  if (!el) return;
  let names = [];
  try { names = (await api('/api/setup/processes')).processes || []; }
  catch (e) { el.innerHTML = `<span class="test-status err">${esc(_errMsg(e))}</span>`; return; }
  const games = new Set(state.setupData.games || []);
  el.innerHTML = names.length
    ? names.map(n => `<label class="chip pointer"><input type="checkbox" data-program="${esc(n)}" ${games.has(n) ? '' : 'checked'}><img class="wiz-icon" alt="" hidden data-icon="${esc(n)}"> ${esc(n)}</label>`).join('')
    : `<span class="t3 fs-12">${th('setup.v2.games_none')}</span>`;
  document.getElementById('wiz-programs').hidden = false;
  hydratePics(el);
}

// ── step 3: metadata ────────────────────────────────────────────────────────
function keyCard(o) {
  const d = state.setupData;
  const link = o.href ? ` <a class="t-amber" href="${o.href}" target="_blank" rel="noopener noreferrer">${o.hrefText}</a>` : '';
  return `<div class="wiz-card">
      <div class="row">${logoImg(o.logo)}<b class="grow">${o.name}</b><span class="badge badge-sm ${o.badgeCls || 'muted'}">${o.badge}</span></div>
      <p class="fs-12 t2 mt-4">${o.why}${link}</p>
      ${o.fields.map(f => `<input class="mt-8" id="${f.id}" ${f.type ? `type="${f.type}"` : ''} aria-label="${f.label}" placeholder="${f.ph}" value="${esc(d[f.field] || '')}">`).join('')}
      ${o.test ? `<div class="row mt-8"><button class="btn btn-secondary btn-sm" ${act('testConn', o.test)}>${th('setup.v2.test')}</button><span class="grow" id="test-${o.test}-result"></span></div>` : ''}
    </div>`;
}

function metadataStepHtml() {
  const opt = th('setup.v2.optional');
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.meta_lead')}</p>
    <div class="wiz-grid">
      ${keyCard({name: 'TMDB', logo: 'tmdb', badge: th('setup.v2.needed'), badgeCls: 'amber', why: th('setup.v2.tmdb_why'),
                 href: 'https://www.themoviedb.org/settings/api', hrefText: 'themoviedb.org/settings/api', test: 'tmdb',
                 fields: [{id: 's-tmdb', field: 'tmdb_api_key', label: th('setup.api_key_of', {service: 'TMDB'}), ph: th('setup.v2.tmdb_ph')}]})}
      ${keyCard({name: 'OMDb', logo: 'omdb', badge: th('setup.v2.recommended'), why: th('setup.v2.omdb_why'),
                 href: 'https://www.omdbapi.com/apikey.aspx', hrefText: 'omdbapi.com',
                 fields: [{id: 's-omdb', field: 'omdb_api_key', label: th('setup.api_key_of', {service: 'OMDb'}), ph: th('setup.v2.key_ph')}]})}
    </div>
    <h3 class="wiz-sub mt-20">${th('setup.v2.music_heading')}</h3>
    <p class="fs-12 t2 mb-12">${th('setup.v2.music_lead')}</p>
    <div class="wiz-grid">
      ${keyCard({name: 'Last.fm', logo: 'lastfm', badge: opt, why: th('setup.v2.lastfm_why'), href: 'https://www.last.fm/api/account/create', hrefText: 'last.fm/api', test: 'lastfm',
                 fields: [{id: 's-lastfm', field: 'lastfm_api_key', label: th('setup.api_key_of', {service: 'Last.fm'}), ph: th('setup.v2.key_ph')}]})}
      ${keyCard({name: 'Spotify', logo: 'spotify', badge: opt, why: th('setup.v2.spotify_why'), href: 'https://developer.spotify.com/dashboard', hrefText: 'developer.spotify.com', test: 'spotify',
                 fields: [{id: 's-spotify-id', field: 'spotify_client_id', label: th('setup.spotify_id'), ph: th('setup.spotify_id')},
                          {id: 's-spotify-secret', field: 'spotify_client_secret', type: 'password', label: th('setup.spotify_secret'), ph: th('setup.spotify_secret')}]})}
      ${keyCard({name: 'ListenBrainz', logo: 'listenbrainz', badge: opt, why: th('setup.v2.listenbrainz_why'), href: 'https://listenbrainz.org/settings/', hrefText: 'listenbrainz.org/settings',
                 fields: [{id: 's-listenbrainz', field: 'listenbrainz_token', type: 'password', label: th('setup.listenbrainz'), ph: th('setup.listenbrainz_ph')}]})}
    </div>`;
}

// ── step 4: arr apps ────────────────────────────────────────────────────────
function arrCard(name, whatKey, extraBadge, keyHint = 'setup.v2.arr_key_hint', port = 0) {
  const d = state.setupData;
  const id = name.toLowerCase();
  const url = d[id + '_url'] || '';
  const found = (d._arr_found || {})[id];
  const status = d._arr_found === undefined ? `<span class="fs-12 t3">${th('setup.v2.arr_looking')}</span>`
    : found ? `<span class="test-status ok">${th('setup.v2.arr_found')}</span>`
    : `<span class="fs-12 t3">${th('setup.v2.arr_not_found')}</span>`;
  const keyPage = url ? ` <a class="t-amber" href="${esc(url.replace(/\/$/, ''))}${id === 'soulsync' ? '' : '/settings/general'}" target="_blank" rel="noopener noreferrer">${th('setup.v2.arr_key_where', {app: name})}</a>` : '';
  return `<div class="wiz-card${found ? ' on' : ''}">
      <div class="row">${logoImg(id, found || '')}<b>${name}</b><span class="badge badge-sm muted">${th(whatKey)}</span>${extraBadge ? `<span class="badge badge-sm amber">${extraBadge}</span>` : ''}<span class="grow"></span>${status}</div>
      <div class="wiz-grid mt-8">
        <input id="s-${id}-url" type="url" aria-label="${th('setup.v2.address_of', {app: name})}" placeholder="http://192.168.1.100:${port}" value="${esc(url)}">
        <input id="s-${id}-key" type="password" aria-label="${th('setup.api_key_of', {service: name})}" placeholder="${th('setup.api_key')}" value="${esc(d[id + '_api_key'] || '')}">
      </div>
      <div class="row mt-8"><span class="fs-12 t2 grow">${th(keyHint)}${keyPage}</span>
        <button class="btn btn-secondary btn-sm" ${act('testConn', id)}>${th('setup.v2.test')}</button></div>
      <div id="test-${id}-result"></div>
    </div>`;
}

function arrStepHtml() {
  return `<p class="fs-13 t2 mb-8">${th('setup.v2.arr_lead', {server: isJellyfin() ? 'Jellyfin' : 'Plex'})}</p>
    <div class="banner warn mb-16"><span class="banner-icon">${SVG_WARN}</span><span class="banner-text">${th('setup.v2.arr_delete')}</span></div>
    <div class="wiz-stack">
      ${arrCard('Radarr', 'nav.movies', '', undefined, 7878)}
      ${arrCard('Sonarr', 'setup.arr_tv_anime', '', undefined, 8989)}
      ${arrCard('Lidarr', 'nav.music', th('setup.v2.lidarr_badge'), undefined, 8686)}
      ${arrCard('SoulSync', 'nav.music', '', 'setup.v2.soulsync_key_hint', 8008)}
    </div>`;
}

// The arr apps' addresses, found next to the media server: filled into the
// fields still empty, the key pages linked.
async function findArrApps() {
  const d = state.setupData;
  if (d._arr_found !== undefined) return;
  const server = isJellyfin() ? d.jellyfin_url : ((d.plex_servers || []).find(s => s.server_id === d.plex_server_id)?.url || d.plex_url);
  let near = '';
  try { near = server ? new URL(server).hostname : ''; } catch (_) { /* not a URL: ask without it */ }
  let found = {};
  try { found = (await api(`/api/setup/arr/find?near=${encodeURIComponent(near)}`)).found || {}; }
  catch (_) { found = {}; }
  collectStep();
  for (const [app, url] of Object.entries(found)) if (!d[app + '_url']) d[app + '_url'] = url;
  d._arr_found = found;
  if (SETUP_STEPS[state.setupStep] === 'arr') renderSetupStep(state.setupStep);
}

// ── step 5: viewing and listening history ───────────────────────────────────
function importStepHtml() {
  const how = `<a class="t-amber" href="https://github.com/Randomname653/Curatarr/blob/main/docs/USAGE.md#bringing-your-history" target="_blank" rel="noopener noreferrer">${th('setup.v2.import_how')}</a>`;
  return `<p class="fs-13 t2 mb-16">${th('setup.v2.import_lead')} ${how}</p>
    <div class="wiz-grid">
      <div class="wiz-card"><div class="row mb-8">${logoImg('spotify')}<b class="grow">Spotify</b><span class="badge badge-sm muted">${th('setup.v2.optional')}</span></div>${spotifyDropZone('su-sp', true)}</div>
      <div class="wiz-card"><div class="row mb-8">${logoImg('netflix')}<b class="grow">Netflix</b><span class="badge badge-sm muted">${th('setup.v2.optional')}</span></div>${netflixDropZone(true)}</div>
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

// The setup code, asked first when this browser is not on the Curatarr
// machine (a container always): the first click used to run into a 401
// before anything asked for it (2026-11-14, Docker walkthrough).
function codeNeeded() {
  return !!state.setupStatus?.setup_code_needed && !state.setupData._code_ok;
}

function codeStepHtml() {
  return `<h2 class="setup-heading">${th('setup.v2.code_heading')}</h2>
    <p class="fs-13 t2 mb-12">${th('setup.v2.code_lead')}</p>
    <div class="wiz-card">
      <p class="fs-12 t2">${esc(state.setupStatus?.setup_code_hint || '')}</p>
      <div class="row mt-8">
        <input class="grow wiz-code" id="s-setup-code" autocomplete="off" spellcheck="false" maxlength="9"
               aria-label="${th('api.setup_code')}" placeholder="ABCD-1234" value="${esc(setupCode())}"
               ${actOn('keydown', 'setupCodeKey', '$event')}>
        <button class="btn btn-primary" id="s-setup-code-btn" ${act('submitSetupCode', EL)}>${th('setup.v2.code_go')}</button>
      </div>
      <div id="s-setup-code-result" role="status" aria-live="polite"></div>
    </div>`;
}

export async function submitSetupCode(btn) {
  const input = document.getElementById('s-setup-code');
  const code = (input?.value || '').trim().toUpperCase();
  const out = document.getElementById('s-setup-code-result');
  if (!code) { input?.focus(); return; }
  try { sessionStorage.setItem('curatarr_setup_code', code); } catch (_) { /* this tab only */ }
  btnBusy(btn, t('setup.testing'));
  try {
    await api('/api/setup/code', 'GET', null, true);
    state.setupData._code_ok = true;
    renderSetupStep(state.setupStep);
  } catch (e) {
    btnDone(btn);
    if (out) out.innerHTML = `<div class="test-status err">${e.status === 401 ? th('setup.v2.code_wrong') : esc(_errMsg(e))}</div>`;
    input?.select();
  }
}

export function setupCodeKey(ev) {
  if (ev.key === 'Enter') submitSetupCode(document.getElementById('s-setup-code-btn'));
}

const SETUP_CONTENT = {
  plex: () => codeNeeded() ? codeStepHtml() : `<h2 class="setup-heading">${th('setup.v2.server_heading')}</h2>${serverChoice()}<div class="mt-16">${isJellyfin()
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
    // Detection runs by itself: the endpoint's models, this machine's card,
    // the programs open now.
    testConn('ollama');
    detectGpu();
    loadSetupPrograms();
  }
  if (key === 'arr') findArrApps();
  if (key === 'import') { refreshSpotifyPending('su-sp'); refreshNetflixPending(); }
  if (key === 'plex' && isJellyfin() && !state.setupData.jellyfin_ticket) findJellyfinServers();
  hydratePics(document.getElementById('setup-body'));
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
    const progs = [...document.querySelectorAll('[data-program]')];
    if (progs.length) {
      d.not_games = progs.filter(c => c.checked).map(c => c.dataset.program);
      d.games = progs.filter(c => !c.checked).map(c => c.dataset.program);
    }
  } else if (key === 'metadata') {
    for (const [id, field] of [['s-listenbrainz', 'listenbrainz_token'], ['s-tmdb', 'tmdb_api_key'],
                               ['s-omdb', 'omdb_api_key'], ['s-soulsync-url', 'soulsync_url'],
                               ['s-soulsync-key', 'soulsync_api_key'], ['s-lastfm', 'lastfm_api_key'],
                               ['s-spotify-id', 'spotify_client_id'], ['s-spotify-secret', 'spotify_client_secret']]) {
      if (document.getElementById(id)) d[field] = val(id);
    }
  } else if (key === 'arr') {
    for (const a of ['radarr', 'sonarr', 'lidarr', 'soulsync']) {
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
  if (codeNeeded()) { submitSetupCode(document.getElementById('s-setup-code-btn')); return; }
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
  const v = id => document.getElementById(id)?.value?.trim();
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
      if (!r.ok) {
        document.getElementById('s-ollama-edit')?.removeAttribute('hidden');
        findOllama();
      }
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
// The protecting pillar in the user's words; a value the dictionary does
// not know (NONE, a future pillar) shows as it is, not as a key.
function _pillarName(p) {
  const key = `setup.v2.pillar.${String(p || '').toLowerCase()}`;
  return t(key) === key ? esc(p || '') : th(key);
}

export function renderHardwarePackage(pkg) {
  if (!pkg) return '';
  const r = pkg.recommended;
  if (!r) return pkg.note ? `<div class="t3 fs-12">${esc(pkg.note)}</div>` : '';
  const ex = r.expected || {};
  // An offloaded package keeps most of its model in system RAM; when this
  // host has too little, the server already picked the alternative that fits.
  const rf = pkg.ram_fallback;
  const ramNote = !rf ? ''
    : rf.instead_of
      ? `<div class="t-amber fs-12">${SVG_WARN} ${th('setup.ram_instead', {model: rf.instead_of, need: rf.needs_gb, host: rf.host_gb, rec: r.model})}</div>`
      : `<div class="t-amber fs-12">${SVG_WARN} ${th('setup.ram_short', {need: rf.needs_gb, host: rf.host_gb})}</div>`;
  // One sentence instead of tok/s and .env lines. The KV cache type is the
  // Ollama server's own setting: named only when it is not Ollama's default.
  const kv = (r.server || {}).OLLAMA_KV_CACHE_TYPE;
  const kvNote = kv && kv !== 'f16' ? `<div class="fs-12 t2 mt-4">${th('setup.v2.bench_kv', {v: esc(kv)})}</div>` : '';
  const sample = pkg.example;
  const example = !sample ? '' : `<details class="fs-12 mt-4"><summary class="pointer t2">${th('setup.example_summary', {model: r.model})}</summary>`
    + `<div class="t3">${esc(sample.title)} · ${th('setup.owner')} ${esc(sample.owner)}. ${esc(sample.setup)}</div>`
    + `<div><b>${esc(sample.verdict_words || sample.verdict)}</b> (${_pillarName(sample.protecting_pillar)}): ${esc(sample.reason)}</div>`
    + (sample.note ? `<blockquote class="t2">${esc(sample.note)}</blockquote>` : '')
    + `<div class="t3">${th('setup.all_examples')}</div></details>`;
  return ramNote + `<div class="fs-12 t2">${th('setup.v2.bench_line', {gb: pkg.tier_gb, model: esc(r.model), s: ex.judge_s ?? '?'})}</div>` + kvNote + example;
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
  // The wizard's plan rows are narrow: the model's name, the rest on hover.
  if (sel.classList.contains('wiz-model')) {
    for (const o of sel.options) { o.title = o.textContent; o.textContent = o.value; }
  }
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
  _brandHeader('ob-logo');
  overlay.classList.remove('hidden');
  // a dialog: focus goes in once the step has rendered (app.js traps Tab there)
  renderOnboardingStep(step).then(() => {
    focusDialog(overlay);
    // The first select used to take the focus, its ring read as "selected".
    overlay.querySelector('.btn-primary')?.focus();
  });
}

// Out of the onboarding into the app: the whole of showApp(), which the
// onboarding had returned early from. Only the libraries and the history
// were loaded here, so the task stream never opened (Activity stuck on
// "Connecting…"), nor the chat history, the starters or the process
// monitor, until the next page load (Docker walkthrough, 2026-11-14).
export async function hideOnboarding() {
  document.getElementById('onboarding-overlay').classList.add('hidden');
  const { showApp } = await import('./auth.js');
  await showApp();
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
    // A sync has no knowable end: the bar sweeps like a running task in
    // Activity, the mark turns like the curator thinking, the count is live.
    body.innerHTML = `
      <div class="ob-sync" id="ob-sync-status">
        <span class="ob-sync-mark" id="ob-sync-mark"></span>
        <div class="ob-sync-count" id="ob-sync-count">0</div>
        <div class="fs-12 t3">${th('setup.v2.ob_plays')}</div>
        <div class="progress-bar mt-16"><div class="progress-fill w-0" id="ob-sync-bar"></div></div>
        <div class="fs-12 t2 mt-8" id="ob-sync-label" role="status" aria-live="polite">${th('setup.ob_waiting')}</div>
      </div>
      <button hidden class="w-full p-12 btn btn-primary" id="ob-sync-btn" ${act('startOnboardingSync')}>
        ${th('setup.ob_start_sync')}
      </button>`;
    const mark = document.getElementById('brand-mark')?.content.firstElementChild;
    if (mark) {
      const m = mark.cloneNode(true);
      m.setAttribute('class', 'brand-logo');
      m.setAttribute('aria-hidden', 'true');
      m.removeAttribute('role');
      m.removeAttribute('aria-label');
      document.getElementById('ob-sync-mark').appendChild(m);
    }
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
      <button class="btn btn-primary wiz-big mt-16" ${act('hideOnboarding')}>${th('setup.ob_open')}</button>`;
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

// Where the sync is, in words and a share of the bar: the libraries are
// the first tenth, the play history (Plex names its total on the first
// page) up to 90 %, writing the rest.
function _syncStep(pr) {
  const n = v => (v || 0).toLocaleString(loc());
  if (pr.phase === 'library' && pr.total)
    return {pct: 2 + Math.round(8 * pr.done / pr.total), label: t('setup.v2.sync_library', {name: pr.label, n: pr.done + 1, total: pr.total})};
  if (pr.phase === 'history' && pr.total)
    return {pct: 10 + Math.round(80 * Math.min(1, pr.done / pr.total)), label: t('setup.v2.sync_history', {done: n(pr.done), total: n(pr.total)})};
  if (pr.phase === 'saving') return {pct: 93, label: t('setup.v2.sync_saving')};
  return {pct: 2, label: t('setup.fetching')};
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
    let readTotal = 0;
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
        const pr = s.progress || {};
        const step = _syncStep(pr);
        lbl.textContent = step.label;
        bar.style.width = `${step.pct}%`;
        const count = document.getElementById('ob-sync-count');
        // Saving: the plays read are all of Plex's total, not the rows written so far.
        if (pr.phase === 'history') readTotal = pr.total || readTotal;
        const shown = pr.phase === 'history' ? pr.done : pr.phase === 'saving' ? (readTotal || entries) : entries;
        if (count) count.textContent = (shown || 0).toLocaleString(loc());
        polls += 1;
        if (!s.running && polls > 1) {
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

// The sidebar's Sign out: this device only. It called logout() too, so it
// signed every phone and tablet out as well, the same as Settings' "Sign
// out everywhere" next to it (UI audit 2026-11-14).
export function logoutHere() {
  state.token = '';
  try { localStorage.removeItem('curatarr_token'); } catch (_) { /* private mode: the tab forgets it */ }
  location.reload();
}

// The Pitcher checkbox shows or hides the model picker beneath it.
export function togglePitcherWrap(el) { document.getElementById('s-pitcher-wrap').hidden = !el.checked; renderAiPlan(); }

// A select in the advanced section changed: the plan above follows it.
export function setupModelChanged() { renderAiPlan(); }
