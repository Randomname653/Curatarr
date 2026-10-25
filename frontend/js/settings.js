// ── ADMIN MAINTENANCE ACTIONS ─────────────────────────────────────────────────
import { EL, _errHtml, _errMsg, _showTestResult, act, actOn, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, haptic, loc, setStatus, toast, trackDirty } from './ui.js';
import { api } from './api.js';
import { loadHistoryStatus } from './history.js';
import { loadLibrarySettings } from './library_settings.js';
import { loadUsers } from './admin.js';
import { loadLibraryConfig } from './libraries.js';
import { showView } from './nav.js';
import { state } from './state.js';
import { loadUnreadMessages } from './notifications.js';
import { loadNodes } from './nodes.js';
export async function reattributeHistory(btn) {
  const status = document.getElementById('admin-action-status');
  btnBusy(btn, 'Re-attributing…');
  setStatus(status, 'Re-attributing…', 'busy');
  try {
    const r = await api('/api/history/admin/re-attribute', 'POST');
    if (r.error) { setStatus(status, r.error, 'err'); return; }

    // Build a calmer summary that explains what each bucket actually means.
    const lines = ['Done.'];
    if (r.updated > 0) {
      lines.push(`• ${r.updated.toLocaleString(loc())} row${r.updated === 1 ? '' : 's'} (re)assigned to a different Plex account.`);
    }
    if (r.unchanged > 0) {
      lines.push(`• ${r.unchanged.toLocaleString(loc())} row${r.unchanged === 1 ? '' : 's'} already correctly attributed (idempotent — no-op on repeat runs).`);
    }
    if (r.unattributable > 0) {
      // Frame this as expected behavior, not failure: Plex has limited history
      // retention, so older plays can't be cross-referenced and stay parked
      // on admin. That's fine for the "single primary user" case and lets the
      // multi-user code path own attribution for everything new from now on.
      lines.push(`• ${r.unattributable.toLocaleString(loc())} row${r.unattributable === 1 ? '' : 's'} kept on admin — older than Plex's history retention horizon, can't be matched. This is expected and harmless.`);
    }
    if (typeof r.plex_history_events !== 'undefined') {
      lines.push('');
      lines.push(`Plex returned ${r.plex_history_events.toLocaleString(loc())} play events covering ${r.plex_history_rating_keys.toLocaleString(loc())} unique items. Your library has ${(r.local_rks_in_plex_history + r.local_rks_missing_from_plex_history).toLocaleString(loc())} distinct watched items in total.`);
    }
    setStatus(status, lines.join(String.fromCharCode(10)), 'ok');
    toast('Re-attribution done', 'success');
  } catch (e) {
    setStatus(status, _errMsg(e), 'err');
  } finally {
    btnDone(btn);
  }
}

export async function cleanupOrphans(btn) {
  const res = await confirmDialog({
    title: 'Hide history for removed media', confirmLabel: 'Hide history',
    body: '<p>Hides watch history for titles that are no longer in any Plex library. The plays are kept for your Wrap-Up.</p><p class="t3 fs-12 mt-8">Spotify history is never hidden. Nothing is hidden if a library can\'t be read.</p>',
  });
  if (!res.ok) return;
  const status = document.getElementById('maint-cleanup-status');
  btnBusy(btn, 'Hiding…');
  setStatus(status, 'Hiding history for removed media…', 'busy');
  try {
    let r = await api('/api/history/admin/cleanup-orphans', 'POST');
    if (r.error && r.would_archive) {
      // The share guard: a mass "orphaning" is usually a reading failure, so
      // the owner confirms the number before anything goes.
      const again = await confirmDialog({
        title: `Hide ${r.would_archive} of ${r.examined} history entries?`,
        confirmLabel: 'Hide anyway',
        body: '<p>That is more than a fifth of all history. This usually means a library was not fully readable, not that the media is gone.</p><p class="t3 fs-12 mt-8">Only continue if you really removed that much from Plex.</p>',
      });
      if (!again.ok) { setStatus(status, r.error + ' (hidden: 0)', 'err'); return; }
      r = await api('/api/history/admin/cleanup-orphans?force=true', 'POST');
    }
    if (r.error) { setStatus(status, r.error + ' (hidden: 0)', 'err'); return; }
    const back = r.returned ? `, ${r.returned} back in Plex` : '';
    setStatus(status, `Done — hid ${r.archived} of ${r.examined} entries checked${back}`, 'ok');
    toast(`Hid ${r.archived} history entries for removed media`, 'success');
    if (r.archived > 0 || r.returned > 0) setTimeout(loadHistoryStatus, 800);
  } catch (e) {
    setStatus(status, _errMsg(e), 'err');
  } finally {
    btnDone(btn);
  }
}

