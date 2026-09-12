// ── ADMIN MAINTENANCE ACTIONS ─────────────────────────────────────────────────
import { _errHtml, _errMsg, _showTestResult, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, setStatus, toast, trackDirty } from './ui.js';
import { api } from './api.js';
import { loadHistoryStatus } from './history.js';
import { loadLibrarySettings } from './library_settings.js';
import { loadUsers } from './admin.js';
import { state } from './state.js';
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
      lines.push(`• ${r.updated.toLocaleString()} row${r.updated === 1 ? '' : 's'} (re)assigned to a different Plex account.`);
    }
    if (r.unchanged > 0) {
      lines.push(`• ${r.unchanged.toLocaleString()} row${r.unchanged === 1 ? '' : 's'} already correctly attributed (idempotent — no-op on repeat runs).`);
    }
    if (r.unattributable > 0) {
      // Frame this as expected behavior, not failure: Plex has limited history
      // retention, so older plays can't be cross-referenced and stay parked
      // on admin. That's fine for the "single primary user" case and lets the
      // multi-user code path own attribution for everything new from now on.
      lines.push(`• ${r.unattributable.toLocaleString()} row${r.unattributable === 1 ? '' : 's'} kept on admin — older than Plex's history retention horizon, can't be matched. This is expected and harmless.`);
    }
    if (typeof r.plex_history_events !== 'undefined') {
      lines.push('');
      lines.push(`Plex returned ${r.plex_history_events.toLocaleString()} play events covering ${r.plex_history_rating_keys.toLocaleString()} unique items. Your library has ${(r.local_rks_in_plex_history + r.local_rks_missing_from_plex_history).toLocaleString()} distinct watched items in total.`);
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
    title: 'Cleanup orphans', danger: true, confirmLabel: 'Delete orphans',
    body: '<p>Deletes watch_history rows pointing to Plex media that no longer exists in any library.</p><p class="t3 fs-12 mt-8">Spotify entries are preserved. The action aborts safely if any library section cannot be read.</p>',
  });
  if (!res.ok) return;
  const status = document.getElementById('maint-cleanup-status');
  btnBusy(btn, 'Cleaning…');
  setStatus(status, 'Cleaning orphans…', 'busy');
  try {
    const r = await api('/api/history/admin/cleanup-orphans', 'POST');
    if (r.error) { setStatus(status, r.error + ' (deleted: 0)', 'err'); return; }
    setStatus(status, `Done — ${r.deleted} deleted, ${r.examined} examined, ${r.live_keys_seen} live ratingKeys in Plex`, 'ok');
    toast(`Cleanup done — ${r.deleted} orphaned rows deleted`, 'success');
    if (r.deleted > 0) setTimeout(loadHistoryStatus, 800);
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
  // Per-pane lazy loaders
  if (name === 'account') {
    populateSettingsAccount();
    loadPinStatus();
  } else if (name === 'notifications') {
    loadNotificationPreferences();
  } else if (name === 'library') {
    loadLibrarySettings();
  } else if (name === 'integrations') {
    loadIntegrations();
  } else if (name === 'users') {
    loadUsers();
  } else if (name === 'maintenance') {
    loadDepsStatus();
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
      ...d.missing.map(m => `<tr><td>${esc(m.name)}</td><td class="t-danger">not installed</td><td>${esc(m.pinned)}</td></tr>`),
      ...d.drift.map(m => `<tr><td>${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.pinned)}</td></tr>`),
    ];
    box.innerHTML = d.clean
      ? `<div class="empty good">All ${d.pins} pinned packages match requirements.txt.</div>`
      : `<div class="banner warn"><span class="banner-icon">!</span><div class="banner-text">${rows.length} of ${d.pins} pinned packages differ. In the Curatarr folder, with this interpreter, run <code class="mono">${esc(d.command)}</code> and restart. start.bat and the tray launcher do this on their own at the next start.</div></div>
         <div class="tbl-wrap mt-8"><table class="tbl"><thead><tr><th>Package</th><th>Installed</th><th>Pinned</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
         <p class="fs-12 t3 mt-8">Interpreter: <span class="mono">${esc(d.interpreter)}</span></p>`;
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
  {key: 'metadata', title: 'Movies & series metadata', fields: [
    {id: 'tmdb_api_key', label: 'TMDB API key', secret: true, test: 'tmdb'},
    {id: 'omdb_api_key', label: 'OMDb API key', secret: true},
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
    shell.innerHTML = _errHtml(e, 'loadIntegrations()');
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
      return `<label class="row fs-13" style="margin:8px 0"><input type="checkbox" id="int-${f.id}" ${cur ? 'checked' : ''}> ${esc(f.label)}</label>`;
    }
    if (f.secret) {
      const isSet = !!(cur && cur.set);
      const chip = isSet ? '<span class="badge success badge-sm">set</span>' : '<span class="badge muted badge-sm">not set</span>';
      const clear = (isSet && !f.required)
        ? `<button type="button" class="btn btn-secondary btn-sm" title="Clear this key" onclick="clearIntegrationSecret('${f.id}', '${card.key}', this)">Clear</button>` : '';
      return `<div class="form-group" style="margin:8px 0">
        <label for="int-${f.id}">${esc(f.label)} ${chip}</label>
        <div class="row"><input id="int-${f.id}" class="grow" type="password" autocomplete="new-password" placeholder="${isSet ? 'type to replace' : 'not set'}">${clear}</div></div>`;
    }
    const type = f.number ? 'number' : 'text';
    return `<div class="form-group" style="margin:8px 0">
      <label for="int-${f.id}">${esc(f.label)}</label>
      <input id="int-${f.id}" type="${type}" value="${esc(cur === undefined || cur === null ? '' : String(cur))}" placeholder="${esc(f.placeholder || '')}"></div>`;
  }).join('');
  const testBtn = card.test
    ? `<button type="button" class="btn btn-secondary btn-sm" onclick="testIntegration('${card.key}',this)">Test connection</button>` : '';
  const rebuild = card.key === 'models'
    ? `<button type="button" class="btn btn-secondary btn-sm" onclick="rebuildModels('${card.key}',this)" title="Bake the chosen models via ollama create">Rebuild models</button>` : '';
  el.innerHTML = `
    <div class="section-head"><h3>${esc(card.title)}</h3></div>
    <div class="section-body">
      <div id="int-form-${card.key}">${rows}</div>
      <div class="row mt-8">
        <button type="button" class="btn btn-primary btn-sm" id="int-save-${card.key}" onclick="saveIntegrations('${card.key}',this)">Save</button>
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
    host.innerHTML = _errHtml(e, 'loadNotificationPreferences()');
  }
}

export async function _setNotifPref(toggle) {
  const wantEnabled = toggle.checked;
  const chip = toggle.closest('.chip'), label = chip.querySelector('span');
  toggle.disabled = true;
  chip.classList.toggle('active', wantEnabled);
  label.textContent = wantEnabled ? 'on' : 'off';
  try {
    await api('/api/users/me/notification-preferences', 'POST', {trigger_type: toggle.dataset.triggerType, enabled: wantEnabled});
    toast('Saved', 'success');
  } catch (e) {
    // Roll back the visual state if the server rejected the change.
    toggle.checked = !wantEnabled;
    chip.classList.toggle('active', toggle.checked);
    label.textContent = toggle.checked ? 'on' : 'off';
    toast(_errMsg(e), 'danger');
  } finally {
    toggle.disabled = false;
  }
}

// One row per trigger; the on/off chip saves immediately (the only pane
// without a Save button — a toggle is its own commit).
export function renderNotificationPreferences(triggers) {
  const host = document.getElementById('notif-prefs');
  if (!host) return;
  if (!triggers.length) { host.innerHTML = emptyHtml('No triggers are configured on this server.'); return; }
  host.innerHTML = triggers.map(t => `<div class="panel-item">
    <div class="panel-item-head">
      <div class="grow"><div class="panel-item-title">${esc(t.label || t.type)}</div><div class="panel-item-sub" style="margin-top:2px">${esc(t.description || '')}</div></div>
      <label class="chip${t.enabled ? ' active' : ''}"><input type="checkbox" ${t.enabled ? 'checked' : ''} data-trigger-type="${escAttr(t.type)}" onchange="_setNotifPref(this)"> <span>${t.enabled ? 'on' : 'off'}</span></label>
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

