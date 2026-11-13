// ── SETTINGS → MEDIA SERVER ──────────────────────────────────────────────────
// Which server this install runs on, connecting the other one, and moving
// over with the curation kept whole - Plex to Jellyfin and back again
// (routers/setup.py /media-server, services/media_server/switch.py).
// Check first (both libraries walked, nothing written), then the switch
// with the admin's picks for users and libraries without a match.
import { EL, EVENT, _errHtml, _errMsg, act, btnBusy, btnDone, confirmDialog, esc, escAttr, toast } from './ui.js';
import { api } from './api.js';
import { t, th } from './i18n.js';
import { findJellyfinServers, jellyfinConnectHtml, stopJellyfinConnect } from './jellyfin_connect.js';

const LABEL = {plex: 'Plex', jellyfin: 'Jellyfin'};
const HOW = {id_memory: 'mswitch.how_memory', name: 'mswitch.how_name', user_flag: 'mswitch.how_pick'};
const POLL_MS = 2000;
let _poll = null;
let _status = null;

function _shell() { return document.getElementById('media-server-shell'); }
function _stop() { if (_poll) { clearInterval(_poll); _poll = null; } }

export async function loadMediaServer() {
  const shell = _shell();
  if (!shell) return;
  _stop();
  try { _status = await api('/api/setup/media-server'); }
  catch (e) { shell.innerHTML = _errHtml(e, act('loadMediaServer')); return; }
  _render();
  if (['checking', 'switching'].includes(_status.phase)) _startPoll();
}

function _startPoll() {
  _stop();
  _poll = setInterval(async () => {
    if (!_shell() || _shell().closest('[hidden]')) { _stop(); return; }
    try { _status = await api('/api/setup/media-server'); }
    catch { return; }                       // a restart in between: the next tick asks again
    if (!['checking', 'switching'].includes(_status.phase)) { _stop(); _render(); }
    else _renderProgress();
  }, POLL_MS);
}

function _other(kind) { return kind === 'jellyfin' ? 'plex' : 'jellyfin'; }

function _render() {
  const s = _status;
  const target = _other(s.active);
  const shell = _shell();
  stopJellyfinConnect();
  shell.innerHTML = `
    <section class="section">
      <div class="section-head"><h3>${th('mswitch.runs_on', {server: LABEL[s.active]})}</h3><span class="section-hint">${th('mswitch.runs_on_hint', {server: LABEL[s.active]})}</span></div>
      <div class="section-body">${_connectionsHtml(s)}</div>
    </section>
    <section class="section">
      <div class="section-head"><h3>${th('mswitch.move_to', {server: LABEL[target]})}</h3><span class="section-hint">${th('mswitch.move_hint')}</span></div>
      <div class="section-body" id="ms-move">${_moveHtml(s, target)}</div>
    </section>
    ${s.undo && !['checking', 'switching'].includes(s.phase) ? `<details class="section">
      <summary>${th('mswitch.emergency')}</summary>
      <div class="section-body">
        <p class="fs-12 t2 lh-15 mb-8">${th('mswitch.undo_hint', {from: LABEL[s.undo.from] || s.undo.from, to: LABEL[s.undo.to] || s.undo.to})}</p>
        <button class="btn btn-danger btn-sm" ${act('mediaServerUndo', EL)}>${th('mswitch.undo_last')}</button>
      </div>
    </details>` : ''}`;
  if (target === 'jellyfin' && !s.configured.jellyfin) findJellyfinServers();
}