// ── SETTINGS VIEW ─────────────────────────────────────────────────────────────

export function openSettingsPane(name, btn) {
  // Toggle sub-nav active state
  document.querySelectorAll('.settings-tab').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  else {
    const tab = document.querySelector(`.settings-tab[data-pane="${name}"]`);
    if (tab) tab.classList.add('active');
  }
  // Show only the matching pane
  document.querySelectorAll('.settings-pane').forEach(p => { p.hidden = p.dataset.pane !== name; });
  // The pane is part of the address (#settings/users): replaced, not pushed,
  // so Back leaves Settings instead of stepping through its tabs.
  if (document.getElementById('settings-view')?.classList.contains('active')) {
    history.replaceState(null, '', '#settings/' + name);
  }
  // Per-pane lazy loaders
  if (name === 'account') {
    populateSettingsAccount();
  } else if (name === 'notifications') {
    loadNotificationPreferences();
  } else if (name === 'plex-libraries') {
    loadLibraryConfig();
  } else if (name === 'library') {
    loadLibrarySettings();
  } else if (name === 'integrations') {
    loadIntegrations();
  } else if (name === 'users') {
    loadUsers();
  } else if (name === 'maintenance') {
    loadNodes();
    loadDepsStatus();
    loadModelUpdates();
  }
}

// Pinned requirements vs. the interpreter behind this server (admin only).
// The server never installs; the launchers do, before the first import.
export async function loadDepsStatus(btn) {
  const box = document.getElementById('maint-deps');
  if (!box) return;
  if (btn) btnBusy(btn, 'Checking…');
  try {
    const d = await api('/api/system/dependencies');
    const rows = [
      ...d.missing.map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td class="t-danger">not installed</td><td>${esc(m.pinned)}</td></tr>`),
      ...d.drift.map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.pinned)}</td></tr>`),
    ];
    const pinsHtml = d.clean
      ? `<div class="empty good">All ${d.pins} pinned packages match requirements.txt.</div>`
      : `<div class="banner warn"><span class="banner-icon">!</span><div class="banner-text">${rows.length} of ${d.pins} pinned packages differ. In the Curatarr folder, with this interpreter, run <code class="mono">${esc(d.command)}</code> and restart. start.bat and the tray launcher do this on their own at the next start.</div></div>
         <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th>Package</th><th>Installed</th><th>Pinned</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
         <p class="fs-12 t3 mt-8">Interpreter: <span class="mono">${esc(d.interpreter)}</span></p>`;
    // The tested install (lock/requirements.txt): what the pins pull in, at
    // the versions the battery ran with. The launchers reconcile at start.
    const L = d.lock || {};
    const lockRows = [
      ...(L.below || []).map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.locked)}</td><td>raised to the lock</td></tr>`),
      ...(L.above || []).map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.locked)}</td><td>lock follows</td></tr>`),
      ...(L.unlocked || []).map(n => `<tr><td class="cell-primary">${esc(n)}</td><td>installed</td><td>not listed</td><td>added to the lock</td></tr>`),
    ];
    const lockHtml = L.error
      ? `<p class="fs-12 t3 mt-8">Tested install: ${esc(L.error)}</p>`
      : L.clean
        ? `<p class="fs-12 t3 mt-8">Tested install: all ${L.packages} packages in lock/requirements.txt match this interpreter.</p>`
        : `<div class="banner warn mt-8"><span class="banner-icon">!</span><div class="banner-text">${lockRows.length} package(s) differ from the tested install (lock/requirements.txt). start.bat and the tray launcher reconcile this at the next start, never lowering anything; by hand: <code class="mono">${esc(L.command)}</code>.</div></div>
           <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th>Package</th><th>Installed</th><th>Lock</th><th>Next start</th></tr></thead><tbody>${lockRows.join('')}</tbody></table></div>`;
    box.innerHTML = pinsHtml + lockHtml;
  } catch (e) {
    box.innerHTML = `<p class="t-danger fs-12">Could not read the dependency report: ${esc(e.message || e)}</p>`;
  } finally {
    if (btn) btnDone(btn, 'Check again');
  }
}

