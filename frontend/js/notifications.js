// ── NOTIFICATION CENTER (bell: proactive messages + system notifications) ────
import { api } from './api.js';
import { state } from './state.js';
import { EL, _errHtml, act, esc, escAttr, loc, setBadge } from './ui.js';
import { _busyStreaming, enterThread, sendMessage } from './chat.js';
import { t, th } from './i18n.js';
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
      html += `<div class="mp-section">${th('bell.system')}</div>`;
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
      html += `<div class="mp-section">${th('bell.principles')}</div>`;
      html += principles.slice(0, 3).map(p => `
        <div class="cursor-default msg-item">
          <div class="mi-text">${p.novelty === 'contradiction' ? `<span class="fs-10 mr-4 badge danger">${th('bell.contradiction')}</span>` : ''}${esc(p.text)}</div>
          <div class="flex gap-8 mt-10">
            <button class="flex-1 btn btn-primary btn-sm" ${act('discussPrinciple', p.id, p.text)}>${th('bell.review_together')}</button>
          </div>
        </div>`).join('');
      if (principles.length > 3) {
        html += `<div class="mp-more">${th('bell.more_principles', {n: principles.length - 3})}</div>`;
      }
    }
    if (r.message) {
      const m = r.message;
      html += `<div class="mp-section">${th('bell.from_curator')}</div>
      <div class="cursor-default msg-item">
        <div class="mi-text">${esc(m.message)}</div>
        <div class="inline-flex gap-6 mt-8">
          <span class="mi-badge">${esc(m.label || m.trigger_type.replaceAll('_', ' '))}</span>
          <span class="mi-time">${new Date(m.created_at).toLocaleString(loc())}</span>
        </div>
        <div class="flex gap-8 mt-10">
          <button class="flex-1 btn btn-primary btn-sm" ${act('respondToMessage', m.id, m.message, m.trigger_type)}>${th('bell.respond')}</button>
          <button class="btn btn-secondary btn-sm" ${act('skipMessage', m.id, EL)} title="${th('bell.skip_title')}">${th('bell.skip')}</button>
        </div>
      </div>`;
      if (r.total > 1) html += `<div class="mp-more">${th('bell.more_waiting', {n: r.total - 1})}</div>`;
    }
    list.innerHTML = html || `<div id="msg-empty">${th('bell.empty')}</div>`;
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
  pc_update: () => act('updateNode'),
};

// Bell → chat: review a learned principle WITH the curator. The backend gets
// both sides (the new rule + the active rule-set) via discuss_context; the
// settled decision is applied automatically post-turn.
export async function discussPrinciple(id, text) {
  if (_busyStreaming()) return;
  toggleMsgPanel();
  if (!await enterThread({ kind: 'principle', principle_id: id }, t('chat.banner.principle'),
                         t('bell.principle_opener', {text}))) return;
  const input = document.getElementById('chat-input');
  if (input) { input.value = t('bell.principle_ask'); }
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
                         t('chat.banner.proactive'), msgText)) return;

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
  document.getElementById('msg-bell')?.setAttribute('aria-expanded', String(opening));
  // Refresh on open so the list is never the boot-time snapshot — shadow
  // principles land in the background long after the page loaded.
  if (opening) {
    loadUnreadMessages(true);
    _armPanelClose();
  }
}

// The panel closes like a menu: Escape, or a click outside it and the bell
// (it only had its own x; UI audit 2026-11-14). One listener pair, armed
// once; every close (here, Settings' "Model updates" link) goes through
// closeMsgPanel, so aria-expanded never says open over a closed panel.
export function closeMsgPanel() {
  document.getElementById('msg-panel')?.classList.remove('open');
  const bell = document.getElementById('msg-bell');
  bell?.setAttribute('aria-expanded', 'false');
  return bell;
}
let _panelArmed = false;
function _armPanelClose() {
  if (_panelArmed) return;
  _panelArmed = true;
  document.addEventListener('keydown', e => {
    const p = document.getElementById('msg-panel');
    if (e.key === 'Escape' && p?.classList.contains('open')) { e.stopPropagation(); closeMsgPanel()?.focus(); }
  }, true);
  document.addEventListener('pointerdown', e => {
    const p = document.getElementById('msg-panel');
    if (p?.classList.contains('open') && !p.contains(e.target) && !e.target.closest('#msg-bell')) closeMsgPanel();
  }, true);
}
