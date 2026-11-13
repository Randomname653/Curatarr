// ── ADMIN MAINTENANCE ACTIONS ─────────────────────────────────────────────────
import { EL, _errHtml, _errMsg, _showTestResult, act, actOn, btnBusy, btnDone, confirmDialog, emptyHtml, esc, escAttr, haptic, loc, openModal, setLocale, setStatus, toast, trackDirty } from './ui.js';
import { setLanguage, t, th } from './i18n.js';
// _muEval and _muHistory name a vote tally `t` (tests read it): they say their words through these.
const _t = t, _th = th;
import { api } from './api.js';
import { loadHistoryStatus } from './history.js';
import { loadLibrarySettings } from './library_settings.js';
import { loadUsers } from './admin.js';
import { loadLibraryConfig } from './libraries.js';
import { showView } from './nav.js';
import { serverLabel, state } from './state.js';
import { loadUnreadMessages } from './notifications.js';
import { loadNodes } from './nodes.js';
import { loadMediaServer } from './media_switch.js';
import { loadHostAdvice } from './host_advice.js';
import { loadShokoUnmatched } from './shoko.js';
export async function reattributeHistory(btn) {
  const status = document.getElementById('admin-action-status');
  btnBusy(btn, t('set.maint.reattributing'));
  setStatus(status, t('set.maint.reattributing'), 'busy');
  try {
    const r = await api('/api/history/admin/re-attribute', 'POST');
    if (r.error) { setStatus(status, r.error, 'err'); return; }
    if (r.skipped) { setStatus(status, r.reason, 'ok'); return; }

    // Build a calmer summary that explains what each bucket actually means.
    const lines = [t('set.maint.done')];
    if (r.updated > 0) {
      lines.push(t('set.maint.ra_updated', {n: r.updated, x: r.updated.toLocaleString(loc())}));
    }
    if (r.unchanged > 0) {
      lines.push(t('set.maint.ra_unchanged', {n: r.unchanged, x: r.unchanged.toLocaleString(loc())}));
    }
    if (r.unattributable > 0) {
      // Frame this as expected behavior, not failure: Plex has limited history
      // retention, so older plays can't be cross-referenced and stay parked
      // on admin. That's fine for the "single primary user" case and lets the
      // multi-user code path own attribution for everything new from now on.
      lines.push(t('set.maint.ra_unattributable', {n: r.unattributable, x: r.unattributable.toLocaleString(loc())}));
    }
    if (typeof r.plex_history_events !== 'undefined') {
      lines.push('');
      lines.push(t('set.maint.ra_plex', {events: r.plex_history_events.toLocaleString(loc()), keys: r.plex_history_rating_keys.toLocaleString(loc()),
        items: (r.local_rks_in_plex_history + r.local_rks_missing_from_plex_history).toLocaleString(loc())}));
    }
    setStatus(status, lines.join(String.fromCharCode(10)), 'ok');
    toast(t('set.maint.ra_done'), 'success');
  } catch (e) {
    setStatus(status, _errMsg(e), 'err');
  } finally {
    btnDone(btn);
  }
}