// ── Settings → Integrations (admin only) ──────────────────────────────────
// GET /api/setup/integrations hands back plain values for URLs / models and
// {set: bool} for secrets; POST /api/setup/reconfigure takes only the fields
// the admin touched. Secrets are typed to replace, cleared via the × button,
// and never travel back to the browser.
const INTEGRATION_CARDS = [
  {key: 'plex', title: 'Plex', test: 'plex', fields: [
    {id: 'plex_url', label: 'Server URL', placeholder: 'http://192.168.1.100:32400'},
    {id: 'plex_token', label: 'Auth state.token', secret: true, required: true},
  ]},
  {key: 'models', title: 'Ollama & models', test: 'ollama', fields: [
    {id: 'ollama_endpoint', label: 'Ollama endpoint', placeholder: 'http://localhost:11434'},
    {id: 'base_curator_model', label: 'Curator model (chat & recommendations)'},
    {id: 'base_summarizer_model', label: 'Summarizer model (enrichment)'},
    {id: 'embedding_model', label: 'Embedding model (changing it means re-embedding the library)'},
    {id: 'enable_pitcher', label: 'Dedicated deletion judge (two-bake split)', toggle: true},
    {id: 'base_pitcher_model', label: 'Judge model'},
  ]},
  {key: 'gpu', title: 'Sharing the graphics card',
   hint: 'What happens while another program holds the card. Applies live, no restart.',
   fields: [
    {id: 'gpu_pressure_gate', label: 'Notice when another program holds the card (treat it like a running game)', toggle: true},
    {id: 'llm_cpu_lane', label: 'Keep the background work going on the processor instead of stopping it', toggle: true},
    {id: 'llm_cpu_threads', label: 'Processor threads for that work (six measured as fast as twelve)', number: true},
    {id: 'llm_cpu_min_free_mb', label: 'Free memory it needs before it starts, MB (one run measured 9.7 GB)', number: true},
  ]},
  {key: 'metadata', title: 'Movies & series metadata', fields: [
    {id: 'tmdb_api_key', label: 'TMDB API key', secret: true, test: 'tmdb'},
    {id: 'omdb_api_key', label: 'OMDb API key', secret: true},
    {id: 'mal_client_id', label: 'MyAnimeList client ID (anime scores, genres, search)', secret: true},
  ]},
  {key: 'music', title: 'Music metadata', fields: [
    {id: 'lastfm_api_key', label: 'Last.fm API key', secret: true, test: 'lastfm'},
    {id: 'spotify_client_id', label: 'Spotify client ID'},
    {id: 'spotify_client_secret', label: 'Spotify client secret', secret: true, test: 'spotify'},
    {id: 'listenbrainz_token', label: 'ListenBrainz user state.token', secret: true},
    {id: 'soulsync_url', label: 'SoulSync URL (LAN, read-only)', placeholder: 'http://192.168.1.100:12279'},
    {id: 'soulsync_api_key', label: 'SoulSync API key', secret: true},
  ]},
  {key: 'subtitles', title: 'Subtitles (dialogue evidence)', fields: [
    {id: 'opensubtitles_api_key', label: 'OpenSubtitles API key', secret: true},
    {id: 'opensubtitles_username', label: 'OpenSubtitles username'},
    {id: 'opensubtitles_password', label: 'OpenSubtitles password', secret: true},
    {id: 'opensubtitles_daily_budget', label: 'Daily request budget', number: true},
  ]},
];

let _integrationsCfg = null;

export async function loadIntegrations() {
  const shell = document.getElementById('integrations-shell');
  if (!shell) return;
  shell.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const r = await api('/api/setup/integrations');
    _integrationsCfg = r.config || {};
    if (r.pitcher_enabled !== undefined) _integrationsCfg.enable_pitcher = !!r.pitcher_enabled;
    shell.innerHTML = '';
    for (const card of INTEGRATION_CARDS) shell.appendChild(renderIntegrationCard(card, _integrationsCfg));
  } catch (e) {
    shell.innerHTML = _errHtml(e, act('loadIntegrations'));
  }
}

