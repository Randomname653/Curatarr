// ── SETUP WIZARD ──────────────────────────────────────────────────────────────
import { state } from './state.js';
import { refreshSpotifyPending, spotifyDropZone } from './spotify_import.js';
import { SETUP_STEPS, SVG_WARN, SVG_X, _errMsg, esc } from './ui.js';
import { api } from './api.js';
import { loadLibraryConfig } from './libraries.js';
import { loadHistoryStatus } from './history.js';
const SETUP_CONTENT = {
  plex: () => `
    <h2 style="font-size:15px;font-weight:600;margin-bottom:16px;color:var(--amber)">Plex Connection</h2>
    <div class="form-group">
      <label for="s-plex-url">Plex Server URL</label>
      <input id="s-plex-url" placeholder="http://192.168.1.100:32400" value="${state.setupData.plex_url||''}">
      <div class="hint">The local IP address of your Plex server.</div>
    </div>
    <div class="form-group">
      <label for="s-plex-token">Plex Auth Token</label>
      <input id="s-plex-token" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${state.setupData.plex_token||''}">
      <div class="hint">Settings → Troubleshooting → "Show XML" → copy the X-Plex-Token from the URL.</div>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="testConn('plex')">Test connection</button>
    <div id="test-plex-result"></div>`,

  ollama: () => `
    <h2 style="font-size:15px;font-weight:600;margin-bottom:16px;color:var(--amber)">Ollama — Local AI</h2>
    <div class="form-group">
      <label for="s-ollama">Ollama Endpoint</label>
      <input id="s-ollama" placeholder="http://localhost:11434" value="${state.setupData.ollama_endpoint||'http://localhost:11434'}">
    </div>
    <button class="btn btn-secondary btn-sm" onclick="testConn('ollama')">Detect models</button>
    <div id="test-ollama-result" style="margin:8px 0"></div>
    <div class="form-group" style="margin-top:12px">
      <label for="s-vram">GPU / VRAM</label>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-secondary btn-sm" onclick="detectGpu()">Detect GPU (nvidia-smi)</button>
        <select id="s-vram" style="width:auto" onchange="refreshModelRecs()">
          <option value="">VRAM: pick manually…</option>
          <option value="8">8 GB</option>
          <option value="12">12 GB</option>
          <option value="16">16 GB</option>
          <option value="24">24 GB</option>
          <option value="32">32+ GB</option>
        </select>
      </div>
      <div id="gpu-result" style="margin-top:6px"></div>
      <div class="hint">Detection runs on the Curatarr host. If Ollama runs on a different machine, pick that machine's VRAM manually — the model recommendations below follow it.</div>
    </div>
    <div id="model-recs" style="margin:8px 0"></div>
    <div class="form-group" style="margin-top:12px">
      <label for="s-curator-model">Curator model (large — for chat & recommendations)</label>
      <select id="s-curator-model"><option value="gemma4:31b">gemma4:31b (recommended — see docs/BENCHMARKS.md)</option></select>
      <div class="hint">This model will be baked with Curatarr's system prompt via <code>ollama create</code>.</div>
    </div>
    <div class="form-group">
      <label for="s-summarizer-model">Summarizer model (small — for metadata processing)</label>
      <select id="s-summarizer-model"><option value="granite4.1:8b">granite4.1:8b (recommended)</option></select>
      <div class="hint">Fast model used to structure metadata. Runs thousands of times during enrichment.</div>
    </div>
    <div class="form-group">
      <label for="s-embed-model">Embedding model</label>
      <select id="s-embed-model"><option value="nomic-embed-text-v2-moe">nomic-embed-text-v2-moe (recommended)</option></select>
    </div>
    <div class="form-group" style="margin-top:12px">
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="s-pitcher-enable" onchange="document.getElementById('s-pitcher-wrap').style.display=this.checked?'':'none'" ${state.setupData.enable_pitcher?'checked':''}>
        Dedicated deletion judge (two-bake split)
      </label>
      <div class="hint">A second bake that ONLY judges deletions — benchmarked more precise and 2.4× faster at pitches than the chat curator. It needs its own VRAM while it runs (the curator is evicted meanwhile), so it pays off on 24 GB cards; below that the curator judges deletions too.</div>
      <div id="s-pitcher-wrap" style="margin-top:8px;${state.setupData.enable_pitcher?'':'display:none'}">
        <label for="s-pitcher-model">Judge model</label>
        <select id="s-pitcher-model"><option value="qwen3.8:27b">qwen3.8:27b (recommended for the split)</option></select>
        <div id="pitcher-note" class="hint"></div>
      </div>
    </div>`,

  metadata: () => `
    <h2 style="font-size:15px;font-weight:600;margin-bottom:16px;color:var(--amber)">Metadata APIs</h2>
    <p style="font-size:12px;color:var(--text2);margin-bottom:16px">These are optional but significantly improve recommendation quality. Skip any you don't need.</p>
    <div class="form-group">
      <label for="s-tmdb">TMDB API Key <span class="badge muted" style="font-size:10px">Movies &amp; Series</span></label>
      <input id="s-tmdb" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.tmdb_api_key||''}">
      <div class="hint">Free at <a href="https://www.themoviedb.org/settings/api" style="color:var(--amber)" target="_blank">themoviedb.org/settings/api</a>. Needed for movie and TV metadata, cast, themes.</div>
    </div>
    <div class="form-group">
      <label for="s-omdb">OMDb API Key <span class="badge muted" style="font-size:10px">Movies &amp; Series</span></label>
      <input id="s-omdb" placeholder="xxxxxxxx" value="${state.setupData.omdb_api_key||''}">
      <div class="hint">Free at <a href="https://www.omdbapi.com/apikey.aspx" style="color:var(--amber)" target="_blank">omdbapi.com</a> (1,000 req/day). Adds Rotten Tomatoes / Metacritic scores, awards, richer plots.</div>
    </div>
    <div class="form-group">
      <label for="s-lastfm">Last.fm API Key <span class="badge muted" style="font-size:10px">Music only</span></label>
      <input id="s-lastfm" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.lastfm_api_key||''}">
      <div class="hint">Free at last.fm/api. Adds music tags, similar artists, and bio. MusicBrainz works without a key.</div>
      <div class="used-for">Skip if you don't use Curatarr for music.</div>
    </div>
    <div class="form-group">
      <label for="s-spotify-id">Spotify Client ID <span class="badge muted" style="font-size:10px">Music only</span></label>
      <input id="s-spotify-id" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.spotify_client_id||''}">
      <div class="hint">Free at <a href="https://developer.spotify.com/dashboard" style="color:var(--amber)" target="_blank">developer.spotify.com</a> → Create App. Enables genre enrichment via Client Credentials — no user login needed.</div>
      <div class="used-for">Skip if you don't use Curatarr for music.</div>
    </div>
    <div class="form-group">
      <label for="s-spotify-secret">Spotify Client Secret</label>
      <input id="s-spotify-secret" type="password" placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" value="${state.setupData.spotify_client_secret||''}">
      <button class="btn btn-secondary btn-sm" style="margin-top:6px" onclick="testConn('spotify')">Test Spotify</button>
      <div id="test-spotify-result" style="margin-top:4px;font-size:12px"></div>
    </div>
    <div class="form-group">
      <label for="s-soulsync-url">SoulSync URL <span class="badge muted" style="font-size:10px">Optional · Music</span></label>
      <input id="s-soulsync-url" placeholder="http://192.168.1.100:12279" value="${state.setupData.soulsync_url||''}">
      <div class="hint">A SoulSync instance on your LAN adds a second music-metadata opinion. Read-only — Curatarr never triggers its downloads. Skip if you don't run one.</div>
    </div>
    <div class="form-group">
      <label for="s-soulsync-key">SoulSync API Key</label>
      <input id="s-soulsync-key" type="password" placeholder="xxxxxxxxxxxxxxxxxxxx" value="${state.setupData.soulsync_api_key||''}">
    </div>
    <div class="form-group">
      <label for="s-listenbrainz">ListenBrainz user token <span class="badge muted" style="font-size:10px">Optional · Music</span></label>
      <input id="s-listenbrainz" type="password" placeholder="free account state.token" value="${state.setupData.listenbrainz_token||''}">
      <div class="hint">Global listener counts as deletion evidence for music. Free account at <a href="https://listenbrainz.org/settings/" style="color:var(--amber)" target="_blank">listenbrainz.org/settings</a> — ListenBrainz requires the state.token since it auth-locked its API.</div>
    </div>`,

  arr: () => `
    <h2 style="font-size:15px;font-weight:600;margin-bottom:16px;color:var(--amber)">*arr Services</h2>
    <p style="font-size:12px;color:var(--text2);margin-bottom:16px">Skip services you don't use. These enable library management and deletion proposals.</p>
    ${['Radarr:Movies:7878','Sonarr:TV & Anime:8989','Lidarr:Music:8686'].map(s=>{
      const [name,what,port] = s.split(':');
      const id = name.toLowerCase();
      return `<div style="border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
          <strong style="font-size:13px">${name}</strong>
          <span class="badge muted" style="font-size:10px">${what}</span>
        </div>
        <div class="form-group" style="margin-bottom:8px">
          <label for="s-${id}-url">URL</label>
          <input id="s-${id}-url" placeholder="http://192.168.1.100:${port}" value="${state.setupData[id+'_url']||''}">
        </div>
        <div class="form-group" style="margin-bottom:8px">
          <label for="s-${id}-key">API Key</label>
          <input id="s-${id}-key" type="password" placeholder="Settings → General → Security" value="${state.setupData[id+'_api_key']||''}">
        </div>
        <button class="btn btn-secondary btn-sm" onclick="testConn('${id}')">Test ${name}</button>
        <div id="test-${id}-result"></div>
      </div>`;
    }).join('')}`,

  import: () => `
    <h2 style="font-size:15px;font-weight:600;margin-bottom:16px;color:var(--amber)">Listening history import</h2>
    <p style="font-size:12px;color:var(--text2);margin-bottom:16px">
      Optional. If you have a Spotify <strong>extended streaming history</strong>
      (privacy.spotify.com → Download your data → extended streaming history),
      drop it here — years of listening give the music curator a head start.
      The files wait until after the first sync; you then attach them to a user
      under <strong>Admin → Spotify history import</strong>.
    </p>
    ${spotifyDropZone('su-sp')}
    <p style="font-size:11px;color:var(--text3);margin-top:12px">
      Nothing yet? Skip this — you can always do it later from the Admin view.
    </p>`,

  done: () => `
    <div style="text-align:center;padding:20px 0">
      <h2 style="font-size:16px;font-weight:600;color:var(--amber);margin-bottom:8px">Ready to launch!</h2>
      <p style="font-size:13px;color:var(--text2);line-height:1.6;max-width:380px;margin:0 auto 20px">
        Curatarr will save your configuration, build the AI models with <code>ollama create</code>,
        then start the initial sync. This will take a few minutes the first time.
      </p>
      <button class="btn btn-primary" style="padding:12px 32px;font-size:14px" onclick="finishSetup()">
        Save & Start Curatarr
      </button>
      <div id="setup-saving" style="margin-top:16px;color:var(--text2);font-size:13px"></div>
    </div>`,
};

