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

function _nodeState(pc) {
  if (pc.available && !pc.busy && !pc.draining) return ['success', 'free'];
  if (pc.available && pc.busy) return ['amber', `busy — ${pc.busy_reason || 'its card is taken'}`];
  if (pc.draining) return ['amber', 'going away'];
  return ['muted', `away${pc.reason ? ` — ${pc.reason}` : ''}`];
}

function _buildLine(b) {
  if (!b) return '<span class="t3">Update state: not reported (an older agent, or the node is away)</span>';
  // When the agent last looked: its answer is cached between checks, so
  // "up to date" is only as fresh as this.
  const at = b.checked_at ? ` <span class="t3">· checked ${esc(_fmtRel(new Date(b.checked_at * 1000).toISOString()))}</span>` : '';
  // A failed check has no count: say why instead of claiming "up to date".
  if (b.behind == null) return `<span class="t3">Update state unknown — ${esc(b.reason || 'the check has not run yet')}</span>${at}`;
  if (!b.behind) return `<span class="t3">Up to date with main</span>${at}`;
  const n = `${b.behind} commit${b.behind === 1 ? '' : 's'} behind main`;
  return b.can_update
    ? `<span>${esc(n)}</span> <button type="button" class="btn btn-secondary btn-sm" ${act('updateNode')}>Update</button>`
    : `<span>${esc(n)} — cannot update itself: ${esc(b.reason || 'unknown')}. Run git pull in its Curatarr folder.</span>`;
}

export async function loadNodes() {
  const box = document.getElementById('maint-nodes');
  if (!box) return;
  let d;
  try { d = await api('/api/hosts'); }
  catch (e) { box.innerHTML = _errHtml(e, act('loadNodes')); return; }
  const pc = d.pc || {};
  const server = `<div class="panel-item"><div class="panel-item-head"><span class="panel-item-title">This server</span></div>
      <div class="panel-item-sub">Runs enrichment and embeddings, and everything else while no node is free.</div>
      <div class="panel-item-foot fs-12" id="card-now-nas"></div></div>`;
  if (!pc.configured) {
    box.innerHTML = server + '<p class="fs-12 t3 mt-8">No node is set up. With PC_AGENT_URL and PC_AGENT_TOKEN in the .env, a second machine lends its card for chat, background curator work and the deletion run.</p>';
    loadCards();
    return;
  }
  const [tone, text] = _nodeState(pc);
  const seen = pc.seen_at ? ` · last report ${esc(_fmtRel(new Date(pc.seen_at * 1000).toISOString()))}` : '';
  box.innerHTML = server + `<div class="panel-item">
      <div class="panel-item-head"><span class="panel-item-title">${esc(pc.name)} <span class="badge ${tone} badge-sm">${esc(text)}</span></span></div>
      <div class="panel-item-sub">Lends its card for chat, background curator work and the deletion run while it is free${seen}.</div>
      <div class="panel-item-foot row">
        <input id="node-name" class="input grow" maxlength="40" value="${escAttr(pc.name_custom || '')}"
               placeholder="${escAttr(pc.name_reported || 'Name this node')}" aria-label="Node name">
        <button type="button" class="btn btn-secondary btn-sm" ${act('saveNodeName', '$el')}>Rename</button>
      </div>
      <div class="panel-item-foot fs-12" id="card-now-pc"></div>
      <div class="panel-item-foot fs-12">${_buildLine(pc.build)}</div>
    </div>`;
  loadCards();
}

const _gb = mb => (mb / 1024).toFixed(1);

function _laneLine(l) {
  const q = l.queued ? ` · ${l.queued} waiting${l.queued_interactive ? ` (${l.queued_interactive} chat)` : ''}` : ' · nobody waiting';
  return l.holder ? `Working: ${esc(l.holder)}${q}` : `Idle${q}`;
}

function _vramLine(v) {
  if (!v) return '';
  return ` · card ${_gb(v.used_mb)} of ${_gb(v.total_mb)} GB${v.util != null ? `, ${v.util} %` : ''}`;
}

function _memLine(m) {
  if (!m) return '';
  const parts = [];
  if (m.used_mb != null) parts.push(m.limit_mb ? `container ${_gb(m.used_mb)} of ${_gb(m.limit_mb)} GB` : `container ${_gb(m.used_mb)} GB`);
  if (m.rss_mb != null) parts.push(`Curatarr ${_gb(m.rss_mb)} GB`);
  if (m.free_mb != null) parts.push(`${_gb(m.free_mb)} GB free`);
  return parts.length ? `<div class="t3">Memory: ${parts.join(' · ')}</div>` : '';
}

const _MODE = {game: 'a game holds the card', cpu: 'summaries on the CPU', paused: 'model work paused'};

function _nasNow(c) {
  const mode = _MODE[c.mode] ? ` <span class="badge amber badge-sm">${esc(_MODE[c.mode])}</span>` : '';
  return `<div>${_laneLine(c.lane)}${_vramLine(c.vram)}${mode}</div>${_memLine(c.memory)}`;
}

function _pcNow(c) {
  if (c.state === 'away') return '<span class="t3">Away: no lane, no card</span>';
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
  catch (e) { nas.innerHTML = `<span class="t3">Now: unknown — ${esc(_errMsg(e))}</span>`; return; }
  nas.innerHTML = _nasNow(d.nas);
  const pc = document.getElementById('card-now-pc');
  if (pc && d.pc) pc.innerHTML = _pcNow(d.pc);
  _cardsTimer = setTimeout(loadCards, 5000);
}

export async function saveNodeName(btn) {
  const input = document.getElementById('node-name');
  if (!input) return;
  btnBusy(btn, 'Saving…');
  try {
    const r = await api('/api/hosts/pc/name', 'PUT', {name: input.value});
    btnDone(btn, 'Saved');
    toast(r.name_custom ? `The node is called ${r.name} now.` : `The node goes by its own name again: ${r.name}.`, 'success');
    loadNodes();
  } catch (e) {
    btnDone(btn, 'Rename');
    toast(`Could not rename the node — ${_errMsg(e)}`, 'danger');
  }
}

// The bell's "Update <node>" and the button above: the node's agent
// fast-forwards its checkout to main and restarts itself.
export async function updateNode() {
  toast('Asking the node to update…', 'info');
  try {
    const r = await api('/api/hosts/pc/update', 'POST');
    const name = r.name || 'The node';
    toast(r.updated ? `${name} updated to ${String(r.to || '').slice(0, 7)} and restarts its agent.`
                    : `${name} is already up to date.`, 'success');
  } catch (e) {
    toast(`The node did not update — ${_errMsg(e)}`, 'danger', {ms: 9000});
  }
  loadUnreadMessages();
  loadNodes();
}