// One .section per integration: the fields, then Save (enabled once a field
// changed) · Test connection · Rebuild models, with the status beside them.
export function renderIntegrationCard(card, cfg) {
  const el = document.createElement('section');
  el.className = 'section';
  const rows = card.fields.map(f => {
    const cur = cfg[f.id];
    if (f.toggle) {
      return `<label class="pref-row" for="int-${f.id}"><span class="pref-text">${esc(f.label)}</span><input type="checkbox" class="switch" id="int-${f.id}" ${cur ? 'checked' : ''}></label>`;
    }
    if (f.secret) {
      const isSet = !!(cur && cur.set);
      const chip = isSet ? '<span class="badge success badge-sm">set</span>' : '<span class="badge muted badge-sm">not set</span>';
      const clear = (isSet && !f.required)
        ? `<button type="button" class="btn btn-secondary btn-sm" title="Clear this key" ${act('clearIntegrationSecret', f.id, card.key, EL)}>Clear</button>` : '';
      return `<div class="my-8 form-group">
        <label for="int-${f.id}">${esc(f.label)} ${chip}</label>
        <div class="row"><input id="int-${f.id}" class="grow" type="password" autocomplete="new-password" placeholder="${isSet ? 'type to replace' : 'not set'}">${clear}</div></div>`;
    }
    const type = f.number ? 'number' : 'text';
    return `<div class="my-8 form-group">
      <label for="int-${f.id}">${esc(f.label)}</label>
      <input id="int-${f.id}" type="${type}" value="${esc(cur === undefined || cur === null ? '' : String(cur))}" placeholder="${esc(f.placeholder || '')}"></div>`;
  }).join('');
  const testBtn = card.test
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('testIntegration', card.key, EL)}>Test connection</button>` : '';
  const rebuild = card.key === 'models'
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('rebuildModels', card.key, EL)} title="Bake the chosen models via ollama create">Rebuild models</button>` : '';
  el.innerHTML = `
    <div class="section-head"><h3>${esc(card.title)}</h3>${card.hint ? `<span class="section-hint">${esc(card.hint)}</span>` : ''}</div>
    <div class="section-body">
      <div id="int-form-${card.key}">${rows}</div>
      <div class="row mt-8">
        <button type="button" class="btn btn-primary btn-sm" id="int-save-${card.key}" ${act('saveIntegrations', card.key, EL)}>Save</button>
        ${testBtn}${rebuild}
        <span id="int-msg-${card.key}" class="status"></span>
      </div>
    </div>`;
  trackDirty(el.querySelector(`#int-form-${card.key}`), el.querySelector(`#int-save-${card.key}`));
  return el;
}

export function _integrationChanges(card) {
  const changes = {};
  for (const f of card.fields) {
    const input = document.getElementById(`int-${f.id}`);
    if (!input) continue;
    if (f.toggle) { changes[f.id] = !!input.checked; continue; }
    const v = input.value.trim();
    if (f.secret) { if (v) changes[f.id] = v; continue; }     // blank = keep
    if (f.number) { if (v !== '') changes[f.id] = parseInt(v, 10); continue; }
    const cur = _integrationsCfg?.[f.id];
    if (v !== String(cur === undefined || cur === null ? '' : cur)) changes[f.id] = v;
  }
  return changes;
}

