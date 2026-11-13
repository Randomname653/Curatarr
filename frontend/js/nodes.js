// ── COMPUTE NODES ────────────────────────────────────────────────────────────
// Settings → Maintenance: this server and the node that lends its card
// (GET /api/hosts). The node is named like a Tdarr node: it reports a name
// of its own (PC_AGENT_NAME, else its computer name) and the owner can rename
// it here. Its update state comes from its agent; "Update" asks it to
// fast-forward and restart (the same call as the bell's button).
// Under each card, what it does right now (GET /api/hosts/cards): the lane's
// holder and queue, the card's memory, on this server the container's.
import { api } from './api.js';
import { _errHtml, _errMsg, _fmtRel, act, btnBusy, btnDone, esc, escAttr, toast } from './ui.js';
import { loadUnreadMessages } from './notifications.js';
import { t, th } from './i18n.js';

function _nodeState(pc) {
  if (pc.available && !pc.busy && !pc.draining) return ['success', t('nodes.free')];
  if (pc.available && pc.busy) return ['amber', t('nodes.busy', {reason: pc.busy_reason || t('nodes.card_taken')})];
  if (pc.draining) return ['amber', t('nodes.going_away')];
  return ['muted', `${t('nodes.away')}${pc.reason ? ` — ${pc.reason}` : ''}`];
}

function _buildLine(b) {
  if (!b) return `<span class="t3">${th('nodes.update_not_reported')}</span>`;
  // When the agent last looked: its answer is cached between checks, so
  // "up to date" is only as fresh as this.
  const at = b.checked_at ? ` <span class="t3">· ${th('nodes.checked', {when: _fmtRel(new Date(b.checked_at * 1000).toISOString())})}</span>` : '';
  // A failed check has no count: say why instead of claiming "up to date".
  if (b.behind == null) return `<span class="t3">${th('nodes.update_unknown', {reason: b.reason || t('nodes.not_run_yet')})}</span>${at}`;
  if (!b.behind) return `<span class="t3">${th('nodes.up_to_date')}</span>${at}`;
  const n = t('nodes.behind', {n: b.behind});
  return b.can_update
    ? `<span>${esc(n)}</span> <button type="button" class="btn btn-secondary btn-sm" ${act('updateNode')}>${th('nodes.update')}</button>`
    : `<span>${esc(n)} ${th('nodes.cannot_update', {reason: b.reason || t('nodes.unknown')})}</span>`;
}

export async function loadNodes() {
  const box = document.getElementById('maint-nodes');
  if (!box) return;
  let d;
  try { d = await api('/api/hosts'); }
  catch (e) { box.innerHTML = _errHtml(e, act('loadNodes')); return; }
  const pc = d.pc || {};
  const server = `<div class="panel-item"><div class="panel-item-head"><span class="panel-item-title">${th('nodes.this_server')}</span></div>
      <div class="panel-item-sub">${th('nodes.this_server_hint')}</div>
      <div class="panel-item-foot fs-12" id="card-now-nas"></div></div>`;
  if (!pc.configured) {
    box.innerHTML = server + `<p class="fs-12 t3 mt-8">${th('nodes.none_set_up')}</p>`;
    loadCards();
    return;
  }
  const [tone, text] = _nodeState(pc);
  const seen = pc.seen_at ? ` · ${th('nodes.last_report', {when: _fmtRel(new Date(pc.seen_at * 1000).toISOString())})}` : '';
  box.innerHTML = server + `<div class="panel-item">
      <div class="panel-item-head"><span class="panel-item-title">${esc(pc.name)} <span class="badge ${tone} badge-sm">${esc(text)}</span></span></div>
      <div class="panel-item-sub">${th('nodes.lends')}${seen}.</div>
      <div class="panel-item-foot row">
        <input id="node-name" class="input grow" maxlength="40" value="${escAttr(pc.name_custom || '')}"
               placeholder="${escAttr(pc.name_reported || t('nodes.name_placeholder'))}" aria-label="${th('nodes.name_aria')}">
        <button type="button" class="btn btn-secondary btn-sm" ${act('saveNodeName', '$el')}>${th('nodes.rename')}</button>
      </div>
      <div class="panel-item-foot fs-12" id="card-now-pc"></div>
      <div class="panel-item-foot fs-12">${_buildLine(pc.build)}</div>
    </div>`;
  loadCards();
}