function _connectionsHtml(s) {
  const jf = s.configured.jellyfin;
  const plex = s.configured.plex;
  const rows = [
    `<div class="row fs-13"><b>Plex</b><span class="badge ${plex ? 'success' : 'muted'}">${th(plex ? 'mswitch.connected' : 'mswitch.not_set_up')}</span>${plex ? '' : `<span class="fs-12 t3">${th('mswitch.plex_where')}</span>`}</div>`,
    `<div class="row fs-13 mt-8"><b>Jellyfin</b><span class="badge ${jf ? 'success' : 'muted'}">${th(jf ? 'mswitch.connected' : 'mswitch.not_connected')}</span>${jf ? `<button class="btn btn-secondary btn-sm" ${act('mediaServerReconnect')}>${th('mswitch.connect_again')}</button>` : ''}</div>`,
  ];
  const box = !jf || _reconnect
    ? `<div class="mt-12" id="ms-jf-connect">${jellyfinConnectHtml('', null, _saveJellyfin)}</div>` : '';
  return rows.join('') + box;
}

let _reconnect = false;
export function mediaServerReconnect() {
  _reconnect = true;
  _render();
  findJellyfinServers();
}

async function _saveJellyfin(c) {
  try {
    await api('/api/setup/jellyfin/save', 'POST', {ticket: c.ticket});
    toast(t('mswitch.jf_connected', {server: c.server_name || c.url}), 'success');
    _reconnect = false;
    loadMediaServer();
  } catch (e) {
    toast(t('mswitch.jf_not_saved', {error: _errMsg(e)}), 'danger');
  }
}

function _moveHtml(s, target) {
  if (!s.configured[target]) {
    return `<p class="fs-12 t3">${th(target === 'plex' ? 'mswitch.connect_first_plex' : 'mswitch.connect_first', {server: LABEL[target]})}</p>`;
  }
  if (s.phase === 'checking' || s.phase === 'switching') return `<div id="ms-progress">${_progressHtml(s)}</div>`;
  if (s.phase === 'done') return _doneHtml(s);
  const failed = s.phase === 'failed' ? `<div class="test-status err mb-8">${esc(s.error || t('mswitch.failed'))}</div>` : '';
  if (s.phase === 'ready' && s.target === target && s.report) return failed + _reportHtml(s.report, s);
  return `${failed}<p class="fs-12 t2 lh-15 mb-8">${th('mswitch.check_hint', {server: LABEL[target]})}</p>
    <button class="btn btn-primary btn-sm" ${act('mediaServerCheck', target, EL)}>${th('mswitch.check_move', {server: LABEL[target]})}</button>`;
}

function _progressHtml(s) {
  const text = th(s.phase === 'checking' ? 'mswitch.checking' : 'mswitch.moving');
  return `<p class="fs-13 t2" role="status" aria-live="polite">${text}</p>`;
}

function _renderProgress() {
  const el = document.getElementById('ms-progress');
  if (el) el.innerHTML = _progressHtml(_status);
}