export function renderSetupStep(step) {
  state.setupStep = step;
  const key = SETUP_STEPS[step];
  document.getElementById('setup-body').innerHTML = SETUP_CONTENT[key]();
  document.getElementById('setup-step-label').textContent = `Step ${step+1} of ${SETUP_STEPS.length}`;
  document.getElementById('setup-back').style.visibility = step > 0 ? 'visible' : 'hidden';
  document.getElementById('setup-next').style.display = step === SETUP_STEPS.length-1 ? 'none' : 'inline-block';
  SETUP_STEPS.forEach((s,i) => {
    const dot = document.getElementById(`step-dot-${i}`);
    if (dot) dot.className = 'setup-step-dot' + (i===step?' active':i<step?' done':'');
  });

  // Load Ollama models if on ollama step
  if (key === 'ollama' && state.setupData.ollama_endpoint) {
    testConn('ollama');
  }
  if (key === 'import') refreshSpotifyPending('su-sp');
}

export function collectStep() {
  const key = SETUP_STEPS[state.setupStep];
  if (key === 'plex') {
    state.setupData.plex_url = document.getElementById('s-plex-url')?.value?.trim() || '';
    state.setupData.plex_token = document.getElementById('s-plex-token')?.value?.trim() || '';
  } else if (key === 'ollama') {
    state.setupData.ollama_endpoint = document.getElementById('s-ollama')?.value?.trim() || 'http://localhost:11434';
    state.setupData.base_curator_model = document.getElementById('s-curator-model')?.value || 'gemma4:31b';
    state.setupData.base_summarizer_model = document.getElementById('s-summarizer-model')?.value || 'granite4.1:8b';
    state.setupData.embedding_model = document.getElementById('s-embed-model')?.value || 'nomic-embed-text-v2-moe';
    state.setupData.enable_pitcher = !!document.getElementById('s-pitcher-enable')?.checked;
    state.setupData.base_pitcher_model = document.getElementById('s-pitcher-model')?.value || 'qwen3.8:27b';
    state.setupData.vram_gb = parseFloat(document.getElementById('s-vram')?.value) || state.setupData.vram_gb || null;
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
  if (resultEl) resultEl.innerHTML = '<div class="test-status">Testing…</div>';

  let url='', token_='', apiKey='';
  if (service==='plex') { url=document.getElementById('s-plex-url')?.value; token_=document.getElementById('s-plex-token')?.value; }
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
        ? `<div class="test-status ok">Spotify credentials valid</div>`
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
      ? `<div class="test-status" style="color:var(--amber);margin-top:4px">${SVG_WARN} ${esc(r.privacy_warning)}</div>`
  : '';
    if (resultEl) resultEl.innerHTML = (r.ok
      ? `<div class="test-status ok">Connected${r.server_name ? ' — '+esc(r.server_name) : r.version ? ' v'+esc(r.version) : ''}</div>`
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
  if (el) el.innerHTML = '<div class="test-status">Probing GPU…</div>';
  try {
    const r = await api('/api/setup/gpu');
    if (!r.ok) {
      if (el) el.innerHTML = `<div class="test-status err">${esc(r.error)}</div>`;
      return;
    }
    const names = r.gpus.map(g => `${g.name} (${g.vram_gb} GB)`).join(', ');
    if (el) el.innerHTML = `<div class="test-status ok">${esc(names)}</div>`;
    state.setupData.vram_gb = r.vram_gb;
    const sel = document.getElementById('s-vram');
    if (sel) {
      [...sel.options].filter(o => o.dataset.detected).forEach(o => o.remove());
      const opt = document.createElement('option');
      opt.value = r.vram_gb; opt.textContent = `${r.vram_gb} GB (detected)`;
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
                        {ollama_endpoint: endpoint, vram_gb: state.setupData.vram_gb ?? null});
    renderRecSelect('s-curator-model', r.curator, r.installed);
    renderRecSelect('s-summarizer-model', r.summarizer, r.installed);
    renderRecSelect('s-embed-model', r.embedding, r.installed);
    renderRecSelect('s-pitcher-model', r.pitcher, r.installed);
    const pn = document.getElementById('pitcher-note');
    if (pn) pn.textContent = r.pitcher_note || '';
    if (panel) panel.innerHTML = r.floor_note
      ? `<div class="test-status" style="color:var(--amber)">${SVG_WARN} ${esc(r.floor_note)}</div>`
      : '';
  } catch(e) {
    if (panel) panel.innerHTML = `<div class="test-status err">${esc(_errMsg(e))}</div>`;
  }
}

export function renderRecSelect(id, entries, installed) {
  const sel = document.getElementById(id);
  if (!sel || !entries) return;
  const cur = sel.value;
  const catalogNames = entries.map(e => e.model);
  const opts = entries.map(e => {
    const tags = [];
    if (e.installed) tags.push('installed — no download');
    if (e.fits === false) tags.push(`needs ~${Math.round(e.vram_gb + 3)} GB`);
    else if (e.tight) tags.push('tight fit');
    const suffix = tags.length ? ` · ${tags.join(' · ')}` : '';
    const unfit = e.fits === false ? 'data-unfit="1"' : '';
    return `<option value="${esc(e.model)}" ${unfit}>${esc(e.model)} — ${esc(e.label)}${esc(suffix)}</option>`;
  });
  // Everything else on the server stays reachable — labeled honestly.
  const extra = (installed || []).filter(m => !catalogNames.includes(m))
    .map(m => `<option value="${esc(m)}">${esc(m)} — untested (installed on server)</option>`);
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
  renderOnboardingStep(step);
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
    title.textContent = 'Step 1 — Configure your Plex libraries';
    sub.textContent = 'Tell Curatarr what each Plex library contains so it can handle them correctly.';

    const disc = await api('/api/libraries/discover').catch(()=>({sections:[]}));
    state.discoverSections = disc.sections || [];

    body.innerHTML = `
      <div class="tbl-wrap" style="margin-bottom:20px">
        <table class="tbl"><thead><tr><th>Library</th><th>Plex type</th><th>Category</th></tr></thead><tbody>
        ${state.discoverSections.map(s=>`<tr>
          <td style="font-weight:500">${esc(s.title)}</td>
          <td style="color:var(--text3);font-size:12px">${esc(s.type)}</td>
          <td><select id="ob-libcat-${s.key}" aria-label="Select category for library" style="background:var(--bg3);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:var(--radius);font-size:12px">
            ${['music','movie','show','anime','ignore'].map(o=>`<option value="${o}" ${(s.suggested_category)===o?'selected':''}>${o}</option>`).join('')}
          </select></td>
        </tr>`).join('')}
        </tbody></table>
      </div>
      <button class="btn btn-primary" style="width:100%;padding:12px" onclick="saveOnboardingLibraries()">
        Save & Continue →
      </button>`;
  }

  else if (step === 'sync') {
    title.textContent = 'Step 2 — Initial sync';
    sub.textContent = 'Curatarr will now pull your complete watch history from Plex and compute your taste profile. This takes a moment.';
    body.innerHTML = `
      <div id="ob-sync-status" style="margin-bottom:20px">
        <div class="progress-bar" style="margin-bottom:8px"><div class="progress-fill" id="ob-sync-bar" style="width:0%"></div></div>
        <div style="font-size:12px;color:var(--text2)" id="ob-sync-label">Waiting to start…</div>
      </div>
      <button class="btn btn-primary" style="width:100%;padding:12px" id="ob-sync-btn" onclick="startOnboardingSync()">
        Start sync
      </button>`;
  }

  else if (step === 'models') {
    title.textContent = 'Step 3 — Build AI models';
    sub.textContent = 'Curatarr will bake its personality into the Ollama models. This only happens once.';
    body.innerHTML = `
      <p style="font-size:13px;color:var(--text2);margin-bottom:16px">
        This creates <code>curatarr-curator</code> and <code>curatarr-summarizer</code>
        with Curatarr's system prompt baked in.
      </p>
      <div id="ob-model-status" style="margin-bottom:20px"></div>
      <button class="btn btn-primary" style="width:100%;padding:12px" onclick="buildOnboardingModels()">
        Build models
      </button>
      <button class="btn btn-secondary" style="width:100%;padding:10px;margin-top:8px" onclick="hideOnboarding()">
        Skip for now (use base models)
      </button>`;
  }

  else if (step === 'done') {
    title.textContent = 'All set!';
    sub.textContent = 'Curatarr is ready. Your taste profile has been computed.';
    body.innerHTML = `
      <div style="text-align:center;padding:20px 0">
        <p style="font-size:13px;color:var(--text2);line-height:1.7;margin-bottom:24px">
          Watch history synced and taste vectors computed.<br>
          You can now chat with your curator, get recommendations, and manage your library.
        </p>
        <button class="btn btn-primary" style="padding:12px 32px;font-size:14px" onclick="hideOnboarding()">
          Open Curatarr
        </button>
      </div>`;
  }
}

export async function saveOnboardingLibraries() {
  const libraries = state.discoverSections.map(s=>({
    key: s.key, title: s.title, plex_type: s.type,
    category: document.getElementById(`ob-libcat-${s.key}`)?.value || s.suggested_category,
  }));
  await api('/api/libraries/configure','POST',{libraries});
  state.libraryCfg = libraries;
  renderOnboardingStep('sync');
  onboardingStep = 'sync';
}

let _onboardingPoll = null;
export function _stopOnboardingPoll() {
  if (_onboardingPoll) { clearInterval(_onboardingPoll); _onboardingPoll = null; }
}

export async function startOnboardingSync() {
  const btn = document.getElementById('ob-sync-btn');
  const lbl = document.getElementById('ob-sync-label');
  const bar = document.getElementById('ob-sync-bar');
  if (btn) { btn.disabled = true; btn.textContent = 'Syncing…'; }

  // If a previous attempt was running, kill it before we start the new one.
  _stopOnboardingPoll();

  try {
    await api('/api/history/sync?force=true','POST');
    lbl.textContent = 'Fetching your library…';

    // Sync should finish well within 30 minutes — past that, give up so we
    // don't keep hitting /api/history/status forever if onboarding is left
    // open in a stale tab.
    const deadline = Date.now() + 30 * 60 * 1000;

    _onboardingPoll = setInterval(async () => {
      // If onboarding moved on or the user navigated, stop hitting the API.
      if (onboardingStep !== 'sync' || Date.now() > deadline) {
        _stopOnboardingPoll();
        return;
      }
      try {
        const s = await api('/api/history/status');
        const entries = s.watch_history_entries || 0;
        lbl.textContent = `${entries.toLocaleString()} entries synced…`;
        if (entries > 0) bar.style.width = '60%';
        if (s.taste_vector?.computed) {
          bar.style.width = '100%';
          lbl.textContent = `${entries.toLocaleString()} entries · taste profile computed`;
          _stopOnboardingPoll();
          setTimeout(() => {
            renderOnboardingStep('models');
            onboardingStep = 'models';
          }, 1500);
        }
      } catch {}
    }, 3000);
  } catch(e) {
    _stopOnboardingPoll();
    if (lbl) lbl.textContent = `Error: ${_errMsg(e)}`;
    if (btn) { btn.disabled = false; btn.textContent = 'Retry'; }
  }
}

export async function buildOnboardingModels() {
  const el = document.getElementById('ob-model-status');
  el.innerHTML = '<p class="loading" role="status" aria-live="polite">Building models… this may take a few minutes.</p>';

  try {
    const r = await api('/api/setup/build-models','POST');
    if (r.curator && r.summarizer) {
      const okLine = '<p style="color:var(--success)">curatarr-curator and curatarr-summarizer created!</p>';
      el.innerHTML = okLine + '<p class="loading" role="status" aria-live="polite">Warm-up check: loading the curator once to verify it runs on the GPU…</p>';
      // A model that doesn't quite fit VRAM runs silently part-on-CPU and
      // the whole app just feels broken-slow. Catch that HERE, not on the
      // user's first real conversation.
      let warm = null;
      try { warm = await api('/api/setup/warmup','POST',{model:'curatarr-curator'}); } catch {}
      let verdict = '';
      if (warm && warm.ok) {
        if (warm.verdict === 'cpu_spill')
          verdict = `<p style="color:var(--amber)">${SVG_WARN} ${warm.cpu_percent}% of the curator runs on CPU — it does not fit this GPU's VRAM and everything will feel slow. Consider a smaller curator model (re-run setup, or edit BASE_CURATOR_MODEL in .env and rebuild).</p>`;
        else if (warm.verdict === 'slow')
          verdict = `<p style="color:var(--amber)">${SVG_WARN} Generation is slow (${warm.tokens_per_s} tok/s) despite full GPU residency.</p>`;
        else
          verdict = `<p style="color:var(--success)">Fully on GPU${warm.tokens_per_s ? ` · ${warm.tokens_per_s} tok/s` : ''}${warm.load_s ? ` (one-time load ${warm.load_s}s)` : ''}</p>`;
      }
      el.innerHTML = okLine + verdict;
      setTimeout(() => { renderOnboardingStep('done'); onboardingStep = 'done'; },
                 warm && warm.ok && warm.verdict !== 'ok' ? 7000 : 1500);
    } else {
      let msg = '';
      if (!r.curator) msg += `${SVG_X} curatarr-curator failed<br>`;
      if (!r.summarizer) msg += `${SVG_X} curatarr-summarizer failed<br>`;
      msg += '<br>Make sure base models are pulled in Ollama.';
      el.innerHTML = `<div style="color:var(--danger);font-size:13px">${msg}</div>`;
    }
  } catch(e) {
    el.innerHTML = `<div style="color:var(--danger)">${esc(_errMsg(e))}</div>`;
  }
}

export async function logout() {
  // Server first: bumping the user's state.token version revokes THIS state.token on
  // every device, not just the copy in this tab's localStorage.
  try { await api('/api/auth/logout', 'POST'); } catch {}
  state.token=''; localStorage.removeItem('curatarr_token'); location.reload();
}
