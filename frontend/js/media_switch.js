// ── SETTINGS → MEDIA SERVER ──────────────────────────────────────────────────
// Which server this install runs on, connecting the other one, and moving
// over with the curation kept whole - Plex to Jellyfin and back again
// (routers/setup.py /media-server, services/media_server/switch.py).
// Check first (both libraries walked, nothing written), then the switch
// with the admin's picks for users and libraries without a match.
import { EL, EVENT, _errHtml, _errMsg, act, btnBusy, btnDone, confirmDialog, esc, escAttr, toast } from './ui.js';
import { api } from './api.js';
import { findJellyfinServers, jellyfinConnectHtml, stopJellyfinConnect } from './jellyfin_connect.js';

const LABEL = {plex: 'Plex', jellyfin: 'Jellyfin'};
const HOW = {id_memory: 'known from before', name: 'same name', user_flag: 'your pick'};
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
      <div class="section-head"><h3>Runs on ${esc(LABEL[s.active])}</h3><span class="section-hint">Members sign in with their ${esc(LABEL[s.active])} accounts; history, playlists and collections go through it.</span></div>
      <div class="section-body">${_connectionsHtml(s)}</div>
    </section>
    <section class="section">
      <div class="section-head"><h3>Move to ${esc(LABEL[target])}</h3><span class="section-hint">Watch history, ratings, verdicts, recommendations and open deletion proposals move with you; nothing is lost on the way back either.</span></div>
      <div class="section-body" id="ms-move">${_moveHtml(s, target)}</div>
    </section>
    ${s.undo && !['checking', 'switching'].includes(s.phase) ? `<details class="section">
      <summary>Emergency exit: undo the last switch</summary>
      <div class="section-body">
        <p class="fs-12 t2 lh-15 mb-8">Replays the switch from ${esc(LABEL[s.undo.from] || s.undo.from)} to ${esc(LABEL[s.undo.to] || s.undo.to)} backwards, plays recorded since included. The normal way back is another move, which also carries this period's plays over.</p>
        <button class="btn btn-danger btn-sm" ${act('mediaServerUndo', EL)}>Undo the last switch</button>
      </div>
    </details>` : ''}`;
  if (target === 'jellyfin' && !s.configured.jellyfin) findJellyfinServers();
}

function _connectionsHtml(s) {
  const jf = s.configured.jellyfin;
  const plex = s.configured.plex;
  const rows = [
    `<div class="row fs-13"><b>Plex</b><span class="badge ${plex ? 'success' : 'muted'}">${plex ? 'connected' : 'not set up'}</span>${plex ? '' : '<span class="fs-12 t3">Address and token: Settings → Integrations → Plex</span>'}</div>`,
    `<div class="row fs-13 mt-8"><b>Jellyfin</b><span class="badge ${jf ? 'success' : 'muted'}">${jf ? 'connected' : 'not connected'}</span>${jf ? `<button class="btn btn-secondary btn-sm" ${act('mediaServerReconnect')}>Connect again</button>` : ''}</div>`,
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
    toast(`Jellyfin connected: ${c.server_name || c.url}`, 'success');
    _reconnect = false;
    loadMediaServer();
  } catch (e) {
    toast(`Couldn't save the Jellyfin connection — ${_errMsg(e)}`, 'danger');
  }
}

function _moveHtml(s, target) {
  if (!s.configured[target]) {
    return `<p class="fs-12 t3">Connect ${esc(LABEL[target])} above first${target === 'plex' ? ' (Settings → Integrations → Plex)' : ''}.</p>`;
  }
  if (s.phase === 'checking' || s.phase === 'switching') return `<div id="ms-progress">${_progressHtml(s)}</div>`;
  if (s.phase === 'done') return _doneHtml(s);
  const failed = s.phase === 'failed' ? `<div class="test-status err mb-8">${esc(s.error || 'Failed')}</div>` : '';
  if (s.phase === 'ready' && s.target === target && s.report) return failed + _reportHtml(s.report, s);
  return `${failed}<p class="fs-12 t2 lh-15 mb-8">First a check: both servers' libraries are read and every title Curatarr knows is matched to ${esc(LABEL[target])}'s copy — by its ids, then by its file. Nothing changes yet; it takes a few minutes on a large library.</p>
    <button class="btn btn-primary btn-sm" ${act('mediaServerCheck', target, EL)}>Check the move to ${esc(LABEL[target])}</button>`;
}