function _reportHtml(r, s) {
  const it = r.items;
  const opts = s.options || {};
  const methods = Object.entries(it.by_method || {}).map(([m, n]) => `${esc(m)} ${n}`).join(' · ');
  const tables = Object.entries(r.tables || {}).filter(([, tb]) => tb.matched || tb.unmatched)
    .map(([name, tb]) => `<tr><td class="fs-12"><code>${esc(name)}</code></td><td class="fs-12">${tb.matched}</td><td class="fs-12 t3">${tb.unmatched}</td></tr>`).join('');
  const users = Object.entries(r.users || {}).map(([, u]) => `<li class="fs-12">${esc(u.name || u.from)} <span class="t3">${th('mswitch.their_account', {server: LABEL[r.to], how: HOW[u.how] ? t(HOW[u.how]) : u.how})}</span></li>`).join('');
  const userOpts = (r.target_users || []).map(n => `<option value="${escAttr(n)}">${esc(n)}</option>`).join('');
  const gapsU = (r.users_unmatched || []).map((n, i) => `<div class="row mt-4">
      <span class="fs-13 grow">${esc(n)}</span>
      <select class="field-sm" id="ms-user-${i}" data-name="${escAttr(n)}" aria-label="${th('mswitch.account_of', {name: n, server: LABEL[r.to]})}">
        <option value="">${th('mswitch.pick_account')}</option>${userOpts}<option value="__skip">${th('mswitch.leave_out')}</option>
      </select></div>`).join('');
  const libOpts = kind => (r.target_libraries || []).filter(l => l.kind === kind)
    .map(l => `<option value="${escAttr(l.key)}">${esc(l.title)}</option>`).join('');
  const gapsL = (r.library_gaps || []).map((l, i) => `<div class="row mt-4">
      <span class="fs-13 grow">${esc(l.title)} <span class="t3 fs-12">${esc(l.kind)}</span></span>
      <select class="field-sm" id="ms-lib-${i}" data-key="${escAttr(l.key)}" aria-label="${th('mswitch.library_for', {title: l.title, server: LABEL[r.to]})}">
        <option value="">${th('mswitch.no_counterpart')}</option>${libOpts(l.kind)}
      </select></div>`).join('');
  const sample = (r.unmatched_sample || []).slice(0, 10).map(u => `<li class="fs-12">${esc(u.title || u.key)}</li>`).join('');
  return `
    ${r.source_walked ? '' : `<p class="t-amber fs-12 mb-8">${th('mswitch.no_answer', {server: LABEL[r.from]})}</p>`}
    <p class="fs-13"><b>${it.matched}</b> ${th('mswitch.titles_matched', {n: it.matched})} <span class="t3 fs-12">(${methods || '—'})</span>, <b>${it.unmatched}</b> ${th('mswitch.without_match')}${it.conflicts ? `, ${th('mswitch.conflicts', {n: it.conflicts})}` : ''}.</p>
    ${sample ? `<details class="mt-4"><summary class="fs-12 t2 pointer">${th('mswitch.without_match_first', {n: Math.min(10, it.unmatched)})}</summary><ul class="mt-4">${sample}</ul><p class="fs-11 t3">${th('mswitch.without_match_hint')}</p></details>` : ''}
    ${it.suggestions ? `<label class="row fs-13 mt-8"><input type="checkbox" id="ms-suggest"${opts.accept_suggestions ? ' checked' : ''}> ${th('mswitch.also_move', {n: it.suggestions})}</label>` : ''}
    ${r.users_listed === false ? `<p class="t-amber fs-12 mt-8">${th('mswitch.no_accounts', {server: LABEL[r.to]})}</p>` : ''}
    <div class="mt-12"><b class="fs-13">${th('mswitch.users')}</b><ul class="mt-4">${users || `<li class="fs-12 t3">${th('mswitch.none_matched')}</li>`}</ul>${gapsU}</div>
    <div class="mt-12"><b class="fs-13">${th('mswitch.libraries')}</b><p class="fs-12 t2">${esc(Object.values(r.libraries || {}).join(', ') || '—')}</p>${gapsL}</div>
    <details class="mt-8"><summary class="fs-12 t2 pointer">${th('mswitch.rows_per_table')}</summary>
      <div class="tbl-wrap mt-4"><table class="tbl"><thead><tr><th>${th('mswitch.table')}</th><th>${th('mswitch.moves')}</th><th>${th('mswitch.stays')}</th></tr></thead><tbody>${tables}</tbody></table></div></details>
    <label class="row fs-13 mt-12"><input type="checkbox" id="ms-sync"${opts.sync_state === false ? '' : ' checked'}> ${th('mswitch.mark_played', {server: LABEL[r.to]})}</label>
    <div class="fs-11 t3 lh-15">${th('mswitch.mark_played_hint', {server: LABEL[r.to]})}${r.to === 'plex' ? ` ${th('mswitch.per_member')}` : ''}</div>
    ${(r.blocking || []).length ? `<p class="t-amber fs-12 mt-8">${esc(r.blocking.join('; '))} ${th('mswitch.blocking_tail')}</p>` : ''}
    <div class="row mt-12">
      <button class="btn btn-primary btn-sm" ${act('mediaServerSwitch', r.to, EL)}>${th('mswitch.move_to', {server: LABEL[r.to]})}</button>
      <button class="btn btn-secondary btn-sm" ${act('mediaServerCheck', r.to, EL)}>${th('mswitch.check_again')}</button>
    </div>`;
}