export async function cleanupOrphans(btn) {
  const res = await confirmDialog({
    title: t('set.maint.hide_title'), confirmLabel: t('set.maint.hide_confirm'),
    body: `<p>${th('set.maint.hide_body')}</p><p class="t3 fs-12 mt-8">${th('set.maint.hide_note')}</p>`,
  });
  if (!res.ok) return;
  const status = document.getElementById('maint-cleanup-status');
  btnBusy(btn, t('set.maint.hiding'));
  setStatus(status, t('set.maint.hiding_long'), 'busy');
  try {
    let r = await api('/api/history/admin/cleanup-orphans', 'POST');
    if (r.error && r.would_archive) {
      // The share guard: a mass "orphaning" is usually a reading failure, so
      // the owner confirms the number before anything goes.
      const again = await confirmDialog({
        title: t('set.maint.hide_many_title', {n: r.would_archive, of: r.examined}),
        confirmLabel: t('set.maint.hide_anyway'),
        body: `<p>${th('set.maint.hide_many_body')}</p><p class="t3 fs-12 mt-8">${th('set.maint.hide_many_note', {server: serverLabel()})}</p>`,
      });
      if (!again.ok) { setStatus(status, r.error + ' ' + t('set.maint.hidden_zero'), 'err'); return; }
      r = await api('/api/history/admin/cleanup-orphans?force=true', 'POST');
    }
    if (r.error) { setStatus(status, r.error + ' ' + t('set.maint.hidden_zero'), 'err'); return; }
    const back = r.returned ? t('set.maint.back_in', {n: r.returned, server: serverLabel()}) : '';
    setStatus(status, t('set.maint.hid_status', {n: r.archived, of: r.examined, back}), 'ok');
    toast(t('set.maint.hid_toast', {n: r.archived}), 'success');
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
  } else if (name === 'media-server') {
    loadMediaServer();
  } else if (name === 'users') {
    loadUsers();
  } else if (name === 'maintenance') {
    // Plex parks plays on the admin until their member signs in; Jellyfin never does.
    const ra = document.getElementById('maint-reattribute');
    if (ra) ra.hidden = state.currentUser?.server === 'jellyfin';
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
  if (btn) btnBusy(btn, t('set.checking'));
  try {
    const d = await api('/api/system/dependencies');
    const rows = [
      ...d.missing.map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td class="t-danger">${th('set.deps.not_installed')}</td><td>${esc(m.pinned)}</td></tr>`),
      ...d.drift.map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.pinned)}</td></tr>`),
    ];
    const pinsHtml = d.clean
      ? `<div class="empty good">${th('set.deps.clean', {n: d.pins})}</div>`
      : `<div class="banner warn"><span class="banner-icon">!</span><div class="banner-text">${th('set.deps.differ', {n: rows.length, of: d.pins})} <code class="mono">${esc(d.command)}</code> ${th('set.deps.differ_tail')}</div></div>
         <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th>${th('set.deps.package')}</th><th>${th('set.deps.installed')}</th><th>${th('set.deps.pinned')}</th></tr></thead><tbody>${rows.join('')}</tbody></table></div>
         <p class="fs-12 t3 mt-8">${th('set.deps.interpreter')} <span class="mono">${esc(d.interpreter)}</span></p>`;
    // The tested install (lock/requirements.txt): what the pins pull in, at
    // the versions the battery ran with. The launchers reconcile at start.
    const L = d.lock || {};
    const lockRows = [
      ...(L.below || []).map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.locked)}</td><td>${th('set.deps.raised')}</td></tr>`),
      ...(L.above || []).map(m => `<tr><td class="cell-primary">${esc(m.name)}</td><td>${esc(m.installed)}</td><td>${esc(m.locked)}</td><td>${th('set.deps.lock_follows')}</td></tr>`),
      ...(L.unlocked || []).map(n => `<tr><td class="cell-primary">${esc(n)}</td><td>${th('set.deps.installed_lc')}</td><td>${th('set.deps.not_listed')}</td><td>${th('set.deps.added')}</td></tr>`),
    ];
    const lockHtml = L.error
      ? `<p class="fs-12 t3 mt-8">${th('set.deps.tested')} ${esc(L.error)}</p>`
      : L.clean
        ? `<p class="fs-12 t3 mt-8">${th('set.deps.tested_clean', {n: L.packages})}</p>`
        : `<div class="banner warn mt-8"><span class="banner-icon">!</span><div class="banner-text">${th('set.deps.lock_differ', {n: lockRows.length})} <code class="mono">${esc(L.command)}</code>.</div></div>
           <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th>${th('set.deps.package')}</th><th>${th('set.deps.installed')}</th><th>${th('set.deps.lock')}</th><th>${th('set.deps.next_start')}</th></tr></thead><tbody>${lockRows.join('')}</tbody></table></div>`;
    box.innerHTML = pinsHtml + lockHtml;
  } catch (e) {
    box.innerHTML = `<p class="t-danger fs-12">${th('set.deps.unreadable')} ${esc(e.message || e)}</p>`;
  } finally {
    if (btn) btnDone(btn, t('set.deps.check_again'));
  }
}

// ── Settings → Integrations (admin only) ──────────────────────────────────
// GET /api/setup/integrations hands back plain values for URLs / models and
// {set: bool} for secrets; POST /api/setup/reconfigure takes only the fields
// the admin touched. Secrets are typed to replace, cleared via the × button,
// and never travel back to the browser.
// Labels, titles and hints are i18n keys, read with t() when a card is drawn.
const PROFILE_OPTIONS = [['large', 'set.int.profile_large'],
                         ['small', 'set.int.profile_small']];
const INTEGRATION_CARDS = [
  {key: 'plex', title: 'set.int.plex', test: 'plex', fields: [
    {id: 'plex_url', label: 'set.int.server_url', placeholder: 'http://192.168.1.100:32400'},
    {id: 'plex_token', label: 'set.int.auth_token', secret: true, required: true},
  ]},
  {key: 'jellyfin', title: 'set.int.jellyfin', test: 'jellyfin',
   hint: 'set.int.jellyfin_hint', fields: [
    {id: 'jellyfin_url', label: 'set.int.server_url', placeholder: 'http://192.168.1.100:8096'},
    {id: 'jellyfin_api_key', label: 'set.int.api_key', secret: true},
  ]},
  // advice: the host whose card the bench's advice is for (host_advice.js);
  // rec: the role whose catalog choices the field lists.
  {key: 'models', title: 'set.int.models', test: 'ollama', advice: 'server', fields: [
    {id: 'ollama_endpoint', label: 'set.int.ollama_endpoint', placeholder: 'http://localhost:11434'},
    {id: 'base_curator_model', label: 'set.int.curator_model_main', rec: 'curator'},
    {id: 'base_summarizer_model', label: 'set.int.summarizer_model', rec: 'summarizer'},
    {id: 'embedding_model', label: 'set.int.embedding_model', rec: 'embedding'},
    {id: 'enable_pitcher', label: 'set.int.enable_pitcher', toggle: true},
    {id: 'base_pitcher_model', label: 'set.int.judge_model', rec: 'pitcher'},
    {id: 'llm_profile', label: 'set.int.profile', options: PROFILE_OPTIONS},
    {id: 'curator_num_ctx', label: 'set.int.num_ctx', number: true},
    {id: 'curator_num_gpu', label: 'set.int.num_gpu', number: true},
  ]},
  // A split install's second machine (OLLAMA_PC_ENDPOINT): its bakes are
  // built from its own bases, with its own profile. Shown only with a node.
  {key: 'node', title: 'set.int.node', node: true, advice: 'node',
   hint: 'set.int.node_hint',
   fields: [
    {id: 'pc_base_curator_model', label: 'set.int.curator_model', rec: 'curator'},
    {id: 'pc_base_summarizer_model', label: 'set.int.summarizer_model_node', rec: 'summarizer'},
    {id: 'pc_base_pitcher_model', label: 'set.int.judge_model_split', rec: 'pitcher'},
    {id: 'pc_llm_profile', label: 'set.int.profile', options: PROFILE_OPTIONS},
    {id: 'pc_curator_num_ctx', label: 'set.int.num_ctx', number: true},
    {id: 'pc_curator_num_gpu', label: 'set.int.num_gpu', number: true},
  ]},
  {key: 'gpu', title: 'set.int.gpu',
   hint: 'set.int.gpu_hint',
   fields: [
    {id: 'gpu_pressure_gate', label: 'set.int.gpu_gate', toggle: true},
    {id: 'llm_cpu_lane', label: 'set.int.cpu_lane', toggle: true},
    {id: 'llm_cpu_threads', label: 'set.int.cpu_threads', number: true},
    {id: 'llm_cpu_min_free_mb', label: 'set.int.cpu_min_free', number: true},
  ]},
  {key: 'metadata', title: 'set.int.metadata', fields: [
    {id: 'tmdb_api_key', label: 'set.int.tmdb_key', secret: true, test: 'tmdb'},
    {id: 'omdb_api_key', label: 'set.int.omdb_key', secret: true},
    {id: 'mal_client_id', label: 'set.int.mal_id', secret: true},
  ]},
  {key: 'music', title: 'set.int.music', fields: [
    {id: 'lastfm_api_key', label: 'set.int.lastfm_key', secret: true, test: 'lastfm'},
    {id: 'spotify_client_id', label: 'set.int.spotify_id'},
    {id: 'spotify_client_secret', label: 'set.int.spotify_secret', secret: true, test: 'spotify'},
    {id: 'listenbrainz_token', label: 'set.int.listenbrainz_token', secret: true},
    {id: 'soulsync_url', label: 'set.int.soulsync_url', placeholder: 'http://192.168.1.100:12279'},
    {id: 'soulsync_api_key', label: 'set.int.soulsync_key', secret: true},
  ]},
  {key: 'shoko', title: 'set.int.shoko', test: 'shoko',
   hint: 'set.int.shoko_hint',
   fields: [
    {id: 'shoko_url', label: 'set.int.shoko_url', placeholder: 'http://192.168.1.100:8111'},
    {id: 'shoko_api_key', label: 'set.int.shoko_keys', secret: true},
  ]},
  {key: 'subtitles', title: 'set.int.subtitles', fields: [
    {id: 'opensubtitles_api_key', label: 'set.int.os_key', secret: true},
    {id: 'opensubtitles_username', label: 'set.int.os_user'},
    {id: 'opensubtitles_password', label: 'set.int.os_password', secret: true},
    {id: 'opensubtitles_daily_budget', label: 'set.int.os_budget', number: true},
  ]},
];

let _integrationsCfg = null;

export async function loadIntegrations() {
  const shell = document.getElementById('integrations-shell');
  if (!shell) return;
  shell.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('common.loading')}</p>`;
  try {
    const r = await api('/api/setup/integrations');
    _integrationsCfg = r.config || {};
    if (r.pitcher_enabled !== undefined) _integrationsCfg.enable_pitcher = !!r.pitcher_enabled;
    shell.innerHTML = '';
    for (const card of INTEGRATION_CARDS) {
      if (card.node && !r.node) continue;
      shell.appendChild(renderIntegrationCard(card, _integrationsCfg));
      // With Shoko set up, the plays its sync could not place sit right
      // under its card, each with Link / Skip (shoko.js).
      if (card.key === 'shoko' && _integrationsCfg.shoko_url) {
        const sec = document.createElement('section');
        sec.className = 'section';
        sec.id = 'shoko-unmatched';
        shell.appendChild(sec);
        loadShokoUnmatched();
      }
    }
    loadHostAdvice();
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
      return `<label class="pref-row" for="int-${f.id}"><span class="pref-text">${th(f.label)}</span><input type="checkbox" class="switch" id="int-${f.id}" ${cur ? 'checked' : ''}></label>`;
    }
    if (f.secret) {
      const isSet = !!(cur && cur.set);
      const chip = `<span class="badge ${isSet ? 'success' : 'muted'} badge-sm">${th(isSet ? 'set.int.set' : 'set.int.not_set')}</span>`;
      const clear = (isSet && !f.required)
        ? `<button type="button" class="btn btn-secondary btn-sm" title="${th('set.int.clear_key')}" ${act('clearIntegrationSecret', f.id, card.key, EL)}>${th('set.int.clear')}</button>` : '';
      return `<div class="my-8 form-group">
        <label for="int-${f.id}">${th(f.label)} ${chip}</label>
        <div class="row"><input id="int-${f.id}" class="grow" type="password" autocomplete="new-password" placeholder="${th(isSet ? 'set.int.type_to_replace' : 'set.int.not_set')}">${clear}</div></div>`;
    }
    if (f.rec) {
      // Filled with the catalog's choices for this host's card by
      // host_advice.js; until then (or without an answer) the value set now.
      const v = cur === undefined || cur === null ? '' : String(cur);
      return `<div class="my-8 form-group">
      <label for="int-${f.id}">${th(f.label)}</label>
      <select class="w-auto" id="int-${f.id}" ${actOn('change', 'onAdviceModelChange', card.advice, f.rec)}><option value="${esc(v)}" selected>${v ? esc(v) : th('set.int.not_set')}</option></select></div>`;
    }
    if (f.options) {
      const opts = f.options.map(([v, text]) => `<option value="${esc(v)}"${String(cur) === v ? ' selected' : ''}>${th(text)}</option>`).join('');
      return `<div class="my-8 form-group">
      <label for="int-${f.id}">${th(f.label)}</label>
      <select class="w-auto" id="int-${f.id}">${opts}</select></div>`;
    }
    const type = f.number ? 'number' : 'text';
    return `<div class="my-8 form-group">
      <label for="int-${f.id}">${th(f.label)}</label>
      <input id="int-${f.id}" type="${type}" value="${esc(cur === undefined || cur === null ? '' : String(cur))}" placeholder="${esc(f.placeholder || '')}"></div>`;
  }).join('');
  // The bench's advice for this host's card (host_advice.js), outside the
  // form: picking a card size is no change to save.
  const advice = card.advice
    ? `<div class="panel-item my-8" id="int-advice-${card.advice}"><p class="fs-12 t3" role="status">${th('set.int.reading_card')}</p></div>` : '';
  const testBtn = card.test
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('testIntegration', card.key, EL)}>${th('set.int.test')}</button>` : '';
  const rebuild = card.key === 'models' || card.key === 'node'
    ? `<button type="button" class="btn btn-secondary btn-sm" ${act('rebuildModels', card.key, card.key === 'node' ? 'pc' : 'main', EL)} title="${th('set.int.rebuild_title')}">${th('set.int.rebuild')}</button>`
    : card.key === 'jellyfin' && state.currentUser?.server === 'jellyfin'
      ? `<button type="button" class="btn btn-secondary btn-sm" ${act('showJellyfinWebhook')}>${th('set.int.webhook')}</button>` : '';
  el.innerHTML = `
    <div class="section-head"><h3>${th(card.title)}</h3>${card.hint ? `<span class="section-hint">${th(card.hint)}</span>` : ''}</div>
    <div class="section-body">
      ${advice}<div id="int-form-${card.key}">${rows}</div>
      <div class="row mt-8">
        <button type="button" class="btn btn-primary btn-sm" id="int-save-${card.key}" ${act('saveIntegrations', card.key, EL)}>${th('set.int.save')}</button>
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
  if (!Object.keys(changes).length) { setStatus(msg, t('set.int.nothing_changed'), null); return; }
  btnBusy(btn, t('set.int.saving_btn'));
  setStatus(msg, t('set.int.saving'), 'busy');
  try {
    const r = await api('/api/setup/reconfigure', 'POST', changes);
    const note = t(r.models_changed ? 'set.int.saved_rebuild'
      : r.restart_recommended ? 'set.int.saved_restart' : 'set.int.saved_live');
    setStatus(msg, note, 'ok');
    toast(`${t(card.title)}: ${note}`, 'success');
    setTimeout(loadIntegrations, 1200);
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}

