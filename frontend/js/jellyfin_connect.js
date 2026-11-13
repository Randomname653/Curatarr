// ── CONNECT A JELLYFIN SERVER ────────────────────────────────────────────────
// Found on the LAN, approved by its admin with a Quick Connect code (or the
// admin password once); Curatarr makes its own API key, which stays on the
// server under a ticket (routers/setup.py, media_server/jellyfin_connect.py).
// The setup wizard and Settings -> Media server both draw this box; the
// caller says what a finished connect means with onConnected.
import { EVENT, _errMsg, act, actOn, esc, escAttr } from './ui.js';
import { api } from './api.js';
import { t, th } from './i18n.js';

let _onConnected = null;
let _poll = null;
// 5 s: one LAN call to Curatarr and one to Jellyfin, inside the 30 a minute
// the setup router budgets per client.
const POLL_MS = 5000;

function _stopPoll() { if (_poll) { clearInterval(_poll); _poll = null; } }

function _detail(e, fallback) {
  try { return JSON.parse(e.message).detail || fallback; } catch { return fallback; }
}

// The box: the found servers, the address field, the two ways to connect.
// ``connected`` = {server_name, version, admin} once a connect finished.
export function jellyfinConnectHtml(url, connected, onConnected) {
  _onConnected = onConnected;
  if (connected) {
    return `<div class="test-status ok">${th('jfconn.connected_to', {server: connected.server_name || 'Jellyfin'})}${connected.version ? ` (Jellyfin ${esc(connected.version)})` : ''}${th('jfconn.approved_by', {admin: connected.admin || t('jfconn.its_admin')})}</div>
      <button class="btn btn-secondary btn-sm mt-8" ${act('jellyfinConnectAgain')}>${th('jfconn.connect_other')}</button>`;
  }
  return `<div class="form-group">
      <label for="s-jellyfin-url">${th('jfconn.server')}</label>
      <div id="jf-found" class="mb-8" role="status" aria-live="polite"><span class="t3 fs-12">${th('jfconn.looking')}</span></div>
      <input id="s-jellyfin-url" type="url" placeholder="http://192.168.1.100:8096" value="${escAttr(url || '')}">
      <div class="hint">${th('jfconn.url_hint')}</div>
    </div>
    <div class="row">
      <button class="btn btn-primary btn-sm" id="jf-connect-btn" ${act('jellyfinConnectStart')}>${th('jfconn.connect_qc')}</button>
      <button class="btn btn-secondary btn-sm" ${act('jellyfinShowPassword')}>${th('jfconn.use_password')}</button>
    </div>
    <div hidden id="jf-setup-qc" class="boxed mt-8 t-center">
      <p class="fs-13 t2">${th('jfconn.qc_where')}</p>
      <div class="setup-qc-code" id="jf-setup-code">··· ···</div>
      <small class="t2" id="jf-setup-status" role="status" aria-live="polite">${th('jfconn.waiting')}</small>
    </div>
    <form hidden id="jf-setup-pw" class="boxed mt-8" ${actOn('submit', 'jellyfinPasswordConnect', EVENT)}>
      <div class="form-group">
        <label for="jf-setup-user">${th('jfconn.admin')}</label>
        <input id="jf-setup-user" autocomplete="username" required maxlength="256">
      </div>
      <div class="form-group">
        <label for="jf-setup-pass">${th('jfconn.password')}</label>
        <input id="jf-setup-pass" type="password" autocomplete="current-password" maxlength="1024">
        <div class="hint">${th('jfconn.password_hint')}</div>
      </div>
      <button class="btn btn-primary btn-sm" id="jf-setup-pw-btn" type="submit">${th('jfconn.connect')}</button>
    </form>
    <div class="fs-11 t3 mt-8 lh-15">${th('jfconn.members_hint')}</div>`;
}