function _doneHtml(s) {
  const res = s.result || {};
  const sync = res.sync_state ? Object.entries(res.sync_state).map(([k, v]) => `${esc(k.replace(/_/g, ' '))} ${esc(String(v))}`).join(' · ') : '';
  return `<p class="fs-13">${res.undone ? th('mswitch.undone') : `${th('mswitch.done', {items: res.items ?? 0, vectors: res.vectors ?? 0, proposals: res.proposals ?? 0})}${res.collisions ? `, ${th('mswitch.collisions', {n: res.collisions})}` : ''}.`}</p>
    ${sync ? `<p class="fs-12 t2 mt-4">${th('mswitch.played_marks')} ${sync}</p>` : ''}
    <p class="fs-12 t2 mt-8">${th('mswitch.reload_hint', {server: LABEL[s.active]})}</p>
    <div class="row mt-8"><button class="btn btn-primary btn-sm" ${act('reloadPage', EVENT)}>${th('common.reload')}</button>
      <button class="btn btn-secondary btn-sm" ${act('mediaServerForget')}>${th('common.close')}</button></div>`;
}

function _picks() {
  const map_user = {}, skip_users = [], map_library = {};
  document.querySelectorAll('[id^="ms-user-"]').forEach(sel => {
    if (sel.value === '__skip') skip_users.push(sel.dataset.name);
    else if (sel.value) map_user[sel.dataset.name] = sel.value;
  });
  document.querySelectorAll('[id^="ms-lib-"]').forEach(sel => { if (sel.value) map_library[sel.dataset.key] = sel.value; });
  return {map_user, skip_users, map_library,
          accept_suggestions: !!document.getElementById('ms-suggest')?.checked,
          sync_state: document.getElementById('ms-sync') ? document.getElementById('ms-sync').checked : true};
}

export async function mediaServerCheck(target, btn) {
  const options = _status?.phase === 'ready' ? _picks() : {};
  btnBusy(btn, t('mswitch.starting'));
  try { _status = await api('/api/setup/media-server/check', 'POST', {target, options}); }
  catch (e) { toast(t('mswitch.check_failed', {error: _errMsg(e)}), 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerSwitch(target, btn) {
  const options = _picks();
  const r = _status?.report;
  const ok = await confirmDialog({
    title: t('mswitch.confirm_title', {server: LABEL[target]}),
    body: `<p class="fs-13 lh-15">${r ? th('mswitch.confirm_body_n', {n: r.items.matched, server: LABEL[target]}) : th('mswitch.confirm_body_all', {server: LABEL[target]})}</p>`,
    confirmLabel: t('mswitch.move_to', {server: LABEL[target]}),
  });
  if (!ok.ok) return;
  btnBusy(btn, t('mswitch.starting'));
  try { _status = await api('/api/setup/media-server/switch', 'POST', {target, options}); }
  catch (e) { toast(t('mswitch.move_failed', {error: _errMsg(e)}), 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerUndo(btn) {
  const u = _status?.undo;
  const ok = await confirmDialog({
    title: t('mswitch.undo_title'), danger: true, countdown: 3,
    body: `<p class="fs-13 lh-15">${th('mswitch.undo_body', {from: LABEL[u?.from] || t('mswitch.old_server'), to: LABEL[u?.to] || t('mswitch.new_server')})}</p>`,
    confirmLabel: t('mswitch.undo'),
  });
  if (!ok.ok) return;
  btnBusy(btn, t('mswitch.starting'));
  try { _status = await api('/api/setup/media-server/undo', 'POST'); }
  catch (e) { toast(t('mswitch.undo_failed', {error: _errMsg(e)}), 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerForget() {
  try { _status = await api('/api/setup/media-server/forget', 'POST'); } catch { /* the reload below shows the truth */ }
  loadMediaServer();
}

