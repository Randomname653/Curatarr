// ── COMPUTE NODES ────────────────────────────────────────────────────────────
// Settings → Maintenance: this server and the node that lends its card
// (GET /api/hosts). The node is named like a Tdarr node: it reports a name
// of its own (PC_AGENT_NAME, else its computer name) and the owner can rename
// it here. Its update state comes from its agent; "Update" asks it to
// fast-forward and restart (the same call as the bell's button).
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
      <div class="panel-item-sub">Runs enrichment and embeddings, and everything else while no node is free.</div></div>`;
  if (!pc.configured) {
    box.innerHTML = server + '<p class="fs-12 t3 mt-8">No node is set up. With PC_AGENT_URL and PC_AGENT_TOKEN in the .env, a second machine lends its card for chat, background curator work and the deletion run.</p>';
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
      <div class="panel-item-foot fs-12">${_buildLine(pc.build)}</div>
    </div>`;
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