function _progressHtml(s) {
  const text = s.phase === 'checking'
    ? 'Checking — reading both libraries, nothing changes yet. Follow it under Activity.'
    : 'Moving — follow it under Activity. Syncs wait until it is done.';
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
  const tables = Object.entries(r.tables || {}).filter(([, t]) => t.matched || t.unmatched)
    .map(([name, t]) => `<tr><td class="fs-12"><code>${esc(name)}</code></td><td class="fs-12">${t.matched}</td><td class="fs-12 t3">${t.unmatched}</td></tr>`).join('');
  const users = Object.entries(r.users || {}).map(([, u]) => `<li class="fs-12">${esc(u.name || u.from)} <span class="t3">→ their ${esc(LABEL[r.to])} account (${esc(HOW[u.how] || u.how)})</span></li>`).join('');
  const userOpts = (r.target_users || []).map(n => `<option value="${escAttr(n)}">${esc(n)}</option>`).join('');
  const gapsU = (r.users_unmatched || []).map((n, i) => `<div class="row mt-4">
      <span class="fs-13 grow">${esc(n)}</span>
      <select class="field-sm" id="ms-user-${i}" data-name="${escAttr(n)}" aria-label="Account of ${escAttr(n)} on ${escAttr(LABEL[r.to])}">
        <option value="">pick their account…</option>${userOpts}<option value="__skip">leave out</option>
      </select></div>`).join('');
  const libOpts = kind => (r.target_libraries || []).filter(l => l.kind === kind)
    .map(l => `<option value="${escAttr(l.key)}">${esc(l.title)}</option>`).join('');
  const gapsL = (r.library_gaps || []).map((l, i) => `<div class="row mt-4">
      <span class="fs-13 grow">${esc(l.title)} <span class="t3 fs-12">${esc(l.kind)}</span></span>
      <select class="field-sm" id="ms-lib-${i}" data-key="${escAttr(l.key)}" aria-label="Library for ${escAttr(l.title)} on ${escAttr(LABEL[r.to])}">
        <option value="">no counterpart</option>${libOpts(l.kind)}
      </select></div>`).join('');
  const sample = (r.unmatched_sample || []).slice(0, 10).map(u => `<li class="fs-12">${esc(u.title || u.key)}</li>`).join('');
  return `
    ${r.source_walked ? '' : `<p class="t-amber fs-12 mb-8">${esc(LABEL[r.from])} did not answer: matched from Curatarr's id memory alone.</p>`}
    <p class="fs-13"><b>${it.matched}</b> titles matched <span class="t3 fs-12">(${methods || '—'})</span>, <b>${it.unmatched}</b> without a match${it.conflicts ? `, ${it.conflicts} conflicts` : ''}.</p>
    ${sample ? `<details class="mt-4"><summary class="fs-12 t2 pointer">Without a match (first ${Math.min(10, it.unmatched)})</summary><ul class="mt-4">${sample}</ul><p class="fs-11 t3">Usually titles no longer in the library; their history stays and finds its key again with a later move.</p></details>` : ''}
    ${it.suggestions ? `<label class="row fs-13 mt-8"><input type="checkbox" id="ms-suggest"${opts.accept_suggestions ? ' checked' : ''}> Also move ${it.suggestions} title(s) matched by title and year only</label>` : ''}
    ${r.users_listed === false ? `<p class="t-amber fs-12 mt-8">${esc(LABEL[r.to])} did not list its accounts: users are matched from Curatarr's memory of their earlier ${esc(LABEL[r.to])} accounts.</p>` : ''}
    <div class="mt-12"><b class="fs-13">Users</b><ul class="mt-4">${users || '<li class="fs-12 t3">none matched yet</li>'}</ul>${gapsU}</div>
    <div class="mt-12"><b class="fs-13">Libraries</b><p class="fs-12 t2">${esc(Object.values(r.libraries || {}).join(', ') || '—')}</p>${gapsL}</div>
    <details class="mt-8"><summary class="fs-12 t2 pointer">Rows per table</summary>
      <div class="tbl-wrap mt-4"><table class="tbl"><thead><tr><th>Table</th><th>moves</th><th>stays</th></tr></thead><tbody>${tables}</tbody></table></div></details>
    <label class="row fs-13 mt-12"><input type="checkbox" id="ms-sync"${opts.sync_state === false ? '' : ' checked'}> Mark what was watched as played on ${esc(LABEL[r.to])}</label>
    <div class="fs-11 t3 lh-15">So ${esc(LABEL[r.to])}'s own "unwatched" and "next episode" agree with your history.${r.to === 'plex' ? ' Per member, for those who signed in with Plex before.' : ''}</div>
    ${(r.blocking || []).length ? `<p class="t-amber fs-12 mt-8">${esc(r.blocking.join('; '))} — pick their account or "leave out" above.</p>` : ''}
    <div class="row mt-12">
      <button class="btn btn-primary btn-sm" ${act('mediaServerSwitch', r.to, EL)}>Move to ${esc(LABEL[r.to])}</button>
      <button class="btn btn-secondary btn-sm" ${act('mediaServerCheck', r.to, EL)}>Check again with these picks</button>
    </div>`;
}

function _doneHtml(s) {
  const res = s.result || {};
  const sync = res.sync_state ? Object.entries(res.sync_state).map(([k, v]) => `${esc(k.replace(/_/g, ' '))} ${esc(String(v))}`).join(' · ') : '';
  return `<p class="fs-13">${res.undone ? 'The last switch was undone.' : `Done: ${res.items ?? 0} titles, ${res.vectors ?? 0} vectors and ${res.proposals ?? 0} open proposals moved${res.collisions ? `, ${res.collisions} rows left on an old key` : ''}.`}</p>
    ${sync ? `<p class="fs-12 t2 mt-4">Played marks: ${sync}</p>` : ''}
    <p class="fs-12 t2 mt-8">Reload the page: the sign-in, the labels and the libraries now follow ${esc(LABEL[s.active])}. Check the library mapping under Settings → ${esc(LABEL[s.active])} libraries.</p>
    <div class="row mt-8"><button class="btn btn-primary btn-sm" ${act('reloadPage', EVENT)}>Reload</button>
      <button class="btn btn-secondary btn-sm" ${act('mediaServerForget')}>Close</button></div>`;
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
  btnBusy(btn, 'Starting…');
  try { _status = await api('/api/setup/media-server/check', 'POST', {target, options}); }
  catch (e) { toast(`Couldn't start the check — ${_errMsg(e)}`, 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerSwitch(target, btn) {
  const options = _picks();
  const r = _status?.report;
  const ok = await confirmDialog({
    title: `Move Curatarr to ${LABEL[target]}?`,
    body: `<p class="fs-13 lh-15">${r ? `${r.items.matched} titles` : 'Every matched title'} and everything curated about them move to ${esc(LABEL[target])}. The database is backed up first; syncs wait until it is done. Members then sign in with their ${esc(LABEL[target])} accounts.</p>`,
    confirmLabel: `Move to ${LABEL[target]}`,
  });
  if (!ok.ok) return;
  btnBusy(btn, 'Starting…');
  try { _status = await api('/api/setup/media-server/switch', 'POST', {target, options}); }
  catch (e) { toast(`Couldn't start the move — ${_errMsg(e)}`, 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerUndo(btn) {
  const u = _status?.undo;
  const ok = await confirmDialog({
    title: 'Undo the last switch?', danger: true, countdown: 3,
    body: `<p class="fs-13 lh-15">Curatarr goes back to ${esc(LABEL[u?.from] || 'the old server')} as it was before the switch. Prefer a normal move if you used ${esc(LABEL[u?.to] || 'the new server')} for a while: it carries those plays over as well.</p>`,
    confirmLabel: 'Undo',
  });
  if (!ok.ok) return;
  btnBusy(btn, 'Starting…');
  try { _status = await api('/api/setup/media-server/undo', 'POST'); }
  catch (e) { toast(`Couldn't start the undo — ${_errMsg(e)}`, 'danger'); btnDone(btn); return; }
  _render();
  _startPoll();
}

export async function mediaServerForget() {
  try { _status = await api('/api/setup/media-server/forget', 'POST'); } catch { /* the reload below shows the truth */ }
  loadMediaServer();
}