export async function saveIntegrations(cardKey, btn) {
  const card = INTEGRATION_CARDS.find(c => c.key === cardKey);
  const msg = document.getElementById(`int-msg-${cardKey}`);
  const changes = _integrationChanges(card);
  if (!Object.keys(changes).length) { setStatus(msg, 'nothing changed', null); return; }
  btnBusy(btn, 'Saving…');
  setStatus(msg, 'saving…', 'busy');
  try {
    const r = await api('/api/setup/reconfigure', 'POST', changes);
    const note = r.models_changed
      ? 'saved — now Rebuild models, then restart Curatarr'
      : r.restart_recommended ? 'saved — restart Curatarr to apply everywhere' : 'saved + live';
    setStatus(msg, note, 'ok');
    toast(`${card.title}: ${note}`, 'success');
    setTimeout(loadIntegrations, 1200);
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}

export async function clearIntegrationSecret(fieldId, cardKey, btn) {
  const res = await confirmDialog({title: 'Clear this key', danger: true, confirmLabel: 'Clear',
    body: '<p>The integration falls back to keyless behaviour or is skipped until a new key is saved.</p>'});
  if (!res.ok) return;
  const msg = document.getElementById(`int-msg-${cardKey}`);
  btnBusy(btn);
  try {
    await api('/api/setup/reconfigure', 'POST', {[fieldId]: ''});
    setStatus(msg, 'cleared', 'ok');
    toast('Key cleared', 'success');
    setTimeout(loadIntegrations, 800);
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}

export async function testIntegration(cardKey, btn) {
  const msg = document.getElementById(`int-msg-${cardKey}`);
  const val = id => document.getElementById(`int-${id}`)?.value?.trim() || '';
  // Blank secrets fall back to the stored ones server-side, so "Test" works
  // without re-typing a key that is already saved.
  let body;
  if (cardKey === 'plex') body = {service: 'plex', url: val('plex_url'), token: val('plex_token')};
  else if (cardKey === 'models') body = {service: 'ollama', url: val('ollama_endpoint')};
  else if (cardKey === 'metadata') body = {service: 'tmdb', api_key: val('tmdb_api_key')};
  else if (cardKey === 'music') body = val('spotify_client_id') || val('spotify_client_secret')
    ? {service: 'spotify', client_id: val('spotify_client_id'), client_secret: val('spotify_client_secret')}
    : {service: 'lastfm', api_key: val('lastfm_api_key')};
  else return;
  btnBusy(btn, 'Testing…');
  setStatus(msg, 'testing…', 'busy');
  try {
    _showTestResult(msg, await api('/api/setup/test', 'POST', body));
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); }
  btnDone(btn);
}

export async function rebuildModels(cardKey, btn) {
  const msg = document.getElementById(`int-msg-${cardKey}`);
  const res = await confirmDialog({title: 'Rebuild the model bakes', confirmLabel: 'Rebuild',
    body: '<p>Pulls missing base models and re-creates the Curatarr bakes; the app keeps running. This can take minutes.</p>'});
  if (!res.ok) return;
  btnBusy(btn, 'Building…');
  setStatus(msg, 'building… this can take minutes', 'busy');
  try {
    const r = await api('/api/setup/build-models', 'POST');
    const ok = r.curator && r.summarizer;
    setStatus(msg, ok ? 'models rebuilt — restart Curatarr to load them' : 'build failed — see the Activity log', ok ? 'ok' : 'err');
    toast(ok ? 'Models rebuilt — restart Curatarr to load them' : 'Model build failed — see Activity', ok ? 'success' : 'danger', {ms: 8000});
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); }
  btnDone(btn);
}


export async function loadNotificationPreferences() {
  const host = document.getElementById('notif-prefs');
  if (!host) return;
  host.innerHTML = '<p class="loading" role="status" aria-live="polite">Loading…</p>';
  try {
    const r = await api('/api/users/me/notification-preferences');
    renderNotificationPreferences(r.triggers || []);
  } catch (e) {
    host.innerHTML = _errHtml(e, act('loadNotificationPreferences'));
  }
}

export async function _setNotifPref(toggle) {
  const wantEnabled = toggle.checked;
  toggle.disabled = true;
  haptic('light');
  try {
    await api('/api/users/me/notification-preferences', 'POST', {trigger_type: toggle.dataset.triggerType, enabled: wantEnabled});
    toast('Saved', 'success');
  } catch (e) {
    // Roll back the visual state if the server rejected the change.
    toggle.checked = !wantEnabled;
    toast(_errMsg(e), 'danger');
  } finally {
    toggle.disabled = false;
  }
}

// One row per trigger; the switch saves immediately (the only pane
// without a Save button — a toggle is its own commit).
export function renderNotificationPreferences(triggers) {
  const host = document.getElementById('notif-prefs');
  if (!host) return;
  if (!triggers.length) { host.innerHTML = emptyHtml('No triggers are configured on this server.'); return; }
  host.innerHTML = triggers.map(t => `<div class="panel-item">
    <div class="panel-item-head">
      <div class="grow"><div class="panel-item-title">${esc(t.label || t.type)}</div><div class="mt-2 panel-item-sub">${esc(t.description || '')}</div></div>
      <input type="checkbox" class="switch" ${t.enabled ? 'checked' : ''} aria-label="${escAttr(t.label || t.type)}" data-trigger-type="${escAttr(t.type)}" ${actOn('change', '_setNotifPref', EL)}>
    </div>
  </div>`).join('');
}

export function populateSettingsAccount() {
  const u = state.currentUser || {};
  const nameEl = document.getElementById('settings-plex-username');
  const roleEl = document.getElementById('settings-plex-role');
  const idEl   = document.getElementById('settings-plex-id');
  if (nameEl) nameEl.textContent = u.username || u.plex_username || '—';
  if (roleEl) roleEl.textContent = u.is_admin ? '· admin' : '· user';
  if (idEl)   idEl.textContent   = u.plex_user_id || u.id || '—';
}

// The user pill: the Settings view with its Account pane open. showView runs
// the pane's loaders once (it opens the pane itself).
export function showSettingsAccount() {
  showView('settings', null, {pane: 'account', focus: true});
}