// Fills #jf-found with the servers on the LAN; one found = picked.
export async function findJellyfinServers() {
  const box = document.getElementById('jf-found');
  if (!box) return;
  let servers = [];
  try { servers = (await api('/api/setup/jellyfin/servers')).servers || []; }
  catch (e) { box.innerHTML = `<span class="t3 fs-12">${th('jfconn.search_failed', {error: _errMsg(e)})}</span>`; return; }
  if (!document.getElementById('jf-found')) return;   // the step moved on meanwhile
  if (!servers.length) {
    box.innerHTML = `<span class="t3 fs-12">${th('jfconn.none_found')}</span>`;
    return;
  }
  box.innerHTML = `<div class="row">${servers.map(s => `<button class="btn btn-secondary btn-sm" ${act('jellyfinPick', s.url)}>${esc(s.name)} <span class="t3">· ${esc(s.version)}</span></button>`).join('')}</div>`;
  const input = document.getElementById('s-jellyfin-url');
  if (servers.length === 1 && input && !input.value.trim()) input.value = servers[0].url;
}

export function jellyfinPick(url) {
  const input = document.getElementById('s-jellyfin-url');
  if (input) input.value = url;
}

function _url() { return document.getElementById('s-jellyfin-url')?.value.trim() || ''; }

function _finish(r) {
  _stopPoll();
  if (_onConnected) _onConnected({ticket: r.ticket, url: r.url, server_name: r.server_name,
                                  version: r.version, admin: r.admin});
}

export async function jellyfinConnectStart() {
  const url = _url();
  if (!url) { document.getElementById('s-jellyfin-url')?.focus(); return; }
  const box = document.getElementById('jf-setup-qc');
  const code = document.getElementById('jf-setup-code');
  const status = document.getElementById('jf-setup-status');
  let data;
  try { data = await api('/api/setup/jellyfin/connect', 'POST', {url}); }
  catch (e) {
    box.hidden = false;
    code.textContent = '';
    status.textContent = _detail(e, t('jfconn.qc_failed', {error: _errMsg(e)}));
    return;
  }
  const c = String(data.code || '');
  box.hidden = false;
  code.textContent = c.length === 6 ? `${c.slice(0, 3)} ${c.slice(3)}` : c;
  status.textContent = t('jfconn.waiting');
  _stopPoll();
  const expiresAt = Date.now() + (data.expires_in || 600) * 1000;
  let misses = 0;
  const stop = (note) => { _stopPoll(); if (note) status.textContent = note; };
  _poll = setInterval(async () => {
    if (!document.getElementById('jf-setup-status')) { _stopPoll(); return; }   // the step was left
    if (Date.now() > expiresAt) { stop(t('jfconn.expired')); return; }
    try {
      const r = await api(`/api/setup/jellyfin/connect/${encodeURIComponent(data.login_id)}`, 'GET', null, false,
                          {'X-Jellyfin-Login-Nonce': data.nonce});
      misses = 0;
      if (r.status === 'ok') _finish(r);
    } catch (e) {
      if (e.status === 403 || (e.status === 502 && misses >= 2)) stop(_detail(e, t('jfconn.failed_restart')));
      else if (++misses >= 3) status.textContent = t('jfconn.not_answering');
    }
  }, POLL_MS);
}

export function jellyfinShowPassword() {
  const form = document.getElementById('jf-setup-pw');
  if (form) { form.hidden = false; document.getElementById('jf-setup-user')?.focus(); }
}

export async function jellyfinPasswordConnect(ev) {
  if (ev) ev.preventDefault();
  const url = _url();
  const user = document.getElementById('jf-setup-user');
  const pass = document.getElementById('jf-setup-pass');
  const btn = document.getElementById('jf-setup-pw-btn');
  if (!url || !user.value.trim()) return;
  btn.disabled = true;
  try {
    const r = await api('/api/setup/jellyfin/connect-password', 'POST',
                        {url, username: user.value.trim(), password: pass.value});
    pass.value = '';
    _finish(r);
  } catch (e) {
    pass.value = '';
    const status = document.getElementById('jf-setup-status');
    document.getElementById('jf-setup-qc').hidden = false;
    document.getElementById('jf-setup-code').textContent = '';
    status.textContent = _detail(e, t('jfconn.failed', {error: _errMsg(e)}));
  } finally {
    btn.disabled = false;
  }
}

export function stopJellyfinConnect() { _stopPoll(); }