export async function clearIntegrationSecret(fieldId, cardKey, btn) {
  const res = await confirmDialog({title: t('set.int.clear_key'), danger: true, confirmLabel: t('set.int.clear'),
    body: `<p>${th('set.int.clear_body')}</p>`});
  if (!res.ok) return;
  const msg = document.getElementById(`int-msg-${cardKey}`);
  btnBusy(btn);
  try {
    await api('/api/setup/reconfigure', 'POST', {[fieldId]: ''});
    setStatus(msg, t('set.int.cleared'), 'ok');
    toast(t('set.int.key_cleared'), 'success');
    setTimeout(loadIntegrations, 800);
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); btnDone(btn); }
}

// The Webhook plugin's destination (history tier B, design §4.2): what to
// paste into Jellyfin. Opening it creates the shared secret.
export async function showJellyfinWebhook(rotate = false) {
  let w;
  try {
    if (rotate) await api('/api/jellyfin/webhook/rotate', 'POST');
    w = await api('/api/jellyfin/webhook/setup');
  } catch (e) { toast(t('set.int.webhook_unavailable', {error: _errMsg(e)}), 'danger'); return; }
  const url = `${location.origin}${w.path}`;
  openModal({title: t('set.wh.title'), size: 'wide', body: `
    <p class="fs-13 t2 mb-12">${th('set.wh.lead')}</p>
    <ol class="fs-13 t2 lh-15 mb-12">
      <li>${th('set.wh.step1')} <strong>Webhook</strong>${th('set.wh.step1_tail')}</li>
      <li>Plugins → Webhook → <strong>Add Generic Destination</strong>.</li>
      <li>${th('set.wh.url')} <code>${esc(url)}</code></li>
      <li>${th('set.wh.types')} <strong>${w.events.map(esc).join(', ')}</strong>; ${th('set.wh.send_all')}</li>
      <li>${th('set.wh.header')} <code>${esc(w.header)}</code> ${th('set.wh.header_value')} <code>${esc(w.secret)}</code></li>
      <li>${th('set.wh.template')}</li>
    </ol>
    <pre class="webhook-template mono">${esc(w.template)}</pre>`,
    foot: `<button type="button" class="btn btn-secondary" ${act('showJellyfinWebhook', true)}>${th('set.wh.new_secret')}</button>
           <button type="button" class="btn btn-primary" ${act('closeModal')}>${th('common.done')}</button>`});
}