// ── Settings → Maintenance → Model updates (admin only) ───────────────────
// Every curatarr-* bake runs the weights it was built from, so a pulled
// model is a pin. The server checks the registry daily; this section shows
// which models have new weights, runs the old-vs-new test on ten library
// titles, and lets the owner take the update or stay on the pin.
const MU_STATUS = {
  current: ['good', 'up to date'], update: ['warn', 'new weights available'],
  pinned: ['', 'staying on the pinned version'], unknown: ['', 'not on the Ollama registry'],
};
const _short = d => (d || '').replace('sha256:', '').slice(0, 12);

// The current model's blind review (services/model_update_eval.py).
const MU_REC = {update: ['ok', 'recommends the update'], stay: ['warn', 'recommends staying'],
                either: ['', 'sees no clear winner'],
                // The judge did not answer most pairs: no recommendation, not a draw.
                'no verdict': ['warn', 'gave no verdict']};
function _muEval(role, ev, facts_rebuilt, run, err, summary) {
  if (run?.role === role && run.kind === 'review')
    return `<div class="banner mt-8"><span class="banner-icon">…</span><div class="banner-text">The current model is reviewing — started ${esc(_muTime(run.since))}; this section updates by itself, progress in Activity.</div></div>`;
  if (!ev) return (err ? `<p class="t-danger fs-12 mt-8">The last review failed: ${esc(err)}</p>` : '')
    + `<div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${run ? 'disabled' : ''} ${act('evaluateModelUpdate', role, EL)}>${err ? 'Review again' : 'Let the current model review it'}</button></div>`;
  const [kind, label] = MU_REC[ev.recommendation] || ['', ev.recommendation];
  const t = ev.tally.titles, c = ev.tally.chat;
  // Reviews from before f3ea0a1 carry no vram_source: their VRAM may be /api/ps's.
  const stale = !summary?.vram_source;
  const rules = ev.guards.length
    ? `<p class="fs-12 mt-4">Hard rules: ${esc(ev.guards.join('; '))}${ev.overruled ? ' — these overrule the model’s “update”.' : '.'}</p>` : '';
  const per = ev.titles.map(r => `<li>${esc(r.key)}: <b>${esc(r.winner)}</b>${r.position_flip ? ' (flipped with the order → tie)' : ''} — ${esc(r.reasons[0] || '')}</li>`).join('');
  return `<div class="banner ${kind} mt-8"><span class="banner-icon">${ev.recommendation === 'no verdict' ? '!' : '✓'}</span><div class="banner-text">
      <b>${esc(ev.judge_model)}</b> ${esc(label)}: ${esc(ev.model_reason)}
      <p class="fs-12 mt-4">Titles: new better ${t.new}, old better ${t.old}, tie ${t.tie}${t.unreadable ? `, unreadable ${t.unreadable}` : ''} · chat: new ${c.new}, old ${c.old}, tie ${c.tie}${c.unreadable ? `, unreadable ${c.unreadable}` : ''}</p>${rules}
      ${(t.unreadable || c.unreadable) ? `<p class="fs-12 t-amber mt-4">${(t.unreadable || 0) + (c.unreadable || 0)} judgement(s) got no readable answer from the judge — those pairs count for neither side.</p>` : ''}
      <p class="fs-12 t3 mt-4">The current model judged blind, each pair in both orders; it is reviewing its own successor, so it may prefer its own style${facts_rebuilt ? '; facts were rebuilt after the test' : ''}.</p>
      <details class="mt-4"><summary class="fs-12">Reason per title</summary><ul class="fs-12">${per}</ul></details>
      ${stale ? `<p class="fs-12 t-amber mt-4">This review was made before VRAM was measured with nvidia-smi — its VRAM figure may be Ollama's misreport. Review again for clean numbers.</p>` : ''}
      <div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${run ? 'disabled' : ''} ${act('evaluateModelUpdate', role, EL)}>Review again (re-measures speed and VRAM)</button></div></div></div>`;
}

const _muTime = iso => iso ? new Date(iso).toLocaleTimeString(loc(), {hour: '2-digit', minute: '2-digit'}) : '';

function _muCompare(role, c, run, err) {
  const s = c.summary;
  const row = (label, k, unit = '') => `<tr><td class="cell-primary">${esc(label)}</td><td>${esc(String(s[k]?.old ?? '–'))}${unit}</td><td>${esc(String(s[k]?.new ?? '–'))}${unit}</td></tr>`;
  const changes = s.verdict_changes.length
    ? `<ul class="fs-12 mt-8">${s.verdict_changes.map(v => `<li>${esc(v.title)}: ${esc(v.old)} → ${esc(v.new)}</li>`).join('')}</ul>` : '';
  const pairs = c.pairs.map(p => `<div class="mt-8"><div class="fw-500 fs-12">${esc(p.title)} — ${esc(p.old_verdict || '?')} / ${esc(p.new_verdict || '?')}</div>
      <div class="fs-12 t3">old: ${esc(p.old || '—')}</div><div class="fs-12">new: ${esc(p.new || '—')}</div></div>`).join('');
  const chat = c.chat.map(p => `<div class="mt-8"><div class="fw-500 fs-12">${esc(p.prompt)}</div>
      <div class="fs-12 t3">old: ${esc(p.old || '—')}</div><div class="fs-12">new: ${esc(p.new || '—')}</div></div>`).join('');
  return _muEval(role, c.evaluation, c.facts_rebuilt, run, err, s) + `<p class="fs-12 mt-8">${s.verdicts_same} of ${s.verdicts_compared} verdicts unchanged · wording ${s.wording_similarity_pct ?? '?'} % alike · chat ${s.chat_similarity_pct ?? '?'} % alike · failures old ${s.failures.old}, new ${s.failures.new}</p>${changes}
    ${s.timings_contaminated ? `<p class="fs-12 t-amber mt-8">The per-title times were taken while another model shared the card${s.remeasured_clean ? '; tok/s, VRAM and GPU share below were measured again with each side alone' : ''}.</p>` : ''}
    <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th></th><th>Pinned</th><th>New</th></tr></thead><tbody>
      ${row('Judge, s per title', 'judge_s')}${row('Card note, s per title', 'monologue_s')}${row('Generation, tok/s', 'gen_tok_s')}
      ${s.speed_unstable ? `<tr><td colspan="3" class="fs-12 t-amber">Speed was unstable on this machine (reads old ${esc(String(s.gen_tok_s_reads?.old))}, new ${esc(String(s.gen_tok_s_reads?.new))} tok/s) — not used for the recommendation.</td></tr>` : ''}
      ${row(`VRAM, GB${s.vram_source ? ` (${[...new Set(Object.values(s.vram_source))].join(' / ')})` : ''}`, 'vram_gb')}${row('On the GPU, %', 'gpu_share_pct')}${row('Card note, words', 'monologue_words')}
    </tbody></table></div>
    <details class="mt-8"><summary class="fs-12">Side by side: ${c.pairs.length} titles, ${c.chat.length} chat answers</summary>${pairs}${chat}</details>`;
}

export async function loadModelUpdates(btn, refresh = false) {
  const box = document.getElementById('maint-models');
  if (!box) return;
  if (btn) btnBusy(btn, 'Checking…');
  try {
    const d = await api('/api/system/model-updates' + (refresh ? '/check' : ''), refresh ? 'POST' : 'GET');
    if (!d.models.length) {
      box.innerHTML = emptyHtml('Not checked yet — the first check runs a few minutes after the start.');
      return;
    }
    // While a test or review runs: every action is disabled (the server
    // refuses a second start anyway) and the section re-reads itself.
    const run = d.running;
    const off = run ? 'disabled' : '';
    box.innerHTML = d.models.map(m => {
      const [kind, label] = MU_STATUS[m.status] || ['', m.status];
      const testing = run?.role === m.role && run.kind === 'test';
      let body = '';
      if (m.comparison) {
        body = _muCompare(m.role, m.comparison, run, m.eval_error) + `<div class="section-actions mt-8">
          <button class="btn btn-primary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'update', EL)}>Take the update</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'stay', EL)}>Stay on the pinned version</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('rerunModelCompare', m.role, EL)}>Run the whole test again (new titles)</button></div>`
          + _muHistory(m.history);
      } else if (m.status === 'update' && m.comparable) {
        body = `<p class="fs-12 t3 mt-8">The test downloads the new weights next to the pinned ones, then judges ten of your titles and answers three chat questions with both, and the current model reviews the result — about half an hour on the GPU, chat waits meanwhile.</p>
          <div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${off} ${act('startModelCompare', m.role, EL)}>${testing ? `Test running since ${esc(_muTime(run.since))} — see Activity` : 'Test old vs new'}</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'stay', EL)}>Stay without testing</button></div>`;
      } else if (m.status === 'update') {
        body = `<p class="fs-12 t3 mt-8">No old-vs-new test for the ${esc(m.role)} yet. To update: <code class="mono">ollama pull ${esc(m.base)}</code>, then Rebuild models.</p>`;
      }
      const err = m.compare_error ? `<p class="t-danger fs-12 mt-8">Last test failed: ${esc(m.compare_error)}</p>` : '';
      return `<div class="banner ${kind}"><span class="banner-icon">${m.status === 'update' ? '!' : '·'}</span><div class="banner-text">
          <b>${esc(m.role)}</b> · ${esc(m.base)} — ${esc(label)}<span class="t3"> (running ${esc(_short(m.running))}${m.remote && m.remote !== m.running ? `, registry ${esc(_short(m.remote))}` : ''})</span>
          ${body}${err}</div></div>`;
    }).join('');
    _muPoll(!!run);
  } catch (e) {
    box.innerHTML = `<p class="t-danger fs-12">Could not read the model updates: ${esc(_errMsg(e))}</p>`;
  } finally {
    if (btn) btnDone(btn, 'Check now');
  }
}