const _gb = mb => (mb / 1024).toFixed(1);

function _laneLine(l) {
  const q = l.queued ? ` · ${esc(t('nodes.waiting', {n: l.queued}))}${l.queued_interactive ? ` ${esc(t('nodes.chat', {n: l.queued_interactive}))}` : ''}` : ` · ${esc(t('nodes.nobody_waiting'))}`;
  return l.holder ? `${th('nodes.working', {holder: l.holder})}${q}` : `${th('nodes.idle')}${q}`;
}

function _vramLine(v) {
  if (!v) return '';
  return ` · ${th('nodes.card', {used: _gb(v.used_mb), total: _gb(v.total_mb)})}${v.util != null ? `, ${v.util} %` : ''}`;
}

function _memLine(m) {
  if (!m) return '';
  const parts = [];
  if (m.used_mb != null) parts.push(m.limit_mb ? th('nodes.container_of', {used: _gb(m.used_mb), limit: _gb(m.limit_mb)}) : th('nodes.container', {used: _gb(m.used_mb)}));
  if (m.rss_mb != null) parts.push(`Curatarr ${_gb(m.rss_mb)} GB`);
  if (m.free_mb != null) parts.push(th('nodes.gb_free', {gb: _gb(m.free_mb)}));
  return parts.length ? `<div class="t3">${th('nodes.memory')} ${parts.join(' · ')}</div>` : '';
}

const _MODE = {game: 'nodes.mode_game', cpu: 'nodes.mode_cpu', paused: 'nodes.mode_paused'};

function _nasNow(c) {
  const mode = _MODE[c.mode] ? ` <span class="badge amber badge-sm">${th(_MODE[c.mode])}</span>` : '';
  return `<div>${_laneLine(c.lane)}${_vramLine(c.vram)}${mode}</div>${_memLine(c.memory)}`;
}

function _pcNow(c) {
  if (c.state === 'away') return `<span class="t3">${th('nodes.away_no_lane')}</span>`;
  const game = c.game ? ` <span class="badge amber badge-sm">${esc(c.game)}</span>` : '';
  return `<div>${_laneLine(c.lane)}${_vramLine(c.vram)}${game}</div>`;
}

// Re-read every 5 s while the section is on screen; stops by itself once
// the pane is left (the next loadNodes starts it again).
let _cardsTimer = null;
export async function loadCards() {
  clearTimeout(_cardsTimer);
  _cardsTimer = null;
  const nas = document.getElementById('card-now-nas');
  if (!nas || nas.offsetParent === null) return;
  let d;
  try { d = await api('/api/hosts/cards'); }
  catch (e) { nas.innerHTML = `<span class="t3">${th('nodes.now_unknown', {error: _errMsg(e)})}</span>`; return; }
  nas.innerHTML = _nasNow(d.nas);
  const pc = document.getElementById('card-now-pc');
  if (pc && d.pc) pc.innerHTML = _pcNow(d.pc);
  _cardsTimer = setTimeout(loadCards, 5000);
}

export async function saveNodeName(btn) {
  const input = document.getElementById('node-name');
  if (!input) return;
  btnBusy(btn, t('nodes.saving'));
  try {
    const r = await api('/api/hosts/pc/name', 'PUT', {name: input.value});
    btnDone(btn, t('common.saved'));
    toast(t(r.name_custom ? 'nodes.renamed' : 'nodes.own_name', {name: r.name}), 'success');
    loadNodes();
  } catch (e) {
    btnDone(btn, t('nodes.rename'));
    toast(t('nodes.rename_failed', {error: _errMsg(e)}), 'danger');
  }
}

// The bell's "Update <node>" and the button above: the node's agent
// fast-forwards its checkout to main and restarts itself.
export async function updateNode() {
  toast(t('nodes.asking_update'), 'info');
  try {
    const r = await api('/api/hosts/pc/update', 'POST');
    const name = r.name || t('nodes.the_node');
    toast(r.updated ? t('nodes.updated', {name, sha: String(r.to || '').slice(0, 7)})
                    : t('nodes.already_current', {name}), 'success');
  } catch (e) {
    toast(t('nodes.update_failed', {error: _errMsg(e)}), 'danger', {ms: 9000});
  }
  loadUnreadMessages();
  loadNodes();
}