export async function testIntegration(cardKey, btn) {
  const msg = document.getElementById(`int-msg-${cardKey}`);
  const val = id => document.getElementById(`int-${id}`)?.value?.trim() || '';
  // Blank secrets fall back to the stored ones server-side, so "Test" works
  // without re-typing a key that is already saved.
  let body;
  if (cardKey === 'plex') body = {service: 'plex', url: val('plex_url'), token: val('plex_token')};
  else if (cardKey === 'jellyfin') body = {service: 'jellyfin', url: val('jellyfin_url'), api_key: val('jellyfin_api_key')};
  else if (cardKey === 'models') body = {service: 'ollama', url: val('ollama_endpoint')};
  else if (cardKey === 'metadata') body = {service: 'tmdb', api_key: val('tmdb_api_key')};
  else if (cardKey === 'shoko') body = {service: 'shoko', url: val('shoko_url'), api_key: val('shoko_api_key')};
  else if (cardKey === 'music') body = val('spotify_client_id') || val('spotify_client_secret')
    ? {service: 'spotify', client_id: val('spotify_client_id'), client_secret: val('spotify_client_secret')}
    : {service: 'lastfm', api_key: val('lastfm_api_key')};
  else return;
  btnBusy(btn, t('set.int.testing_btn'));
  setStatus(msg, t('set.int.testing'), 'busy');
  try {
    _showTestResult(msg, await api('/api/setup/test', 'POST', body));
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); }
  btnDone(btn);
}