// Re-read every 15 s while something runs and the section is on screen;
// stops by itself when the run ends or the pane is left.
let _muTimer = null;
function _muPoll(on) {
  clearTimeout(_muTimer);
  _muTimer = null;
  if (!on) { loadUnreadMessages(); return; }
  _muTimer = setTimeout(() => {
    const box = document.getElementById('maint-models');
    if (box && box.offsetParent !== null) loadModelUpdates();
  }, 15000);
}

// Earlier runs of this update test, newest first: when they ran, on which
// titles, what the review recommended.
function _muHistory(hist) {
  if (!hist?.length) return '';
  const rows = [...hist].reverse().map(h => {
    const t = h.tally?.titles;
    return `<li>${esc(h.at ? new Date(h.at).toLocaleString(loc()) : '?')} — ${esc(h.recommendation || 'no review')}`
      + (t ? ` (titles new ${t.new}, old ${t.old}, tie ${t.tie})` : '')
      + `<span class="t3"> · ${esc((h.titles || []).join(', '))}</span></li>`;
  }).join('');
  return `<details class="mt-8"><summary class="fs-12">Earlier runs (${hist.length})</summary><ul class="fs-12">${rows}</ul></details>`;
}

export async function rerunModelCompare(role, btn) {
  const res = await confirmDialog({
    title: 'Run the whole test again?',
    body: '<p>Ten other titles from your library, both sides judged again, then the review — about half an hour on the GPU, chat waits meanwhile. The current result moves to “Earlier runs”.</p>',
    confirmLabel: 'Run again',
  });
  if (!res.ok) return;
  return startModelCompare(role, btn, true);
}

