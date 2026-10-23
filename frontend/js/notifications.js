// ── NOTIFICATION CENTER (bell: proactive messages + system notifications) ────
import { api } from './api.js';
import { state } from './state.js';
import { EL, _errHtml, act, esc, escAttr, setBadge } from './ui.js';
import { _busyStreaming, enterThread, sendMessage } from './chat.js';
// seen === true only when the panel is open and the message is on screen —
// the 60 s badge poll must not count as an impression, or every message
// retires unread after ~40 minutes of an open tab. Strict === because
// setInterval may hand the callback a stray argument.
// Consecutive failed polls. The whole poll sat in `catch {}`: the badge and
// list froze at their last values for as long as the server failed.
let _bellFails = 0;
// Last principles list read: a failed read keeps it (as sysOk keeps the
// Settings count) instead of emptying the review queue from the bell.
let _lastPrinciples = [];

export async function loadUnreadMessages(seen) {
  try {
    const r = await api(seen === true ? '/api/messages/unread?seen=1' : '/api/messages/unread');
    // System notifications: learned principles awaiting review (admin only —
    // the endpoint is admin-gated anyway, this just avoids a guaranteed 403).
    let principles = [];
    if (state.currentUser?.is_admin) {
      try {
        const pr = await api('/api/recommendations/principles');
        principles = _lastPrinciples = (pr.principles || []).filter(p => p.status === 'shadow');
      } catch (e) {
        console.warn('[bell] principles read failed - showing the last known list', e);
        principles = _lastPrinciples;
      }
    }
    // System notifications (services/notifications.py): model updates today,
    // more kinds later — one list, rendered the same way. The server names an
    // action KEY; only the handlers in SYS_ACTIONS can run from here.
    let sys = {items: [], needs_action: 0}, sysOk = true;
    try { sys = await api('/api/system/notifications'); } catch { sysOk = false; }
    // The Settings bubble in the sidebar: what waits for an admin decision.
    // A failed read keeps the last count: zeroing it hid a pending
    // model-update decision whenever this one call failed.
    if (sysOk) setBadge('settings-badge', sys.settings_action ?? sys.needs_action ?? 0);
    const badge = document.getElementById('msg-badge');
    const total = (r.total || 0) + principles.length + (sys.needs_action || 0);
    badge.textContent = total;
    badge.classList.toggle('show', total > 0);
    const list = document.getElementById('msg-list');

    let html = '';
    if (sys.items.length) {
      html += `<div class="mp-section">System</div>`;
      html += sys.items.map(n => {
        const run = n.action && SYS_ACTIONS[n.action.key];
        const run2 = n.secondary && SYS_ACTIONS[n.secondary.key];
        const tone = n.severity === 'danger' ? 'danger' : n.severity === 'warn' ? 'amber' : 'muted';
        return `<div class="cursor-default msg-item">
          <div class="mi-text"><span class="fs-10 mr-4 badge ${tone}">${esc(n.kind.replace('_', ' '))}</span><b>${esc(n.title)}</b></div>
          <div class="fs-12 t2 mt-4">${esc(n.text)}</div>
          ${run ? `<div class="flex gap-8 mt-10"><button class="flex-1 btn btn-primary btn-sm" ${run(n.action)}>${esc(n.action.label)}</button>${
            run2 ? `<button class="flex-1 btn btn-secondary btn-sm" ${run2(n.secondary)}>${esc(n.secondary.label)}</button>` : ''}</div>` : ''}
        </div>`;
      }).join('');
    }
    if (principles.length) {
      html += `<div class="mp-section">Principles awaiting review</div>`;
      html += principles.slice(0, 3).map(p => `
        <div class="cursor-default msg-item">
          <div class="mi-text">${p.novelty === 'contradiction' ? '<span class="fs-10 mr-4 badge danger">contradiction</span>' : ''}${esc(p.text)}</div>
          <div class="flex gap-8 mt-10">
            <button class="flex-1 btn btn-primary btn-sm" ${act('discussPrinciple', p.id, p.text)}>Review together</button>
          </div>
        </div>`).join('');
      if (principles.length > 3) {
        html += `<div class="mp-more">${principles.length - 3} more in the Learned principles panel</div>`;
      }
    }
    if (r.message) {
      const m = r.message;
      html += `<div class="mp-section">From the curator</div>
      <div class="cursor-default msg-item">
        <div class="mi-text">${esc(m.message)}</div>
        <div class="inline-flex gap-6 mt-8">
          <span class="mi-badge">${esc(m.trigger_type.replace('_',' '))}</span>
          <span class="mi-time">${new Date(m.created_at).toLocaleString()}</span>
        </div>
        <div class="flex gap-8 mt-10">
          <button class="flex-1 btn btn-primary btn-sm" ${act('respondToMessage', m.id, m.message, m.trigger_type)}>Respond</button>
          <button class="btn btn-secondary btn-sm" ${act('skipMessage', m.id, EL)} title="Skip — may come back later">Skip</button>
        </div>
      </div>`;
      if (r.total > 1) html += `<div class="mp-more">${r.total - 1} more waiting</div>`;
    }
    list.innerHTML = html || '<div id="msg-empty">No new notifications</div>';
    _bellFails = 0;
  } catch (e) {
    // A 401 already ended the session in api(). Anything else: the third
    // miss in a row marks the bell and puts the error in the panel.
    if (++_bellFails < 3) return;
    const badge = document.getElementById('msg-badge');
    if (badge) { badge.textContent = '!'; badge.classList.add('show'); }
    const list = document.getElementById('msg-list');
    if (list) list.innerHTML = _errHtml(e, act('loadUnreadMessages'));
  }
}