// host: "main" (this install's Ollama) or "pc" (the node's, while it is free).
export async function rebuildModels(cardKey, host, btn) {
  const msg = document.getElementById(`int-msg-${cardKey}`);
  const where = t(host === 'pc' ? 'set.int.where_node' : 'set.int.where_here');
  const res = await confirmDialog({title: t('set.int.rebuild_dialog'), confirmLabel: t('set.int.rebuild_confirm'),
    body: `<p>${th('set.int.rebuild_body', {where})}</p>`});
  if (!res.ok) return;
  btnBusy(btn, t('set.int.building_btn'));
  setStatus(msg, t('set.int.building'), 'busy');
  try {
    const r = await api(`/api/setup/build-models?host=${host === 'pc' ? 'pc' : 'main'}`, 'POST');
    const ok = r.curator && r.summarizer;
    setStatus(msg, t(ok ? 'set.int.rebuilt_status' : 'set.int.build_failed_status'), ok ? 'ok' : 'err');
    toast(t(ok ? 'set.int.rebuilt_toast' : 'set.int.build_failed_toast'), ok ? 'success' : 'danger', {ms: 8000});
  } catch (e) { setStatus(msg, _errMsg(e), 'err'); }
  btnDone(btn);
}


export async function loadNotificationPreferences() {
  const host = document.getElementById('notif-prefs');
  if (!host) return;
  host.innerHTML = `<p class="loading" role="status" aria-live="polite">${th('common.loading')}</p>`;
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
    toast(t('common.saved'), 'success');
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
  if (!triggers.length) { host.innerHTML = emptyHtml(th('set.notif.none')); return; }
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
  if (roleEl) roleEl.textContent = `· ${t(u.is_admin ? 'account.role_admin' : 'account.role_user')}`;
  if (idEl)   idEl.textContent   = u.plex_user_id || u.id || '—';
  loadCuratorVoice();
  loadUiLanguage();
}