export async function loadPinStatus() {
  const stateEl = document.getElementById('settings-pin-state');
  const formSet = document.getElementById('settings-pin-form-set');
  const formChange = document.getElementById('settings-pin-form-change');
  if (!stateEl || !formSet || !formChange) return;
  stateEl.textContent = 'checking…';
  stateEl.classList.remove('t-amber', 't-danger');
  try {
    const r = await api('/api/users/me/pin-status');
    if (r.has_pin) {
      const when = r.set_at ? new Date(r.set_at).toLocaleString() : '';
      stateEl.textContent = when ? `set · last updated ${when}` : 'set';
      stateEl.classList.add('t-amber');
    } else {
      stateEl.textContent = 'not set';
    }
    formSet.hidden = !!r.has_pin;
    formChange.hidden = !r.has_pin;
  } catch (e) {
    stateEl.textContent = _errMsg(e) || 'failed to load PIN status';
    stateEl.classList.add('t-danger');
  }
}

export async function submitPinSet(btn) {
  const newPin = document.getElementById('pin-new').value;
  const confirmPin = document.getElementById('pin-new-confirm').value;
  const status = document.getElementById('pin-set-status');
  if (newPin.length < 6) { setStatus(status, 'PIN must be at least 6 characters.', 'err'); return; }
  if (newPin !== confirmPin) { setStatus(status, "PINs don't match.", 'err'); return; }
  btnBusy(btn, 'Saving…');
  setStatus(status, 'Saving…', 'busy');
  try {
    await api('/api/users/me/pin', 'POST', { pin: newPin });
    setStatus(status, 'PIN set.', 'ok');
    toast('PIN set', 'success');
    document.getElementById('pin-new').value = '';
    document.getElementById('pin-new-confirm').value = '';
    loadPinStatus();
  } catch (e) {
    setStatus(status, _errMsg(e) || 'Failed', 'err');
  }
  btnDone(btn);
}

export async function submitPinChange(btn) {
  const cur = document.getElementById('pin-current').value;
  const newPin = document.getElementById('pin-change-new').value;
  const confirmPin = document.getElementById('pin-change-confirm').value;
  const status = document.getElementById('pin-change-status');
  if (!cur) { setStatus(status, 'Current PIN required.', 'err'); return; }
  if (newPin.length < 6) { setStatus(status, 'New PIN must be at least 6 characters.', 'err'); return; }
  if (newPin !== confirmPin) { setStatus(status, "New PINs don't match.", 'err'); return; }
  if (newPin === cur) { setStatus(status, 'New PIN must differ from current.', 'err'); return; }
  btnBusy(btn, 'Saving…');
  setStatus(status, 'Saving…', 'busy');
  try {
    await api('/api/users/me/pin', 'POST', { pin: newPin, current_pin: cur });
    setStatus(status, 'PIN changed.', 'ok');
    toast('PIN changed', 'success');
    document.getElementById('pin-current').value = '';
    document.getElementById('pin-change-new').value = '';
    document.getElementById('pin-change-confirm').value = '';
    loadPinStatus();
  } catch (e) {
    setStatus(status, _errMsg(e) || 'Failed', 'err');
  }
  btnDone(btn);
}