export async function startModelCompare(role, btn, fresh = false) {
  btnBusy(btn, 'Starting…');
  try {
    await api(`/api/system/model-updates/${role}/compare${fresh ? '?fresh=true' : ''}`, 'POST');
    toast('Old-vs-new test started — progress in Activity.', 'info');
    loadUnreadMessages();
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
  loadModelUpdates();
}

export async function decideModelUpdate(role, action, btn) {
  const update = action === 'update';
  const res = await confirmDialog({
    title: update ? 'Take the new weights?' : 'Stay on the pinned version?',
    body: update ? '<p>The curatarr models are rebuilt from the new weights; the pinned copy is removed.</p>'
                 : '<p>The pinned weights go back under the tag. You hear about this model again only when the registry changes once more.</p>',
    confirmLabel: update ? 'Update' : 'Stay',
  });
  if (!res.ok) return;
  btnBusy(btn, update ? 'Rebuilding…' : 'Restoring…');
  try {
    await api(`/api/system/model-updates/${role}/decide`, 'POST', {action});
    toast(update ? 'Updated — the models were rebuilt.' : 'Staying on the pinned version.', 'success');
    loadUnreadMessages();
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
  loadModelUpdates();
}

export async function evaluateModelUpdate(role, btn) {
  btnBusy(btn, 'Starting…');
  try {
    await api(`/api/system/model-updates/${role}/evaluate`, 'POST');
    toast('Review started — the current model reads both sides; progress in Activity.', 'info');
    loadUnreadMessages();
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
  loadModelUpdates();
}

export function openModelUpdates() {
  document.getElementById('msg-panel')?.classList.remove('open');   // reached from the bell
  showView('settings', null, {pane: 'maintenance', focus: true});
  setTimeout(() => document.getElementById('maint-models')?.scrollIntoView({block: 'start'}), 50);
}