// Settings → Account → Language (UX design record 3.8): Automatic (the
// language the user writes to the curator in), English or Deutsch. It sets
// the UI and every text the server writes for this user - never the language
// of a chat answer. Saved on the server; the UI switches at once.
const _langRadios = () => document.querySelectorAll('input[name="ui-language"]');

export async function loadUiLanguage() {
  try {
    const r = await api('/api/users/me/ui-language');
    for (const el of _langRadios()) el.checked = el.value === r.setting;
    const hint = document.getElementById('ui-language-detected');
    // A detected language without a name in the dictionary reads as English (as the UI does).
    const name = t(`language.${r.detected}`);
    if (hint) hint.textContent = t('account.language_detected', {language: name.startsWith('language.') ? t('language.en') : name});
  } catch (_) { /* the radios stay unset; picking one still saves */ }
}

export async function setUiLanguage(input) {
  const language = input?.value;
  if (!language) return;
  haptic('light');
  try {
    const r = await api('/api/users/me/ui-language', 'POST', {language});
    setLocale(r.locale);
    setLanguage(r.language);
    if (state.currentUser) Object.assign(state.currentUser, {ui_language: r.language, locale: r.locale});
    toast(t('common.saved'), 'success');
    loadUiLanguage();
  } catch (e) {
    toast(_errMsg(e), 'danger');
    loadUiLanguage();
  }
}

// Settings → Account → Curator: the tone the curator talks to this user in
// (services/curator_voice). Saved on the server, so it follows the account.
const _voiceRadios = () => document.querySelectorAll('input[name="curator-voice"]');

export async function loadCuratorVoice() {
  try {
    const r = await api('/api/users/me/curator-voice');
    for (const el of _voiceRadios()) el.checked = el.value === r.voice;
  } catch (_) { /* the radios stay unset; picking one still saves */ }
}

export async function setCuratorVoice(input) {
  const voice = input?.value;
  if (!voice) return;
  haptic('light');
  try {
    await api('/api/users/me/curator-voice', 'POST', {voice});
    toast(t('common.saved'), 'success');
  } catch (e) {
    toast(_errMsg(e), 'danger');
    loadCuratorVoice();
  }
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
  current: ['good', 'set.mu.st_current'], update: ['warn', 'set.mu.st_update'],
  pinned: ['', 'set.mu.st_pinned'], unknown: ['', 'set.mu.st_unknown'],
};
const _short = d => (d || '').replace('sha256:', '').slice(0, 12);

// The current model's blind review (services/model_update_eval.py).
const MU_REC = {update: ['ok', 'set.mu.rec_update'], stay: ['warn', 'set.mu.rec_stay'],
                either: ['', 'set.mu.rec_either'],
                // The judge did not answer most pairs: no recommendation, not a draw.
                'no verdict': ['warn', 'set.mu.rec_none']};
function _muEval(role, ev, facts_rebuilt, run, err, summary) {
  if (run?.role === role && run.kind === 'review')
    return `<div class="banner mt-8"><span class="banner-icon">…</span><div class="banner-text">${_th('set.mu.reviewing', {time: _muTime(run.since)})}</div></div>`;
  if (!ev) return (err ? `<p class="t-danger fs-12 mt-8">${_th('set.mu.review_failed')} ${esc(err)}</p>` : '')
    + `<div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${run ? 'disabled' : ''} ${act('evaluateModelUpdate', role, EL)}>${_th(err ? 'set.mu.review_again' : 'set.mu.review_it')}</button></div>`;
  const [kind, lk] = MU_REC[ev.recommendation] || ['', null];
  const label = lk ? _t(lk) : ev.recommendation;
  const t = ev.tally.titles, c = ev.tally.chat;
  // Reviews from before f3ea0a1 carry no vram_source: their VRAM may be /api/ps's.
  const stale = !summary?.vram_source;
  const rules = ev.guards.length
    ? `<p class="fs-12 mt-4">${_th('set.mu.hard_rules')} ${esc(ev.guards.join('; '))}${ev.overruled ? _th('set.mu.overruled') : '.'}</p>` : '';
  const per = ev.titles.map(r => `<li>${esc(r.key)}: <b>${esc(r.winner)}</b>${r.position_flip ? _th('set.mu.flipped') : ''} — ${esc(r.reasons[0] || '')}</li>`).join('');
  return `<div class="banner ${kind} mt-8"><span class="banner-icon">${ev.recommendation === 'no verdict' ? '!' : '✓'}</span><div class="banner-text">
      <b>${esc(ev.judge_model)}</b> ${esc(label)}: ${esc(ev.model_reason)}
      <p class="fs-12 mt-4">${_th('set.mu.tally_titles', {new: t.new, old: t.old, tie: t.tie})}${t.unreadable ? _th('set.mu.unreadable_n', {n: t.unreadable}) : ''} · ${_th('set.mu.tally_chat', {new: c.new, old: c.old, tie: c.tie})}${c.unreadable ? _th('set.mu.unreadable_n', {n: c.unreadable}) : ''}</p>${rules}
      ${(t.unreadable || c.unreadable) ? `<p class="fs-12 t-amber mt-4">${_th('set.mu.unreadable_note', {n: (t.unreadable || 0) + (c.unreadable || 0)})}</p>` : ''}
      <p class="fs-12 t3 mt-4">${_th('set.mu.blind', {rebuilt: facts_rebuilt ? _t('set.mu.rebuilt') : ''})}</p>
      <details class="mt-4"><summary class="fs-12">${_th('set.mu.reason_per_title')}</summary><ul class="fs-12">${per}</ul></details>
      ${stale ? `<p class="fs-12 t-amber mt-4">${_th('set.mu.stale')}</p>` : ''}
      <div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${run ? 'disabled' : ''} ${act('evaluateModelUpdate', role, EL)}>${_th('set.mu.review_again_full')}</button></div></div></div>`;
}

