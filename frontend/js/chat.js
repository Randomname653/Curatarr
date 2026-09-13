// ── CHAT ──────────────────────────────────────────────────────────────────────
import { _errMsg, _posterImg, confirmDialog, esc, escAttr, proxyImg, toast } from './ui.js';
import { api } from './api.js';
import { state } from './state.js';
import { _updateKbBadge } from './kb.js';
import { showView } from './nav.js';
import { _deleteBody } from './deletions.js';
import { applyOrphanRepair } from './libraries.js';
export function handleKey(e) { if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendMessage();} }

export async function newChat() {
  // Clear conversation history for the active thread (free chat = "general")
  // and wipe the visible messages. Memories + library remain — only the
  // chat-message log gets reset, so the curator starts each new topic
  // without prior-turn baggage.
  const res = await confirmDialog({
    title: 'Clear conversation history', danger: true, confirmLabel: 'Clear',
    body: '<p>This deletes the chat log for this thread. Memories and the library are kept.</p>',
  });
  if (!res.ok) return;
  try {
    await api('/api/chat/history?thread_id=general', 'DELETE');
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
        <div id="suggested-prompts" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
          <button class="sp-chip" onclick="fillPrompt('What should I watch tonight?')">What should I watch tonight?</button>
          <button class="sp-chip" onclick="fillPrompt('What can I clean up in my library?')">What can I clean up in my library?</button>
          <button class="sp-chip" onclick="fillPrompt('What does my watch history say about my taste?')">What does my watch history say about my taste?</button>
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
    const r = await api('/api/chat/correct-anchor', 'POST', { thread_id: 'general' });
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

export async function sendMessage() {
  const inp = document.getElementById('chat-input');
  const btn = document.getElementById('send-btn');
  const text = inp.value.trim(); if (!text) return;
  inp.value = ''; inp.style.height = 'auto';
  inp.disabled = true;
  btn.disabled = true;
  document.getElementById('glance-panel')?.remove();
  document.getElementById('last-played-panel')?.remove();
  document.getElementById('suggested-prompts')?.remove();
  addMsg(text, 'user');
  const thinking = addThinkingMsg();

  try {
    const tok = localStorage.getItem('curatarr_token') || sessionStorage.getItem('curatarr_token') || '';
    // Build the payload and check whether we're coming from a "Discuss" button
    const payload = { message: text };
    if (state.pendingDiscussContext) {
      // Pass 81d: snapshot the context so we can clear one-shot flags on
      // the live object without retroactively mutating this turn's
      // payload. The ``reevaluate`` flag is one-shot — it fires the
      // Level-2 framing on the FIRST turn after the Reevaluate button,
      // then we strip it so follow-up turns ("but also check Director X")
      // don't re-inject the long framing every send.
      payload.discuss_context = { ...state.pendingDiscussContext };
      if (state.pendingDiscussContext.reevaluate) {
        state.pendingDiscussContext.reevaluate = false;
      }
    }

    const resp = await fetch('/api/chat/message', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${tok}`,
      },
      body: JSON.stringify(payload),
    });

    // Pass 27: state.pendingDiscussContext is now STICKY — keep it until the
    // user explicitly exits (Exit-discussion button), starts a new chat
    // (+ New), or clicks Discuss on a different proposal. Clearing after
    // every send routed follow-up turns to thread_id="general" which
    // (a) lost the proposal-pitch anchor (curator flipped its verdict)
    // and (b) leaked stale state from previous discussions (Star Trek
    // showing up in an American Dad! discussion).

    if (!resp.ok) {
      const err = await resp.json().catch(() => ({ detail: resp.statusText }));
      // Pydantic 422 detail is an array of validation error objects — flatten to readable string
      const detail = Array.isArray(err.detail)
        ? err.detail.map(e => e.msg || JSON.stringify(e)).join('; ')
        : (err.detail || resp.statusText);
      thinking.textContent = detail;
      return;
    }

    // Stream SSE tokens — keep the animated thinking dots visible UNTIL the
    // first real state.token arrives, otherwise the bubble flashes empty between
    // the spinner and the first character. Pass 14.4 fix: only clear the
    // dots inside the state.token branch, on the first iteration.
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';

    let fullResponseText = '';
    let firstTokenSeen = false;
    let warningBanner = null;
    let statusNode = null;
    let statusLog = null;        // accumulated status history (small grey trail)
    // Pre-stream events arrive in a quick burst (because the backend code
    // runs sequentially before yielding). To make them readable, we hold
    // each visible for 700ms AND keep a faded trail of the previous
    // statuses underneath so the user sees the full sequence even if the
    // first state.token arrives before the queue has fully drained.
    const statusQueue = [];
    let statusTimer = null;
    function showNextStatus() {
      if (statusQueue.length === 0) {
        statusTimer = null;
        return;
      }
      const next = statusQueue.shift();
      if (!statusNode) {
        statusNode = document.createElement('div');
        statusNode.className = 'thinking-status';
        thinking.appendChild(statusNode);
        statusLog = document.createElement('div');
        statusLog.className = 'thinking-status-log';
        thinking.appendChild(statusLog);
      }
      // If a status was already showing, push it down into the trail
      if (statusNode.textContent) {
        const trailItem = document.createElement('div');
        trailItem.textContent = statusNode.textContent;
        statusLog.appendChild(trailItem);
      }
      // Re-trigger the fade animation by removing + re-adding the class
      statusNode.style.animation = 'none';
      // eslint-disable-next-line no-unused-expressions
      statusNode.offsetHeight;
      statusNode.style.animation = '';
      statusNode.textContent = next;
      // 700ms minimum dwell per status so a burst still reads as a sequence
      statusTimer = setTimeout(showNextStatus, 700);
    }

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop(); // keep incomplete line
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const data = JSON.parse(line.slice(6));
          if (data.status) {
            // Push to queue; if no timer is running, start the drain.
            statusQueue.push(data.status);
            if (!statusTimer) showNextStatus();
            continue;
          }
          if (data.warning) {
            // VRAM fallback / health warning from the backend — shown as a
            // small banner above the streaming bubble so the user knows the
            // response will be slower than usual.
            if (!warningBanner) {
              warningBanner = document.createElement('div');
              warningBanner.className = 'banner ' + (data.severity === 'severe' ? 'danger' : 'warn');
              warningBanner.style.alignSelf = 'stretch';
              warningBanner.textContent = data.warning;
              thinking.parentElement?.insertBefore(warningBanner, thinking);
            }
          }
          if (data.token) {
            // First state.token in: replace the dots + status subnote with the
            // actual response text.
            if (!firstTokenSeen) {
              thinking.textContent = '';
              statusNode = null;
              statusQueue.length = 0;
              if (statusTimer) { clearTimeout(statusTimer); statusTimer = null; }
              firstTokenSeen = true;
            }
            fullResponseText += data.token;
            // marked.parse wandelt den String in HTML um, innerHTML rendert es.
            // DOMPurify dazwischen, weil marked rohes HTML durchreicht und der
            // Modell-Output Metadaten-Text aus fremden Quellen zitieren kann.
            thinking.innerHTML = DOMPurify.sanitize(marked.parse(fullResponseText));
          }
          if (data.done) {
            // Stream ended without any tokens (unusual — empty response).
            // Clear dots so the bubble doesn't sit empty-but-animating.
            if (!firstTokenSeen) {
              thinking.textContent = '(no response)';
            }
            break;
          }
        } catch {}
      }
    }

  } catch(e) {
    thinking.textContent = e.message;
  } finally {
    inp.disabled = false;
    btn.disabled = false;
    inp.focus();
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

  el.innerHTML = `<div class="stat-row" style="margin:16px 0 4px">` + tiles.map(t => `
    <div class="stat-box" style="cursor:pointer" onclick="showView('${t.view}', document.querySelector(&quot;.sb-item[onclick*='${t.view}']&quot;))">
      <div class="num"${t.small ? ' style="font-size:16px"' : ''}>${esc(String(t.num))}</div>
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
        return `<div class="lp-chip glow-interactive selectable" role="button" tabindex="0" aria-label="Discuss ${escAttr(title)}" onclick="discussLastPlayed(${i})" onkeydown="if(event.key==='Enter'||event.key===' '){event.preventDefault();this.click();}">
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
      `<button class="sp-chip" onclick="useStarter(${s.id}, this)">${esc(s.text)}</button>`
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
export function useStarter(id, btn) {
  const text = btn.textContent;
  api(`/api/chat/starters/${id}/used`, 'POST').catch(() => {});
  addMsg(text, 'assistant');
  state.pendingDiscussContext = { kind: 'starter', starter_id: id };
  _setDiscussBanner('Curator opened this thread');
  const inp = document.getElementById('chat-input');
  inp.value = '';
  inp.focus();
}
export function discussLastPlayed(i) {
  const e = state._lastPlayedEntries[i];
  if (!e) return;
  const isMusic = e.media_type === 'music';
  let title = e.title;
  if (isMusic && e.series_title) title = `${e.title} — ${e.series_title}`;
  else if ((e.media_type === 'anime' || e.media_type === 'show') && e.series_title) title = `${e.series_title}: ${e.title}`;

  // Structured context anchors on this exact watch-history row (history_id,
  // always present -- every /api/history/recent entry carries its own PK)
  // so the backend can inject real facts instead of trusting our text. Same
  // trust model + sticky state.pendingDiscussContext pipeline as discussDeletion/
  // respondToMessage: set it, then let sendMessage() pick it up below.
  if (e.id) {
    state.pendingDiscussContext = {
      kind: 'watched_title',
      history_id: e.id,
      series_title: e.series_title || null,
      title: e.title || null,
      category: e.media_type || null,
      season: e.season ?? null,
      episode: e.episode ?? null,
      viewed_at: e.viewed_at || null,
      poster_url: e.poster_url || null,
    };
    _setDiscussBanner(`Discussing "${title}"`);
  } else {
    state.pendingDiscussContext = null;
    _setDiscussBanner(null);
  }
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

// ── DISCUSS IN CHAT ───────────────────────────────────────────────────────────
export function discussInChat(type, data) {
  // Switch to chat view
  showView('chat', document.querySelector('.sb-item[onclick*=chat]'));

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
    const r = await api(`/api/recommendations/deletions/${id}/comment?comment=${encodeURIComponent(comment)}`, 'POST');
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
    banner.style.display = 'flex';
    // Pass 53: the "Delete & exit" button only makes sense for deletion
    // discussions — proactive-message threads have nothing to delete.
    // Relies on state.pendingDiscussContext being set BEFORE this call, which
    // both call sites (discussDeletion, discussInChat/proactive) do.
    if (delBtn) {
      const isDeletion = state.pendingDiscussContext?.kind === 'deletion_proposal';
      delBtn.style.display = isDeletion ? 'inline-block' : 'none';
      delBtn.disabled = false;
      delBtn.textContent = 'Delete & exit';
    }
  } else {
    banner.style.display = 'none';
    txt.textContent = '';
    if (delBtn) delBtn.style.display = 'none';
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
  msgs.style.backgroundImage = `linear-gradient(var(--bg-veil), var(--bg-veil)), url("${proxyImg(big)}")`;
}

// Pass 61: mirror of the backend _thread_id_for — derive the thread id from
// a discuss context so the frontend can target the right thread for the
// memory-flush endpoint.
export function _threadIdForContext(ctx) {
  if (!ctx) return 'general';
  if (ctx.kind === 'deletion_proposal' && ctx.proposal_id) return `deletion_proposal:${ctx.proposal_id}`;
  if (ctx.kind === 'proactive_message' && ctx.message_id) return `proactive_message:${ctx.message_id}`;
  return 'general';
}

export function exitDiscussion() {
  // Pass 61: "Exit discussion" is an explicit end-of-conversation signal —
  // flush the thread's pending memory extraction NOW instead of letting it
  // wait out the 90s debounce (or get lost if the user never returns to
  // this thread). Fire-and-forget — the UI shouldn't block on it. Must
  // read the thread id BEFORE we null state.pendingDiscussContext below.
  const tid = _threadIdForContext(state.pendingDiscussContext);
  if (tid && tid !== 'general') {
    api('/api/chat/flush-memories', 'POST', { thread_id: tid }).catch(() => {});
  }
  // Drops the sticky discuss_context so the next message is plain free chat.
  // Doesn't wipe chat history — that's what the "+ New" button is for.
  state.pendingDiscussContext = null;
  _setDiscussBanner(null);
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
    body: _deleteBody(title, 'This removes it from your *arr library and deletes the files. This cannot be undone.'),
  });
  if (!res.ok) return;

  const btn = document.getElementById('discuss-delete-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }

  try {
    // Lightweight audit trail — the actual reasoning lives in the thread
    // history, this is just a marker on the proposal row.
    await api(
      `/api/recommendations/deletions/${id}/comment?comment=${encodeURIComponent('Deleted after in-chat discussion')}`,
      'POST',
    ).catch(() => {});

    const r = await api(`/api/recommendations/deletions/${id}/approve`, 'POST');

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
        api('/api/chat/flush-memories', 'POST', { thread_id: tid }).catch(() => {});
      }
      addMsg(`Deleted "${title}". Discussion thread closed.`, 'assistant');
      // Close the discussion — same end-state as exitDiscussion(), minus
      // its "exited thread" note (we just posted a more specific one).
      state.pendingDiscussContext = null;
      _setDiscussBanner(null);
    } else {
      addMsg(`Delete failed for "${title}". Try again from the Deletions view.`, 'assistant');
      if (btn) { btn.disabled = false; btn.textContent = 'Delete & exit'; }
    }
  } catch (e) {
    addMsg(`Delete request failed for "${title}". Try again from the Deletions view.`, 'assistant');
    if (btn) { btn.disabled = false; btn.textContent = 'Delete & exit'; }
  }
}

export function discussDeletion(id, title, pitch, category, poster_url) {
  const userNote = (document.getElementById(`del-comment-${id}`)?.value || '').trim();

  // 1. Save the context for the next API send event. Backend looks up the
  //    proposal by id (with ownership check) and builds the RAG block from
  //    the trusted DB row — title/pitch are NOT forwarded to the server.
  //    `category` is forwarded only as a domain hint for RAG-quarantine.
  // Pass 27: ``state.pendingDiscussContext`` is now sticky across follow-up sends.
  // We update the banner so the user can SEE they're in a discussion thread
  // and have an exit hatch.
  state.pendingDiscussContext = {
    kind: 'deletion_proposal',
    proposal_id: id,
    category: category || null,
    // Pass 53: title is a legacy hint field — the backend ignores it
    // (it looks the proposal up by id), but the in-chat "Delete & exit"
    // button needs it for the confirm modal + the success note.
    title: title || null,
    poster_url: poster_url || null,
  };
  _setDiscussBanner(`Discussing deletion of "${title}"`);

  // 2. Switch to the chat view
  showView('chat', document.querySelector('.sb-item[onclick*=chat]'));

  // 3. Show the pitch IMMEDIATELY as an Assistant message in the chat window
  //    (UI-only; this is what the user sees, NOT what the backend persists).
  addMsg(`I have suggested "${title}" for deletion. Reason: "${pitch}"`, 'assistant');

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
