// ── CHAT ──────────────────────────────────────────────────────────────────────
import { EL, EVENT, _errMsg, _posterImg, act, actOn, confirmDialog, esc, escAttr, proxyImg, renderMarkdown, toast } from './ui.js';
import { api } from './api.js';
import { state } from './state.js';
import { _updateKbBadge } from './kb.js';
import { showView } from './nav.js';
import { _deleteBody, deleteFateLines } from './deletions.js';
import { applyOrphanRepair } from './libraries.js';
import { loadUnreadMessages } from './notifications.js';
export function handleKey(e) { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();} }

export async function newChat() {
  // Clear conversation history for the active thread (free chat = "general")
  // and wipe the visible messages. Memories + library remain — only the
  // chat-message log gets reset, so the curator starts each new topic
  // without prior-turn baggage. The thread is read BEFORE the dialog: it
  // used to be hard-coded to "general", so + New inside a discussion wiped
  // the free-chat log and left the discussion's own log in place.
  const tid = activeThreadId();
  const res = await confirmDialog({
    title: 'Clear conversation history', danger: true, confirmLabel: 'Clear',
    body: '<p>This deletes the chat log for this thread. Memories and the library are kept.</p>',
  });
  if (!res.ok) return;
  // A reply still streaming would be persisted AFTER the wipe and open the
  // "fresh" thread. Aborting it disconnects the stream, and the server
  // never persists a turn whose client went away.
  abortActiveStream();
  _threadSeq++;                              // a history load in flight must not repaint the pane
  try {
    const url = `/api/chat/history?thread_id=${encodeURIComponent(tid)}`;
    try {
      await api(url, 'DELETE');
    } catch (e) {
      // 409: the server could not save this chat's memories before the wipe
      // (summarizer down). Clearing now would lose them, so ask first.
      if (e.status !== 409) throw e;
      const again = await confirmDialog({
        title: 'Memories not saved yet', danger: true, confirmLabel: 'Clear anyway',
        body: `<p>${esc(_errMsg(e).replace(/,? or clear anyway with \?force=1\.?/, '.'))}</p><p>Clearing now loses what Curatarr would have remembered from this chat.</p>`,
      });
      if (!again.ok) return;
      await api(url + '&force=1', 'DELETE');
    }
    const msgs = document.getElementById('messages');
    if (msgs) {
      // Pass: + New is the explicit "start over" action, so it's also the
      // right moment to bring the glance/last-played/suggested-prompts
      // panels back -- they only die on send (see sendMessage()), not on
      // every view switch, so without this a user would see them exactly
      // once per page load and never again for the rest of the session.
      msgs.innerHTML = `
        <div class="msg assistant">Fresh chat. Ask me anything.</div>
        <div id="glance-panel"></div>
        <div id="last-played-panel"></div>
        <div class="flex gap-8 flex-wrap mt-14" id="suggested-prompts">
          <button class="sp-chip" ${act('fillPrompt', 'What should I watch tonight?')}>What should I watch tonight?</button>
          <button class="sp-chip" ${act('fillPrompt', 'What can I clean up in my library?')}>What can I clean up in my library?</button>
          <button class="sp-chip" ${act('fillPrompt', 'What does my watch history say about my taste?')}>What does my watch history say about my taste?</button>
        </div>`;
      loadGlancePanel();
      loadLastPlayed();
      loadStarters();
    }
    // Pass 27: + New also exits any active discussion thread — sticky
    // discuss-context shouldn't survive a chat reset.
    state.pendingDiscussContext = null;
    _setDiscussBanner(null);
  } catch (e) {
    toast('Failed to clear chat history: ' + _errMsg(e), 'danger');
  }
}

