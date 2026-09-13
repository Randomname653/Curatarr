// ── NOTIFICATION CENTER (bell: proactive messages + system notifications) ────
import { api } from './api.js';
import { state } from './state.js';
import { esc, escAttr } from './ui.js';
import { showView } from './nav.js';
import { _setDiscussBanner, addMsg, sendMessage } from './chat.js';
export async function loadUnreadMessages() {
  try {
    const r = await api('/api/messages/unread');
    // System notifications: learned principles awaiting review (admin only —
    // the endpoint is admin-gated anyway, this just avoids a guaranteed 403).
    let principles = [];
    if (state.currentUser?.is_admin) {
      try {
        const pr = await api('/api/recommendations/principles');
        principles = (pr.principles || []).filter(p => p.status === 'shadow');
      } catch {}
    }
    const badge = document.getElementById('msg-badge');
    const total = (r.total || 0) + principles.length;
    badge.textContent = total;
    badge.classList.toggle('show', total > 0);
    const list = document.getElementById('msg-list');

    let html = '';
    if (principles.length) {
      html += `<div class="mp-section">Principles awaiting review</div>`;
      html += principles.slice(0, 3).map(p => `
        <div class="msg-item" style="cursor:default">
          <div class="mi-text">${p.novelty === 'contradiction' ? '<span class="badge danger" style="font-size:10px;margin-right:4px">contradiction</span>' : ''}${esc(p.text)}</div>
          <div style="display:flex;gap:8px;margin-top:10px">
            <button class="btn btn-primary btn-sm" style="flex:1" data-pid="${p.id}" data-ptext="${escAttr(p.text)}" onclick="discussPrinciple(+this.dataset.pid, this.dataset.ptext)">Review together</button>
          </div>
        </div>`).join('');
      if (principles.length > 3) {
        html += `<div class="mp-more">${principles.length - 3} more in the Learned principles panel</div>`;
      }
    }
    if (r.message) {
      const m = r.message;
      html += `<div class="mp-section">Curator messages</div>
      <div class="msg-item" style="cursor:default">
        <div class="mi-text">${esc(m.message)}</div>
        <div style="display:flex;align-items:center;gap:6px;margin-top:8px">
          <span class="mi-badge">${esc(m.trigger_type.replace('_',' '))}</span>
          <span class="mi-time">${new Date(m.created_at).toLocaleString()}</span>
        </div>
        <div style="display:flex;gap:8px;margin-top:10px">
          <button class="btn btn-primary btn-sm" style="flex:1" data-mid="${m.id}" data-msg="${esc(m.message).replace(/"/g,'&quot;')}" data-ttype="${esc(m.trigger_type)}" onclick="respondToMessage(+this.dataset.mid, this.dataset.msg, this.dataset.ttype)">Respond</button>
          <button class="btn btn-secondary btn-sm" onclick="skipMessage(${m.id},this)" title="Skip — may come back later">Skip</button>
        </div>
      </div>`;
      if (r.total > 1) html += `<div class="mp-more">${r.total - 1} more waiting</div>`;
    }
    list.innerHTML = html || '<div id="msg-empty">No new notifications</div>';
  } catch {}
}

// Bell → chat: review a learned principle WITH the curator. The backend gets
// both sides (the new rule + the active rule-set) via discuss_context; the
// settled decision is applied automatically post-turn.
export async function discussPrinciple(id, text) {
  toggleMsgPanel();
  showView('chat', document.querySelector('.sb-item[onclick*=chat]'));
  addMsg(`I've learned a new rule from our debates and want to settle it with you: "${text}"`, 'assistant');
  state.pendingDiscussContext = { kind: 'principle', principle_id: id };
  _setDiscussBanner('Reviewing a learned principle');
  const input = document.getElementById('chat-input');
  if (input) { input.value = "Let's review this learned principle together."; }
  sendMessage();
}

export async function respondToMessage(id, msgText, triggerType) {
  await api(`/api/messages/${id}/read`, 'POST').catch(()=>{});
  toggleMsgPanel();

  // Switch to chat — same pattern as discussDeletion
  showView('chat', document.querySelector('.sb-item[onclick*=chat]'));

  // Show the curator's proactive message as an assistant bubble (UI-only; the
  // backend looks up the message by id and injects it as a RAG context block,
  // it doesn't need us to forward msgText).
  addMsg(msgText, 'assistant');

  state.pendingDiscussContext = {
    kind: 'proactive_message',
    message_id: id,
  };
  // Pass 27: sticky discuss-context — show banner so user knows
  _setDiscussBanner('Discussing a proactive message');

  // Leave input empty — user types their own response
  const input = document.getElementById('chat-input');
  if (input) { input.value = ''; input.focus(); }

  loadUnreadMessages();
}

export async function skipMessage(id, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  await api(`/api/messages/${id}/read`, 'POST').catch(()=>{});
  await loadUnreadMessages();
}

export function toggleMsgPanel() {
  const p = document.getElementById('msg-panel');
  const opening = !p.classList.contains('open');
  p.classList.toggle('open');
  // Refresh on open so the list is never the boot-time snapshot — shadow
  // principles land in the background long after the page loaded.
  if (opening) loadUnreadMessages();
}