const _muTime = iso => iso ? new Date(iso).toLocaleTimeString(loc(), {hour: '2-digit', minute: '2-digit'}) : '';

function _muCompare(role, c, run, err) {
  const s = c.summary;
  const row = (label, k, unit = '') => `<tr><td class="cell-primary">${esc(label)}</td><td>${esc(String(s[k]?.old ?? '–'))}${unit}</td><td>${esc(String(s[k]?.new ?? '–'))}${unit}</td></tr>`;
  const changes = s.verdict_changes.length
    ? `<ul class="fs-12 mt-8">${s.verdict_changes.map(v => `<li>${esc(v.title)}: ${esc(v.old)} → ${esc(v.new)}</li>`).join('')}</ul>` : '';
  const pairs = c.pairs.map(p => `<div class="mt-8"><div class="fw-500 fs-12">${esc(p.title)} — ${esc(p.old_verdict || '?')} / ${esc(p.new_verdict || '?')}</div>
      <div class="fs-12 t3">${th('set.mu.old')} ${esc(p.old || '—')}</div><div class="fs-12">${th('set.mu.new')} ${esc(p.new || '—')}</div></div>`).join('');
  const chat = c.chat.map(p => `<div class="mt-8"><div class="fw-500 fs-12">${esc(p.prompt)}</div>
      <div class="fs-12 t3">${th('set.mu.old')} ${esc(p.old || '—')}</div><div class="fs-12">${th('set.mu.new')} ${esc(p.new || '—')}</div></div>`).join('');
  return _muEval(role, c.evaluation, c.facts_rebuilt, run, err, s) + `<p class="fs-12 mt-8">${th('set.mu.summary', {same: s.verdicts_same, of: s.verdicts_compared, wording: s.wording_similarity_pct ?? '?', chat: s.chat_similarity_pct ?? '?', fold: s.failures.old, fnew: s.failures.new})}</p>${changes}
    ${s.timings_contaminated ? `<p class="fs-12 t-amber mt-8">${th('set.mu.contaminated', {again: s.remeasured_clean ? t('set.mu.remeasured') : ''})}</p>` : ''}
    <div class="tbl-wrap mt-8"><table class="tbl tbl-cards"><thead><tr><th></th><th>${th('set.deps.pinned')}</th><th>${th('set.mu.col_new')}</th></tr></thead><tbody>
      ${row(t('set.mu.row_judge'), 'judge_s')}${row(t('set.mu.row_note_s'), 'monologue_s')}${row(t('set.mu.row_gen'), 'gen_tok_s')}
      ${s.speed_unstable ? `<tr><td colspan="3" class="fs-12 t-amber">${th('set.mu.unstable', {old: String(s.gen_tok_s_reads?.old), new: String(s.gen_tok_s_reads?.new)})}</td></tr>` : ''}
      ${row(`VRAM, GB${s.vram_source ? ` (${[...new Set(Object.values(s.vram_source))].join(' / ')})` : ''}`, 'vram_gb')}${row(t('set.mu.row_gpu'), 'gpu_share_pct')}${row(t('set.mu.row_note_words'), 'monologue_words')}
    </tbody></table></div>
    <details class="mt-8"><summary class="fs-12">${th('set.mu.side_by_side', {titles: c.pairs.length, chat: c.chat.length})}</summary>${pairs}${chat}</details>`;
}