// Pass 18: invalidate the cached enrichment for the chat's current
// anchor — the title Curatarr is currently focused on. Use case: the
// LLM said something factually wrong about the current topic and the
// cached profile keeps reproducing the error. After clicking, the next
// question re-runs the cascade and re-fetches metadata from the
// upstream APIs (TMDB / AniList / MusicBrainz). Memory + chat history
// are untouched — only the metadata cache row goes.
export async function correctChatAnchor() {
  const btn = document.getElementById('recheck-btn');
  if (!btn || btn.disabled) return;
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = 'clearing…';
  try {
    const r = await api('/api/chat/correct-anchor', 'POST', { thread_id: activeThreadId() });
    // Inject a small system note into the chat so the user sees what happened
    // without a modal interrupting their flow.
    const msgs = document.getElementById('messages');
    if (msgs) {
      const note = document.createElement('div');
      note.className = 'msg system';
      note.textContent = r.ok
        ? `${r.message || `Cleared cache for '${r.title}'. Your next question will re-fetch fresh metadata.`}`
        : `${r.reason || 'No active topic to clear yet.'}`;
      msgs.appendChild(note);
      msgs.scrollTop = msgs.scrollHeight;
    }
  } catch (e) {
    toast('Re-fetch failed: ' + _errMsg(e), 'danger');
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

// ── STREAM CONTROLLER ───────────────────────────────────────────────────────
// One chat stream at a time, abortable (+ New aborts it), with an idle
// watchdog. The server sends ': keepalive' at least every 15 s for the whole
// turn — queued for the GPU, cold-loading the model, evaluating the prompt,
// inside a <think> block — so 45 s of silence after the headers means the
// connection is dead, not slow. Before the headers the server is still
// building context (discuss lookups, metadata cascade, RAG) and sends
// nothing, so that phase gets a wider limit of its own.
const STREAM_IDLE_MS = 45_000;
const STREAM_HEADERS_MS = 300_000;
let _stream = null;                       // { ctrl, threadId }

export const isStreaming = () => !!_stream;

// The thread the next turn (or the running one) lives on. The server names
// it in each stream's first frame; between streams it is derived from the
// discuss context exactly as chat.py's _thread_id_for does.
export function activeThreadId() {
  return _stream?.threadId || _threadIdForContext(state.pendingDiscussContext);
}

export function abortActiveStream(reason = 'superseded') {
  _stream?.ctrl.abort(reason);
  _stream = null;
}

// Every discuss / respond / review entry point starts here: switching the
// thread under a running reply would show the new banner over the old
// answer and send the next turn into a 409.
export function _busyStreaming() {
  if (!_stream) return false;
  toast('The curator is still answering — wait for this reply to finish first.', 'amber');
  return true;
}

// Pre-stream status events arrive in a quick burst (the backend runs them
// sequentially before streaming). Each stays visible for 700 ms, and the
// previous ones fade into a small trail underneath, so the sequence reads
// even when the first token lands before the queue has drained.
function _statusTicker(bubble) {
  const q = [];
  let timer = null, node = null, log = null, stopped = false;
  const next = () => {
    if (stopped || !q.length) { timer = null; return; }
    if (!node) {
      node = bubble.appendChild(Object.assign(document.createElement('div'), {className: 'thinking-status'}));
      log = bubble.appendChild(Object.assign(document.createElement('div'), {className: 'thinking-status-log'}));
    }
    if (node.textContent) log.appendChild(Object.assign(document.createElement('div'), {textContent: node.textContent}));
    // Re-trigger the fade animation by removing + re-adding it
    node.style.animation = 'none';
    void node.offsetHeight;
    node.style.animation = '';
    node.textContent = q.shift();
    timer = setTimeout(next, 700);
  };
  return {
    push(s) { if (stopped) return; q.push(s); if (!timer) next(); },
    stop() { stopped = true; q.length = 0; clearTimeout(timer); timer = null; },
  };
}

export async function sendMessage() {
  if (_busyStreaming()) return;
  const inp = document.getElementById('chat-input');
  const btn = document.getElementById('send-btn');
  const text = inp.value.trim(); if (!text) return;
  inp.value = ''; inp.style.height = 'auto';
  inp.disabled = btn.disabled = true;
  ['glance-panel', 'last-played-panel', 'suggested-prompts'].forEach(id => document.getElementById(id)?.remove());
  addMsg(text, 'user');
  const bubble = addThinkingMsg();

  // Pass 27: state.pendingDiscussContext is STICKY — it stays until the user
  // exits the discussion, starts a new chat, or opens another one. The
  // payload gets a snapshot so clearing a one-shot flag below cannot reach
  // back into this turn.
  const ctx = state.pendingDiscussContext;
  const payload = { message: text, ...(ctx && { discuss_context: { ...ctx } }) };
  const ctrl = new AbortController();
  const me = _stream = { ctrl, threadId: _threadIdForContext(ctx) };
  const status = _statusTicker(bubble);

  let idle = null, rafId = 0, full = '', painted = '', gotDone = false, httpError = false;
  const arm = ms => { clearTimeout(idle); idle = setTimeout(() => ctrl.abort('idle'), ms); };
  // marked passes raw HTML through and the model can quote metadata text
  // from foreign sources: renderMarkdown sanitizes it and strips the
  // data-action / data-on-* attributes the dispatcher would run. One parse
  // per animation frame, not per token — the whole reply is re-parsed each
  // time, so per-token rendering grew quadratically with its length.
  const paint = () => {
    rafId = 0;
    if (full !== painted && bubble.isConnected) { bubble.innerHTML = renderMarkdown(full); painted = full; }
  };

  try {
    arm(STREAM_HEADERS_MS);
    const tok = state.token || localStorage.getItem('curatarr_token') || sessionStorage.getItem('curatarr_token') || '';
    const resp = await fetch('/api/chat/message', {
      method: 'POST',
      signal: ctrl.signal,
      headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${tok}` },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      httpError = true;
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      // Pydantic 422 detail is an array of validation error objects — flatten to readable string
      bubble.textContent = Array.isArray(err.detail)
        ? err.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (err.detail || resp.statusText);
      return;
    }

    // Pass 81d: ``reevaluate`` fires the Level-2 framing on the FIRST turn
    // after the Reevaluate button only. It is spent once the server has
    // accepted the turn — a 409 used to burn it before anything ran.
    if (ctx?.reevaluate && state.pendingDiscussContext === ctx) ctx.reevaluate = false;

    // The animated dots stay until the first real token, otherwise the
    // bubble flashes empty between the spinner and the first character.
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (!gotDone) {
      arm(STREAM_IDLE_MS);
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop(); // keep incomplete line
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;   // ': keepalive' only re-arms the watchdog
        let data;
        try { data = JSON.parse(line.slice(6)); } catch { continue; }
        if (data.thread) me.threadId = data.thread;
        if (data.status) status.push(data.status);
        if (data.warning && !bubble.previousElementSibling?.classList.contains('banner')) {
          // VRAM fallback / health warning from the backend — shown as a
          // small banner above the streaming bubble so the user knows the
          // response will be slower than usual.
          const banner = document.createElement('div');
          banner.className = 'banner ' + (data.severity === 'severe' ? 'danger' : 'warn');
          banner.style.alignSelf = 'stretch';
          banner.textContent = data.warning;
          bubble.before(banner);
        }
        if (data.token) {
          if (!full) { status.stop(); bubble.textContent = ''; }
          full += data.token;
          if (!rafId) rafId = requestAnimationFrame(paint);
        }
        if (data.done) { gotDone = true; break; }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') console.warn('[chat] stream failed:', e);
  } finally {
    clearTimeout(idle);
    status.stop();
    if (rafId) cancelAnimationFrame(rafId);
    paint();                                      // the last frame is never lost
    if (!gotDone && !httpError) {
      // The partial reply stays on screen; the note says it was not kept
      // (the server persists a turn only once its stream completes).
      const why = ctrl.signal.reason;
      const note = why === 'superseded' ? 'Stopped.'
        : why === 'idle' ? 'The connection went quiet — this reply was interrupted and not saved.'
        : 'Connection lost — this reply was interrupted and not saved.';
      if (!full) bubble.textContent = note;
      else bubble.after(Object.assign(document.createElement('div'), {className: 'msg system', textContent: note}));
    } else if (gotDone && !full) {
      bubble.textContent = '(no response)';
    }
    if (_stream === me) _stream = null;
    inp.disabled = btn.disabled = false;
    if (bubble.isConnected) inp.focus();
    // A "keep it" in a deletion discussion protects the title in a
    // background job after the turn: the Deletions tab cache must not serve
    // the stale proposal, and the outcome note lands in the bell.
    if (gotDone && me.threadId.startsWith('deletion_proposal:')) {
      state._delProposalsAll = null;
      setTimeout(() => loadUnreadMessages(), 5000);
    }
  }
}

// ── CHAT EMPTY-STATE "AT A GLANCE" PANEL ────────────────────────────────────
// Fires once after login, before the first message. Every source is fetched
// independently (Promise.allSettled) so one failing endpoint just means one
// missing tile, not a blank panel -- and each tile jumps straight to the
// view it summarizes. Gone the moment the user actually sends something
// (see sendMessage()); the point is a landing glance, not a permanent
// dashboard competing with the conversation.
export async function loadGlancePanel() {
  const el = document.getElementById('glance-panel');
  if (!el) return;
  const isAdmin = !!state.currentUser?.is_admin;

  const [enrich, overview, recs, dels, tasks] = await Promise.allSettled([
    api('/api/enrichment/status?quick=true'),
    api('/api/enrichment/overview'),
    api('/api/recommendations/?source=cache&limit=1'),
    isAdmin ? api('/api/recommendations/deletions?refresh=false') : Promise.resolve(null),
    api('/api/tasks/history'),
  ]);

  const tiles = [];

  // Two honest numbers with two denominators. The Knowledge-Base overview
  // divides by DOWNLOADED library items — the same basis as the per-library
  // cards. The quick status divides by distinct WATCHED titles; that used to
  // be this tile's only figure and read "93%" next to cards saying 46/92/70/24.
  let libPct = null, watchedPct = null;
  if (overview.status === 'fulfilled') {
    let live = 0, of = 0;
    for (const c of Object.values(overview.value.categories || {})) {
      live += (c?.states?.enriched || 0) + (c?.states?.enriched_provisional || 0);
      of += c?.denominator?.downloaded || 0;
    }
    if (of) libPct = Math.round(100 * live / of);
    _updateKbBadge(overview.value);
  }
  if (enrich.status === 'fulfilled') {
    let done = 0, total = 0;
    for (const c of Object.values(enrich.value.categories || {})) {
      done += c?.enriched || 0;
      total += c?.total_unique || 0;
    }
    if (total) watchedPct = Math.round(100 * done / total);
  }
  if (libPct !== null) {
    tiles.push({num: libPct + '%', view: 'enrich',
                lbl: watchedPct !== null ? `Library enriched · ${watchedPct}% of watched titles` : 'Library enriched'});
  } else if (watchedPct !== null) {
    tiles.push({num: watchedPct + '%', lbl: 'Watched titles enriched', view: 'enrich'});
  }

  if (recs.status === 'fulfilled' && recs.value.recommendations) {
    tiles.push({num: recs.value.recommendations.length, lbl: 'Recommendations ready', view: 'recs'});
  }

  if (isAdmin && dels.status === 'fulfilled' && dels.value) {
    const n = (dels.value.proposals || []).length;
    tiles.push({num: n, lbl: n === 1 ? 'Deletion proposal' : 'Deletion proposals', view: 'deletions'});
  }

  if (tasks.status === 'fulfilled') {
    const latest = Object.values(tasks.value.last_runs || {})
      .filter(r => r.at).sort((a, b) => (b.at || '').localeCompare(a.at || ''))[0];
    if (latest) {
      const mins = Math.round((Date.now() - new Date(latest.at).getTime()) / 60000);
      const when = mins < 1 ? 'just now' : mins < 60 ? `${mins}m ago` : `${Math.round(mins / 60)}h ago`;
      tiles.push({num: when, lbl: 'Last activity', view: 'tasks', small: true});
    }
  }

  if (!tiles.length) return; // every source failed -- say nothing rather than show an empty shell

  el.innerHTML = `<div class="mt-16 mb-4 stat-row">` + tiles.map(t => `
    <div class="pointer stat-box" role="button" tabindex="0" aria-label="Go to ${escAttr(t.lbl)}" ${act('goToView', t.view)} ${actOn('keydown', 'keyActivate', EVENT, EL)}>
      <div class="fs-16 num"${t.small ? '' : ''}>${esc(String(t.num))}</div>
      <div class="lbl">${esc(t.lbl)}</div>
    </div>`).join('') + `</div>`;
}

// ── CHAT EMPTY-STATE "LAST PLAYED" STRIP ────────────────────────────────────
// Same lifecycle as the glance panel (loaded once at boot, gone once the
// user sends anything -- see sendMessage()). poster_url isn't in
// /api/history/recent's response yet (verified against
// src/routers/history.py before writing this) -- _posterImg() already
// degrades that to a "?" tile with zero special-casing here, so this ships
// now and just gets prettier the day that field lands.
// Moved state._lastPlayedEntries to state
export async function loadLastPlayed() {
  const el = document.getElementById('last-played-panel');
  if (!el) return;
  try {
    // Per-category, not just "the last N overall" -- a music-heavy stretch
    // would otherwise crowd out movies/shows/anime entirely (seen live: 8
    // straight music entries, nothing else). 2 per category, interleaved
    // back to newest-first so it still reads as "recent" as a whole.
    const cats = ['movie', 'show', 'anime', 'music'];
    const results = await Promise.allSettled(
      cats.map(c => api(`/api/history/recent?limit=2&category=${c}`))
    );
    state._lastPlayedEntries = results
      .flatMap(r => r.status === 'fulfilled' ? (r.value.entries || []) : [])
      .sort((a, b) => (b.viewed_at || '').localeCompare(a.viewed_at || ''));
    if (!state._lastPlayedEntries.length) return;
    el.innerHTML = `<div class="lp-heading">Last played</div><div class="lp-row">` +
      state._lastPlayedEntries.map((e, i) => {
        const isMusic = e.media_type === 'music';
        let title = e.title;
        if (isMusic && e.series_title) title = `${e.title} — ${e.series_title}`;
        else if ((e.media_type === 'anime' || e.media_type === 'show') && e.series_title) title = `${e.series_title}: ${e.title}`;
        return `<div class="lp-chip glow-interactive selectable" role="button" tabindex="0" aria-label="Discuss ${escAttr(title)}" ${act('discussLastPlayed', i)} ${actOn('keydown', 'keyActivate', EVENT, EL)}>
          ${_posterImg(e.poster_url, 72, isMusic ? 72 : 108, isMusic)}
          <div class="lp-title">${esc(title)}</div>
        </div>`;
      }).join('') + `</div>`;
  } catch {} // best-effort nicety, not core functionality -- fail silent
}
// Unlike discussLastPlayed()/discussInChat(), this only fills + focuses --
// no auto-send -- since a suggested prompt is a starting point the user is
// expected to tweak, not a specific-title primer that's already complete.
export function fillPrompt(text) {
  const inp = document.getElementById('chat-input');
  inp.value = text;
  inp.focus();
}
// Swaps the static #suggested-prompts chips (already in the DOM, written by
// newChat()/the page's initial HTML) for curator-picked ones when the pool
// has fresh material. Empty or failed GET -- e.g. a brand-new user with no
// pool yet -- leaves the 3 static chips exactly as they were: no partial
// replace, no empty panel.
export async function loadStarters() {
  const el = document.getElementById('suggested-prompts');
  if (!el) return;
  try {
    const r = await api('/api/chat/starters');
    const starters = (r.starters || []).slice(0, 3);
    if (!starters.length) return;
    el.innerHTML = starters.map(s =>
      `<button class="sp-chip" ${act('useStarter', s.id, EL)}>${esc(s.text)}</button>`
    ).join('');
  } catch {} // best-effort nicety, keep the static fallback -- fail silent
}
// Unlike fillPrompt(), auto-sends -- a curator-picked starter is already a
// complete, ready-to-send question, not a template to tweak. Marking it
// used is fire-and-forget: the pool bookkeeping shouldn't gate the send.
// Backend contract (e9ff931): a starter chip makes the CURATOR say the
// opener, not the user -- discuss_context {kind:'starter', starter_id} is
// re-injected server-side on every turn of the thread (never persisted as
// a fake assistant message), the same non-persisted-opener mechanism as
// proactive-message discussions. Mirrors respondToMessage()'s shape, not
// discussInChat()'s: render the line as an assistant bubble, set the
// sticky context, leave the input empty for the user's own reply -- no
// auto-send, there's nothing of the user's to send yet. The static
// fallback chips (fillPrompt(), never routed through here) stay
// unchanged: user-voiced text the user fills in and edits, not opened by
// the curator.
export async function useStarter(id, btn) {
  if (_busyStreaming()) return;
  const text = btn.textContent;
  api(`/api/chat/starters/${encodeURIComponent(id)}/used`, 'POST')
    .catch(e => console.warn('[chat] starter mark-used failed', id, e));
  if (!await enterThread({ kind: 'starter', starter_id: id }, 'Curator opened this thread', text)) return;
  const inp = document.getElementById('chat-input');
  inp.value = '';
  inp.focus();
}
export async function discussLastPlayed(i) {
  const e = state._lastPlayedEntries[i];
  if (!e || _busyStreaming()) return;
  const isMusic = e.media_type === 'music';
  let title = e.title;
  if (isMusic && e.series_title) title = `${e.title} — ${e.series_title}`;
  else if ((e.media_type === 'anime' || e.media_type === 'show') && e.series_title) title = `${e.series_title}: ${e.title}`;

  // Structured context anchors on this exact watch-history row (history_id,
  // always present -- every /api/history/recent entry carries its own PK)
  // so the backend can inject real facts instead of trusting our text. Same
  // trust model + sticky state.pendingDiscussContext pipeline as discussDeletion/
  // respondToMessage: set it, then let sendMessage() pick it up below.
  const ctx = e.id ? {
    kind: 'watched_title',
    history_id: e.id,
    series_title: e.series_title || null,
    title: e.title || null,
    category: e.media_type || null,
    season: e.season ?? null,
    episode: e.episode ?? null,
    viewed_at: e.viewed_at || null,
    poster_url: e.poster_url || null,
  } : null;
  if (!await enterThread(ctx, ctx ? `Discussing "${title}"` : null)) return;
  discussInChat('custom', {message: `I recently ${isMusic ? 'listened to' : 'watched'} "${title}" — what do you think about it, or what should I know?`});
}

export function addMsg(text,role) {
  const div = document.createElement('div'); div.className='msg '+role;
  div.textContent=text;
  const msgs = document.getElementById('messages');
  msgs.appendChild(div); msgs.scrollTop=msgs.scrollHeight;
  return div;
}

// Animated three-dot placeholder used while we wait for the first state.token
// from the streaming chat endpoint. Same structure as addMsg() so the
// caller can later replace innerHTML with the real response.
export function addThinkingMsg() {
  const div = document.createElement('div');
  div.className = 'msg assistant';
  div.innerHTML = '<div class="thinking-dots" aria-label="Thinking"><span></span><span></span><span></span></div>';
  const msgs = document.getElementById('messages');
  msgs.appendChild(div);
  msgs.scrollTop = msgs.scrollHeight;
  return div;
}

// ── THREAD PANE ───────────────────────────────────────────────────────────────
// #messages shows ONE thread. The curator reads a thread's saved history as
// context on every turn, so switching into or out of a discussion — or a
// reload — paints that thread's history: the pane used to keep whatever the
// previous thread left in it, and showed nothing at all after F5.
const HISTORY_LIMIT = 50;
let _threadSeq = 0;           // a newer switch (or + New) overtakes an older load

function _historyBubble(m) {
  const div = document.createElement('div');
  if (m.role === 'user') {
    div.className = 'msg user';
    div.textContent = m.content || '';
  } else {
    div.className = 'msg assistant';
    div.innerHTML = renderMarkdown(m.content || '');
  }
  return div;
}

async function _loadThread(tid) {
  try {
    const r = await api(`/api/chat/history?thread_id=${encodeURIComponent(tid)}&limit=${HISTORY_LIMIT}`);
    return r.messages || [];
  } catch (e) {
    // null, not []: an empty thread would claim there is no history
    console.warn('[chat] history load failed', tid, e);
    return null;
  }
}

function _historyErrorRow(onRetry) {
  const row = document.createElement('div');
  row.className = 'msg system load-err';
  row.textContent = "Couldn't load this conversation's history — it is still saved on the server. ";
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'btn btn-secondary btn-sm';
  retry.textContent = 'Retry';
  retry.addEventListener('click', () => { if (!_busyStreaming()) onRetry(); });
  row.appendChild(retry);
  return row;
}

// Repaint #messages with thread `tid`: the curator's opener first (UI-only —
// the backend injects the real record as context and never persists it),
// then the saved exchange. False when a newer switch overtook this one.
async function _showThread(tid, opener) {
  const seq = ++_threadSeq;
  const history = await _loadThread(tid);
  const msgs = document.getElementById('messages');
  if (seq !== _threadSeq || !msgs) return false;
  msgs.replaceChildren();
  if (opener) addMsg(opener, 'assistant');
  if (history === null) msgs.appendChild(_historyErrorRow(() => _showThread(tid, opener)));
  else for (const m of history) msgs.appendChild(_historyBubble(m));
  msgs.scrollTop = msgs.scrollHeight;
  return true;
}

// Every way into a discussion: the sticky context, its banner, the chat
// view, and that thread's history. Callers check _busyStreaming() first.
export async function enterThread(ctx, banner, opener) {
  state.pendingDiscussContext = ctx;
  _setDiscussBanner(banner);
  showView('chat');
  return _showThread(_threadIdForContext(ctx), opener);
}

// Boot: the free-chat history replaces the greeting, above the landing
// panels (glance, last played, starters), which stay until the first send.
// Skipped once anything else owns the pane — a send, a discussion, + New.
export async function restoreChatHistory() {
  const seq = ++_threadSeq;
  const history = await _loadThread('general');
  const msgs = document.getElementById('messages');
  if (seq !== _threadSeq || !msgs || state.pendingDiscussContext
      || msgs.querySelector('.msg.user')) return;
  const anchor = document.getElementById('glance-panel');
  if (history === null) {
    // keep the greeting and the landing panels, but say the history is missing
    msgs.querySelector('.msg.load-err')?.remove();
    msgs.insertBefore(_historyErrorRow(restoreChatHistory), anchor);
    return;
  }
  if (!history.length) return;
  msgs.querySelector('.msg.load-err')?.remove();
  msgs.querySelector('.msg.assistant')?.remove();
  for (const m of history) msgs.insertBefore(_historyBubble(m), anchor);
  msgs.scrollTop = msgs.scrollHeight;
}

// ── DISCUSS IN CHAT ───────────────────────────────────────────────────────────
export function discussInChat(type, data) {
  if (_busyStreaming()) return;
  // Switch to chat view
  showView('chat');

  // Prime the chat with context
  let primer = '';
  if (type === 'rec') {
    primer = `I want to talk about the recommendation for "${data.title}". You suggested it because: "${data.reason}". Let's discuss whether this actually fits my taste.`;
  } else if (type === 'delete') {
    primer = `I just deleted "${data.title}" from my library. My reason: "${data.reason || 'no specific reason given'}". What does this tell you about my taste? What should I avoid in the future?`;
  } else if (type === 'custom') {
    primer = data.message || '';
  }

  if (primer) {
    const input = document.getElementById('chat-input');
    input.value = primer;
    input.focus();
    // Auto-send after short delay so user can see what's being sent
    setTimeout(() => sendMessage(), 300);
  }
}

// The note saves itself on blur / Ctrl+Enter; the hint line under it says
// what happened. A note that reads as "keep" closes the proposal.
export async function saveComment(id) {
  const ta = document.getElementById(`del-comment-${id}`);
  const hint = document.getElementById(`del-note-hint-${id}`);
  if (!ta) return;
  const comment = ta.value.trim();
  if (comment === (ta.dataset.saved || '')) return;   // nothing new
  if (hint) hint.textContent = 'Saving…';
  try {
    const r = await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/comment?comment=${encodeURIComponent(comment)}`, 'POST');
    ta.dataset.saved = comment;
    if (r && r.is_kept) {
      if (hint) hint.textContent = 'Saved — read as "keep", the proposal is closed.';
      ta.closest('.card')?.classList.add('done');
      if (state._delProposalsAll) state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
    } else if (hint) {
      hint.textContent = comment ? 'Note saved.' : 'Note cleared.';
    }
  } catch (e) {
    if (hint) hint.textContent = 'Could not save the note — ' + _errMsg(e);
  }
}

// Pass 27: discuss-banner helpers. Sticky discuss_context means the user
// can be "anchored" on a proposal across many turns — they need to see
// that AND have a way out.
export function _setDiscussBanner(text) {
  const banner = document.getElementById('discuss-banner');
  const txt    = document.getElementById('discuss-banner-text');
  const delBtn = document.getElementById('discuss-delete-btn');
  if (!banner || !txt) return;
  if (text) {
    txt.textContent = text;
    banner.hidden = false;
    // Pass 53: the "Delete & exit" button only makes sense for deletion
    // discussions — proactive-message threads have nothing to delete.
    // Relies on state.pendingDiscussContext being set BEFORE this call, which
    // both call sites (discussDeletion, discussInChat/proactive) do.
    if (delBtn) {
      const isDeletion = state.pendingDiscussContext?.kind === 'deletion_proposal';
      delBtn.hidden = !isDeletion;
      delBtn.disabled = false;
      delBtn.textContent = 'Delete & exit';
    }
  } else {
    banner.hidden = true;
    txt.textContent = '';
    if (delBtn) delBtn.hidden = true;
  }
  // B4: every set/clear of the banner already means state.pendingDiscussContext
  // just changed (both happen together at every call site), so this is
  // the one chokepoint to also drive the watermark from -- no need to
  // touch discussDeletion/discussLastPlayed/etc. individually.
  _setChatWatermark(state.pendingDiscussContext?.poster_url || null);
}

// Faint poster wash behind the active discussion -- var(--bg-veil) is the
// app's own --bg at ~93% opacity, so the ~7% of image that shows through
// reads as texture, not a picture competing with the message text on top.
// No watermark at all when the context has no poster (proactive_message,
// principle -- neither is about one specific titled thing).
export function _setChatWatermark(url) {
  const msgs = document.getElementById('messages');
  if (!msgs) return;
  if (!url) { msgs.style.backgroundImage = ''; return; }
  const sizeMatch = url.match(/\/(w\d+)\//);
  // w780, not w342: the chat pane spans most of a desktop viewport, and a
  // 342px source stretched across ~1500+px reads as mush even under the
  // veil. w780 is TMDB's largest fixed poster size; at ~7% visibility the
  // extra bytes are paid once (disk-cached by the proxy, immutable in the
  // browser) and the texture actually resolves.
  const big = sizeMatch ? url.replace(sizeMatch[1], 'w780') : url;
  const css = _cssUrl(proxyImg(big));
  msgs.style.backgroundImage = css ? `linear-gradient(var(--bg-veil), var(--bg-veil)), ${css}` : '';
}

// A CSS url() from a URL string. proxyImg() hands same-origin URLs back
// raw, and a quote in one (poster_url = '/a"), url("https://tracker/x')
// closed the url() and opened a second one straight to a third-party host,
// past the fail-closed image proxy. Serialising through URL() and
// percent-encoding what can end a double-quoted url() (", \, a newline)
// closes that.
export function _cssUrl(raw) {
  try {
    const u = new URL(raw, location.origin);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return `url("${u.href.replace(/["\\\n\r]/g, c => encodeURIComponent(c))}")`;
  } catch {
    return null;
  }
}

// Pass 61: mirror of the backend _thread_id_for (src/routers/chat.py) —
// derive the thread id from a discuss context so the frontend targets the
// right thread for the memory flush, + New, the anchor re-fetch and the
// history pane. Keep the two in step: principle / starter / watched-title
// threads used to fall through to "general" here, so their memories were
// never flushed on exit. A running stream's server-named thread wins (see
// activeThreadId()).
export function _threadIdForContext(ctx) {
  if (!ctx) return 'general';
  const kind = ctx.kind || ctx.type || ctx.action;
  if ((kind === 'deletion_proposal' || kind === 'deletion') && ctx.proposal_id) return `deletion_proposal:${ctx.proposal_id}`;
  if (kind === 'proactive_message' && ctx.message_id) return `proactive_message:${ctx.message_id}`;
  if (kind === 'principle' && ctx.principle_id) return `principle:${ctx.principle_id}`;
  if (kind === 'starter' && ctx.starter_id) return `starter:${ctx.starter_id}`;
  if (kind === 'watched_title') {
    if (ctx.tmdb_id) return `watched:tmdb:${ctx.tmdb_id}`;
    const t = (ctx.series_title || ctx.title || '').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
    if (t) return `watched:${t.slice(0, 80)}`;
  }
  return 'general';
}

// Fire-and-forget on an explicit end of conversation. A failure gets a toast:
// it is what the owner said in this thread not being remembered yet. The
// endpoint answers {ok: false} (HTTP 200) when the extraction itself failed.
function _flushThreadMemories(tid) {
  api('/api/chat/flush-memories', 'POST', { thread_id: tid })
    .then(r => { if (r && r.ok === false) throw new Error(r.error || 'memory extraction failed'); })
    .catch(e => {
      console.warn('[chat] flush-memories failed', tid, e);
      toast("Couldn't save this conversation's memories yet: " + _errMsg(e), 'amber');
    });
}

export async function exitDiscussion() {
  if (_busyStreaming()) return;
  // Pass 61: "Exit discussion" is an explicit end-of-conversation signal —
  // flush the thread's pending memory extraction NOW instead of letting it
  // wait out the 90s debounce (or get lost if the user never returns to
  // this thread). Fire-and-forget — the UI shouldn't block on it. Must
  // read the thread id BEFORE we null state.pendingDiscussContext below.
  const tid = _threadIdForContext(state.pendingDiscussContext);
  if (tid && tid !== 'general') {
    _flushThreadMemories(tid);
  }
  // Drops the sticky discuss_context so the next message is plain free chat.
  // Doesn't wipe chat history — that's what the "+ New" button is for.
  state.pendingDiscussContext = null;
  _setDiscussBanner(null);
  if (!await _showThread('general')) return;
  // Inject a small system note so the user sees their action took effect.
  const msgs = document.getElementById('messages');
  if (msgs) {
    const note = document.createElement('div');
    note.className = 'msg system';
    note.textContent = 'Exited the discussion thread. The next message is free chat.';
    msgs.appendChild(note);
    msgs.scrollTop = msgs.scrollHeight;
  }
}

// Pass 53: act on the deletion proposal we're already discussing, straight
// from the chat thread. We know the proposal_id from state.pendingDiscussContext,
// so there's no reason to send the user back to the Deletions view just to
// click delete on something the whole thread was about. Hits the same
// /approve endpoint the Deletions-view "Delete" button uses.
export async function deleteFromDiscussion() {
  const ctx = state.pendingDiscussContext;
  if (!ctx || ctx.kind !== 'deletion_proposal' || !ctx.proposal_id) return;
  const id    = ctx.proposal_id;
  const title = ctx.title || 'this item';

  // Deletion is irreversible — keep the 3-second-countdown confirm. No
  // reason field here: the discussion thread itself IS the reasoning.
  const res = await confirmDialog({
    title: 'Delete from library', danger: true, countdown: 3, confirmLabel: 'Delete',
    body: _deleteBody(title, await deleteFateLines([(state._delProposalsAll || []).find(p => String(p.id) === String(id))?.service])),
  });
  if (!res.ok) return;

  const btn = document.getElementById('discuss-delete-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }

  try {
    // Lightweight audit trail — the actual reasoning lives in the thread
    // history, this is just a marker on the proposal row.
    await api(
      `/api/recommendations/deletions/${encodeURIComponent(id)}/comment?comment=${encodeURIComponent('Deleted after in-chat discussion')}`,
      'POST',
    ).catch(e => console.warn('[chat] deletion audit comment failed', id, e));

    const r = await api(`/api/recommendations/deletions/${encodeURIComponent(id)}/approve`, 'POST');

    if (r.limbo) {
      // ARR unreachable — proposal kept in limbo, user can retry.
      addMsg(r.error, 'assistant');
      if (btn) { btn.disabled = false; btn.textContent = 'Delete & exit'; }
      return;
    }

    if (r.ok) {
      // Evict from the Deletions-view client cache so the card doesn't
      // reappear when the user switches tabs.
      if (typeof state._delProposalsAll !== 'undefined' && state._delProposalsAll) {
        state._delProposalsAll = state._delProposalsAll.filter(p => p.id !== id);
      }
      // Pass 61: explicit end-of-conversation — flush this thread's pending
      // memory extraction now. Fire-and-forget; read the thread id before
      // state.pendingDiscussContext is nulled below.
      const tid = _threadIdForContext(state.pendingDiscussContext);
      if (tid && tid !== 'general') {
        _flushThreadMemories(tid);
      }
      // Close the discussion — same end-state as exitDiscussion(), minus
      // its "exited thread" note (a more specific one follows). A reply
      // still streaming keeps its pane; the next send is free chat either way.
      state.pendingDiscussContext = null;
      _setDiscussBanner(null);
      if (!_stream) await _showThread('general');
      addMsg(`Deleted "${title}". Discussion thread closed.`, 'assistant');
    } else {
      addMsg(`Delete failed for "${title}". Try again from the Deletions view.`, 'assistant');
      if (btn) { btn.disabled = false; btn.textContent = 'Delete & exit'; }
    }
  } catch (e) {
    addMsg(`Delete request failed for "${title}". Try again from the Deletions view.`, 'assistant');
    if (btn) { btn.disabled = false; btn.textContent = 'Delete & exit'; }
  }
}

export async function discussDeletion(id, title, pitch, category, poster_url) {
  if (_busyStreaming()) return;
  const userNote = (document.getElementById(`del-comment-${id}`)?.value || '').trim();

  // 1. Save the context for the next API send event. Backend looks up the
  //    proposal by id (with ownership check) and builds the RAG block from
  //    the trusted DB row — title/pitch are NOT forwarded to the server.
  //    `category` is forwarded only as a domain hint for RAG-quarantine.
  // Pass 27: ``state.pendingDiscussContext`` is now sticky across follow-up sends.
  // We update the banner so the user can SEE they're in a discussion thread
  // and have an exit hatch.
  // 2. Switch to the chat view, with this proposal's thread history.
  // 3. The pitch opens the pane as an Assistant message (UI-only; this is
  //    what the user sees, NOT what the backend persists).
  const ctx = {
    kind: 'deletion_proposal',
    proposal_id: id,
    category: category || null,
    // Pass 53: title is a legacy hint field — the backend ignores it
    // (it looks the proposal up by id), but the in-chat "Delete & exit"
    // button needs it for the confirm modal + the success note.
    title: title || null,
    poster_url: poster_url || null,
  };
  if (!await enterThread(ctx, `Discussing deletion of "${title}"`,
                         `I have suggested "${title}" for deletion. Reason: "${pitch}"`)) return;

  // 4. Prepare the input field for the user
  const input = document.getElementById('chat-input');
  if (userNote) {
    input.value = `My note on this: "${userNote}". Let's discuss it.`;
  } else {
    input.value = `Let's discuss whether this should stay in my library.`;
  }

  input.focus();
  // We NO LONGER auto-send using setTimeout(() => sendMessage(), 300) here.
  // The user should have time to review the message and press "Send" manually!
}

// ── DOM-event-friendly wrappers for the discuss / orphan buttons ─────────────
// Buttons render with data-* attributes; these helpers read them and call
// the underlying handler. Avoids the previous JSON.stringify-into-onclick
// pattern that was an XSS surface for any LLM-generated text in the title /
// reason / pitch fields.

export function onDiscussRec(btn) {
  if (!btn) return;
  discussInChat('rec', {
    title:    btn.dataset.title    || '',
    reason:   btn.dataset.reason   || '',
    category: btn.dataset.category || '',
  });
}

export function onDiscussDeletion(btn) {
  if (!btn) return;
  const pid = parseInt(btn.dataset.pid || '0', 10);
  if (!pid) return;
  discussDeletion(
    pid,
    btn.dataset.title    || '',
    btn.dataset.pitch    || '',
    btn.dataset.category || '',
    btn.dataset.poster   || '',
  );
}

export function onApplyOrphanRepair(btn) {
  if (!btn) return;
  let sections;
  try { sections = JSON.parse(btn.dataset.sections || '[]'); }
  catch { sections = []; }
  applyOrphanRepair(sections);
}