// The only handlers a system notification may trigger, by the key the
// server sends. Literal act() calls, so the handler registry stays checkable;
// ``a.arg`` is data (act() escapes it), never a name to call.
const SYS_ACTIONS = {
  model_updates: () => act('openModelUpdates'),
  game_yes: a => act('answerGameQuestion', a.arg, true),
  game_no: a => act('answerGameQuestion', a.arg, false),
  pc_update: () => act('updateGamingPc'),
};

// Bell → chat: review a learned principle WITH the curator. The backend gets
// both sides (the new rule + the active rule-set) via discuss_context; the
// settled decision is applied automatically post-turn.
export async function discussPrinciple(id, text) {
  if (_busyStreaming()) return;
  toggleMsgPanel();
  if (!await enterThread({ kind: 'principle', principle_id: id }, 'Reviewing a learned principle',
                         `I've learned a new rule from our debates and want to settle it with you: "${text}"`)) return;
  const input = document.getElementById('chat-input');
  if (input) { input.value = "Let's review this learned principle together."; }
  sendMessage();
}

export async function respondToMessage(id, msgText, triggerType) {
  if (_busyStreaming()) return;
  // A verification question stays unread until it is ANSWERED: the backend
  // claims it (read → true) when the user's reply in this thread is
  // processed, and only matches unread questions. Marking it read here, before
  // the user has typed a word, meant no answer was ever recorded.
  if (triggerType !== 'verification') {
    await api(`/api/messages/${encodeURIComponent(id)}/read`, 'POST')
      .catch(e => console.warn('[bell] mark-read failed', id, e));
  }
  toggleMsgPanel();

  // Switch to chat — same pattern as discussDeletion: this message's thread
  // history, opened by the curator's proactive message as an assistant
  // bubble (UI-only; the backend looks up the message by id and injects it
  // as a RAG context block, it doesn't need us to forward msgText). Pass 27:
  // sticky discuss-context — the banner shows the user they're in a thread.
  if (!await enterThread({ kind: 'proactive_message', message_id: id },
                         'Discussing a proactive message', msgText)) return;

  // Leave input empty — user types their own response
  const input = document.getElementById('chat-input');
  if (input) { input.value = ''; input.focus(); }

  loadUnreadMessages();
}

export async function skipMessage(id, btn) {
  btn.disabled = true;
  btn.textContent = '…';
  await api(`/api/messages/${encodeURIComponent(id)}/read`, 'POST')
    .catch(e => console.warn('[bell] skip (mark-read) failed', id, e));
  await loadUnreadMessages(true);   // panel still open: the next one is shown
}

export function toggleMsgPanel() {
  const p = document.getElementById('msg-panel');
  const opening = !p.classList.contains('open');
  p.classList.toggle('open');
  // Refresh on open so the list is never the boot-time snapshot — shadow
  // principles land in the background long after the page loaded.
  if (opening) loadUnreadMessages(true);
}