export async function loadModelUpdates(btn, refresh = false) {
  const box = document.getElementById('maint-models');
  if (!box) return;
  if (btn) btnBusy(btn, t('set.checking'));
  try {
    const d = await api('/api/system/model-updates' + (refresh ? '/check' : ''), refresh ? 'POST' : 'GET');
    if (!d.models.length) {
      box.innerHTML = emptyHtml(th('set.mu.not_checked'));
      return;
    }
    // While a test or review runs: every action is disabled (the server
    // refuses a second start anyway) and the section re-reads itself.
    const run = d.running;
    const off = run ? 'disabled' : '';
    box.innerHTML = d.models.map(m => {
      const [kind, lk] = MU_STATUS[m.status] || ['', null];
      const label = lk ? t(lk) : m.status;
      const testing = run?.role === m.role && run.kind === 'test';
      let body = '';
      if (m.comparison) {
        body = _muCompare(m.role, m.comparison, run, m.eval_error) + `<div class="section-actions mt-8">
          <button class="btn btn-primary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'update', EL)}>${th('set.mu.take')}</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'stay', EL)}>${th('set.mu.stay_btn')}</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('rerunModelCompare', m.role, EL)}>${th('set.mu.rerun_btn')}</button></div>`
          + _muHistory(m.history);
      } else if (m.status === 'update' && m.comparable) {
        body = `<p class="fs-12 t3 mt-8">${th('set.mu.test_explain')}</p>
          <div class="section-actions mt-8"><button class="btn btn-secondary btn-sm" ${off} ${act('startModelCompare', m.role, EL)}>${testing ? th('set.mu.test_running', {time: _muTime(run.since)}) : th('set.mu.test')}</button>
          <button class="btn btn-secondary btn-sm" ${off} ${act('decideModelUpdate', m.role, 'stay', EL)}>${th('set.mu.stay_untested')}</button></div>`;
      } else if (m.status === 'update') {
        body = `<p class="fs-12 t3 mt-8">${th('set.mu.no_test', {role: m.role})} <code class="mono">ollama pull ${esc(m.base)}</code>${th('set.mu.no_test_tail')}</p>`;
      }
      const err = m.compare_error ? `<p class="t-danger fs-12 mt-8">${th('set.mu.last_failed')} ${esc(m.compare_error)}</p>` : '';
      return `<div class="banner ${kind}"><span class="banner-icon">${m.status === 'update' ? '!' : '·'}</span><div class="banner-text">
          <b>${esc(m.role)}</b> · ${esc(m.base)} — ${esc(label)}<span class="t3"> (${th('set.mu.running', {d: _short(m.running)})}${m.remote && m.remote !== m.running ? th('set.mu.registry', {d: _short(m.remote)}) : ''})</span>
          ${body}${err}</div></div>`;
    }).join('');
    _muPoll(!!run);
  } catch (e) {
    box.innerHTML = `<p class="t-danger fs-12">${th('set.mu.unreadable')} ${esc(_errMsg(e))}</p>`;
  } finally {
    if (btn) btnDone(btn, t('set.mu.check_now'));
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
    return `<li>${esc(h.at ? new Date(h.at).toLocaleString(loc()) : '?')} — ${h.recommendation ? esc(h.recommendation) : _th('set.mu.no_review')}`
      + (t ? _th('set.mu.hist_tally', {new: t.new, old: t.old, tie: t.tie}) : '')
      + `<span class="t3"> · ${esc((h.titles || []).join(', '))}</span></li>`;
  }).join('');
  return `<details class="mt-8"><summary class="fs-12">${_th('set.mu.earlier', {n: hist.length})}</summary><ul class="fs-12">${rows}</ul></details>`;
}

export async function rerunModelCompare(role, btn) {
  const res = await confirmDialog({
    title: t('set.mu.rerun_title'),
    body: `<p>${th('set.mu.rerun_body')}</p>`,
    confirmLabel: t('set.mu.rerun_confirm'),
  });
  if (!res.ok) return;
  return startModelCompare(role, btn, true);
}

export async function startModelCompare(role, btn, fresh = false) {
  btnBusy(btn, t('set.starting'));
  try {
    await api(`/api/system/model-updates/${role}/compare${fresh ? '?fresh=true' : ''}`, 'POST');
    toast(t('set.mu.test_started'), 'info');
    loadUnreadMessages();
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
  loadModelUpdates();
}

export async function decideModelUpdate(role, action, btn) {
  const update = action === 'update';
  const res = await confirmDialog({
    title: t(update ? 'set.mu.take_title' : 'set.mu.stay_title'),
    body: `<p>${th(update ? 'set.mu.take_body' : 'set.mu.stay_body')}</p>`,
    confirmLabel: t(update ? 'set.mu.update' : 'set.mu.stay'),
  });
  if (!res.ok) return;
  btnBusy(btn, t(update ? 'set.mu.rebuilding' : 'set.mu.restoring'));
  try {
    await api(`/api/system/model-updates/${role}/decide`, 'POST', {action});
    toast(t(update ? 'set.mu.updated' : 'set.mu.staying'), 'success');
    loadUnreadMessages();
  } catch (e) {
    toast(_errMsg(e), 'danger');
  }
  loadModelUpdates();
}

export async function evaluateModelUpdate(role, btn) {
  btnBusy(btn, t('set.starting'));
  try {
    await api(`/api/system/model-updates/${role}/evaluate`, 'POST');
    toast(t('set.mu.review_started'), 'info');
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
