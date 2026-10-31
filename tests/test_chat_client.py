"""Chat client: the stream controller, the thread the pane and + New act on,
the guarded discussion entry points, and the single task stream.

2026-10-15 audit: + New wiped the free-chat log from inside a discussion;
a stalled stream locked the composer for good; per-token markdown parsing
grew quadratically; a 409 burned the one-shot Level-2 flag; the history
pane never loaded; two EventSources could open at once; and a quote in a
poster URL opened a second url() past the image proxy. Node runs the pure
helpers where it is installed (CI has it); the wiring is pinned statically.

    python tests/test_chat_client.py
"""
import json
import pathlib
import re
import shutil
import subprocess
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
_JS = _ROOT / "frontend" / "js"


def _read(name: str) -> str:
    return (_JS / name).read_text(encoding="utf-8")


def _function(src: str, name: str) -> str:
    """The source of a top-level ``export function name(...) {...}``."""
    m = re.search(r"^export (?:async )?function %s\(.*?^\}$" % re.escape(name), src, re.M | re.S)
    assert m, f"{name}() not found"
    return m.group(0).removeprefix("export ")


def _node(js: str) -> str | None:
    node = shutil.which("node")
    if not node:
        print("  (node not installed - checked statically)")
        return None
    r = subprocess.run([node, "-e", js], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr[:800]
    return r.stdout


# ── the thread id: one rule on both sides ───────────────────────────────────

_CONTEXTS = [
    None,
    {"kind": "deletion_proposal", "proposal_id": 12},
    {"action": "deletion", "proposal_id": 12},
    {"kind": "deletion_proposal"},
    {"kind": "proactive_message", "message_id": 4},
    {"kind": "principle", "principle_id": 9},
    {"kind": "starter", "starter_id": 3},
    {"kind": "watched_title", "tmdb_id": 1234, "series_title": "Ignored"},
    {"type": "watched_title", "series_title": "Kill la Kill!  (2013)", "title": "Ep 1"},
    {"kind": "watched_title", "title": "Amélie — Le Fabuleux Destin"},
    {"kind": "watched_title", "title": "x" * 120},
    {"kind": "watched_title", "title": "!!!"},
    {"kind": "something_new", "proposal_id": 1},
]


def test_the_client_names_the_same_thread_as_the_server():
    """+ New, the anchor re-fetch, the memory flush and the history pane all
    target the client's thread id. Principle / starter / watched-title
    threads used to fall through to "general" here."""
    from src.routers.chat import _thread_id_for
    from src.schemas.chat import DiscussContext
    want = [_thread_id_for(DiscussContext(**c) if c else None) for c in _CONTEXTS]
    fn = _function(_read("chat.js"), "_threadIdForContext")
    out = _node(fn + f"\nprocess.stdout.write(JSON.stringify({json.dumps(_CONTEXTS)}.map(_threadIdForContext)));")
    if out is None:
        for kind in ("principle", "starter", "watched_title", "proactive_message", "deletion_proposal"):
            assert f"'{kind}'" in fn, f"_threadIdForContext lost the {kind} thread"
        return
    assert json.loads(out) == want, f"client {out} != server {want}"


def test_a_poster_url_cannot_open_a_second_css_url():
    fn = _function(_read("chat.js"), "_cssUrl")
    cases = ['/a"), url("https://tracker.example/x', "/poster.jpg", "javascript:alert(1)",
             "https://image.tmdb.org/t/p/w780/a'b.jpg", "/a\\\"), url(//evil"]
    out = _node("globalThis.location = {origin: 'http://curatarr.local'};\n" + fn
                + f"\nprocess.stdout.write(JSON.stringify({json.dumps(cases)}.map(_cssUrl)));")
    if out is None:
        assert "encodeURIComponent" in fn and "new URL(" in fn
        return
    got = json.loads(out)
    for raw, css in zip(cases, got):
        if css is None:
            continue
        inner = css[len('url("'):-len('")')]
        # Inside a double-quoted url() only a quote, a backslash or a newline ends it.
        assert css.startswith('url("') and css.endswith('")') and not re.search(r'["\\\n\r]', inner), \
            f"{raw!r} -> {css!r}"
    assert got[2] is None, "a non-http(s) URL must not reach url()"
    assert got[1] == 'url("http://curatarr.local/poster.jpg")'


# ── the stream controller ───────────────────────────────────────────────────

def test_the_stream_is_abortable_and_watched():
    chat = _read("chat.js")
    send = _function(chat, "sendMessage")
    assert "new AbortController()" in send and "signal: ctrl.signal" in send
    assert "arm(STREAM_IDLE_MS)" in send and "arm(STREAM_HEADERS_MS)" in send, \
        "a stalled read must not hold the composer for good"
    m = re.search(r"const STREAM_IDLE_MS = ([\d_]+);", chat)
    assert m and int(m.group(1).replace("_", "")) >= 3 * 15_000, \
        "the idle limit must cover several of the server's 15 s keepalives"
    assert "requestAnimationFrame(paint)" in send, "one markdown parse per frame, not per token"
    assert "thinking.innerHTML = renderMarkdown(fullResponseText)" not in chat
    # The partial reply survives a dropped connection; the note goes beside it.
    assert "thinking.textContent = e.message" not in chat
    assert "if (_busyStreaming()) return;" in send


def test_the_one_shot_reevaluate_flag_survives_a_refused_send():
    send = _function(_read("chat.js"), "sendMessage")
    assert send.index("if (!resp.ok)") < send.index("ctx.reevaluate = false"), \
        "a 409 must not spend the Level-2 framing"


def test_new_chat_clears_the_active_thread_and_stops_the_stream():
    new = _function(_read("chat.js"), "newChat")
    assert "thread_id=general" not in new, "+ New wiped free chat from inside a discussion"
    assert "const tid = activeThreadId();" in new
    assert new.index("abortActiveStream()") < new.index("'DELETE'"), \
        "a reply still streaming would land in the cleared thread"


def test_the_anchor_refetch_targets_the_active_thread():
    fn = _function(_read("chat.js"), "correctChatAnchor")
    assert "thread_id: activeThreadId()" in fn and "thread_id: 'general'" not in fn


# ── the stream watchdog, run for real ───────────────────────────────────────
# The real sendMessage (with the real limits and abort helpers) on a virtual
# clock: a fake fetch whose body yields scripted frames at virtual times, a
# minimal DOM, and timers that only fire when the test advances the clock.

_RIG = r"""
const T = [];                                   // virtual timers: {at, fn, id}
let NOW = 0, NEXT_ID = 1;
globalThis.setTimeout = (fn, ms) => { const id = NEXT_ID++; T.push({at: NOW + (ms || 0), fn, id}); return id; };
globalThis.clearTimeout = id => { const i = T.findIndex(t => t.id === id); if (i >= 0) T.splice(i, 1); };
const flush = () => new Promise(r => setImmediate(r));
async function advance(ms) {                    // fire due timers in time order
  const end = NOW + ms;
  for (;;) {
    await flush(); await flush();
    T.sort((a, b) => a.at - b.at);
    if (!T.length || T[0].at > end) break;
    const t = T.shift(); NOW = t.at; t.fn();
  }
  NOW = end; await flush(); await flush();
}
function el(id) {
  return {id, textContent: '', innerHTML: '', value: '', disabled: false, style: {},
          isConnected: true, parentElement: null, previousElementSibling: null, siblings: [],
          classList: {contains: () => false, add() {}, remove() {}},
          setAttribute(k, v) { this[k] = v; }, focus() {}, remove() {},
          before(x) { this.siblings.push(x); }, after(x) { this.siblings.push(x); }};
}
const DOM = {'chat-input': el('chat-input'), 'send-btn': el('send-btn'), messages: el('messages')};
globalThis.document = {getElementById: id => DOM[id] || null, createElement: tag => el(tag)};
globalThis.requestAnimationFrame = fn => setTimeout(fn, 16);
globalThis.cancelAnimationFrame = id => clearTimeout(id);
globalThis.localStorage = globalThis.sessionStorage = {getItem: () => ''};
const state = {token: 't', pendingDiscussContext: null};
const BUBBLE = el('bubble');
const haptic = () => {}, addMsg = () => {}, _follow = () => {}, _atBottom = () => true,
      toast = () => {}, loadUnreadMessages = () => {}, renderMarkdown = s => s,
      addThinkingMsg = () => BUBBLE, _threadIdForContext = () => 'general';
let WORK = 0;
const modelWorkStarted = () => WORK++, modelWorkEnded = () => WORK--;
const _statusTicker = () => ({push() {}, stop() {}});

// The fake server: SCRIPT = {headersAt, frames: [[t, text], ...], endAt}
let SCRIPT = null, FETCHED = null;
globalThis.fetch = (url, opts) => new Promise((resolve, reject) => {
  FETCHED = {url, opts};
  const sig = opts.signal;
  const abortErr = () => Object.assign(new Error('aborted'), {name: 'AbortError'});
  sig.addEventListener('abort', () => reject(abortErr()));
  setTimeout(() => {
    const queue = SCRIPT.frames.map(([t, text]) => ({t, text}));
    let waiting = null;
    const enc = new TextEncoder();
    for (const f of queue) setTimeout(() => {
      f.ready = true; if (waiting) { const w = waiting; waiting = null; w(); }
    }, f.t);
    if (SCRIPT.endAt != null) setTimeout(() => { SCRIPT.ended = true; if (waiting) { const w = waiting; waiting = null; w(); } }, SCRIPT.endAt);
    resolve({ok: true, body: {getReader: () => ({
      read: () => new Promise((res, rej) => {
        sig.addEventListener('abort', () => rej(abortErr()));
        const take = () => {
          const f = queue.find(q => q.ready && !q.taken);
          if (f) { f.taken = true; return res({done: false, value: enc.encode(f.text)}); }
          if (SCRIPT.ended) return res({done: true});
          waiting = take;
        };
        take();
      })})}});
  }, SCRIPT.headersAt);
});
"""


def _watchdog(script: dict, steps: list) -> list | None:
    """Run the real sendMessage against ``script``; after each advance in
    ``steps`` (virtual ms) record what a user would see."""
    chat = _read("chat.js")
    consts = "\n".join(re.findall(r"^const STREAM_(?:IDLE|HEADERS)_MS = [\d_]+;$", chat, re.M))
    assert consts.count("const") == 2, "the stream limits moved"
    body = "\n".join([consts, "let _stream = null;", _function(chat, "abortActiveStream"),
                      _function(chat, "_busyStreaming"), _function(chat, "sendMessage")])
    drive = f"""
SCRIPT = {json.dumps(script)};
DOM['chat-input'].value = 'hello';
(async () => {{
  const out = [];
  const turn = sendMessage();
  for (const ms of {json.dumps(steps)}) {{
    await advance(ms);
    out.push({{t: NOW, streaming: !!_stream, composer: !DOM['chat-input'].disabled,
              text: BUBBLE.textContent || BUBBLE.innerHTML,
              note: BUBBLE.siblings.map(s => s.textContent).join(' | '), work: WORK}});
  }}
  await advance(1); await turn;
  process.stdout.write(JSON.stringify(out));
}})();
"""
    out = _node(_RIG + body + drive)
    return None if out is None else json.loads(out)


_TOKEN = 'data: {"token": "%s"}\n'
_DONE = 'data: {"done": true}\n'


def test_a_stream_that_goes_quiet_after_the_headers_is_dropped_at_45_s():
    got = _watchdog({"headersAt": 100, "frames": []}, [100 + 44_000, 2_000])
    if got is None:
        return
    at_44, at_46 = got
    assert at_44["streaming"] and not at_44["composer"], at_44
    assert not at_46["streaming"] and at_46["composer"], "a dead stream must free the composer"
    assert "went quiet" in at_46["text"], at_46
    assert at_46["work"] == 0, "the model-work indicator must end with the turn"


def test_keepalives_hold_a_slow_turn_open_until_its_reply():
    # The server's 15 s keepalives for two minutes (queue, cold load, think),
    # then the reply: 2 min > 45 s, yet the watchdog never fires.
    frames = [[100 + 15_000 * i, ": keepalive\n\n"] for i in range(1, 9)]
    # The reply's first token shares its chunk with a keepalive: the comment
    # line must be skipped, not end the parse of the chunk.
    frames += [[121_000, ": keepalive\n\n" + _TOKEN % "Hello"], [121_100, _DONE]]
    got = _watchdog({"headersAt": 100, "frames": frames, "endAt": 121_200}, [120_000, 2_000])
    if got is None:
        return
    mid, end = got
    assert mid["streaming"] and "went quiet" not in mid["text"], mid
    assert not end["streaming"] and end["text"] == "Hello" and end["note"] == "", end


def test_the_header_phase_gets_its_own_wider_limit():
    # No headers yet (the server is still building context): 45 s is not
    # enough to give up, 300 s is.
    got = _watchdog({"headersAt": 10_000_000, "frames": []}, [200_000, 101_000])
    if got is None:
        return
    at_200, at_301 = got
    assert at_200["streaming"], "the header phase must outlast the 45 s idle limit"
    assert not at_301["streaming"] and "went quiet" in at_301["text"], at_301


def test_a_partial_reply_survives_the_drop_with_a_note_beside_it():
    got = _watchdog({"headersAt": 100, "frames": [[200, _TOKEN % "Half a "]]}, [1_000, 46_000])
    if got is None:
        return
    _, dropped = got
    assert dropped["text"] == "Half a ", "the partial reply must stay on screen"
    assert "went quiet" in dropped["note"] and "not saved" in dropped["note"], dropped


# ── the thread pane ─────────────────────────────────────────────────────────

def test_every_way_into_a_thread_is_guarded_and_loads_its_history():
    chat, dels, notes = _read("chat.js"), _read("deletions.js"), _read("notifications.js")
    entries = [(chat, "discussDeletion"), (chat, "useStarter"), (chat, "discussLastPlayed"),
               (dels, "onReevaluateDeletion"), (notes, "discussPrinciple"),
               (notes, "respondToMessage")]
    for src, name in entries:
        fn = _function(src, name)
        assert "_busyStreaming()" in fn, f"{name}() switches threads under a running reply"
        assert "await enterThread(" in fn, f"{name}() does not load the thread's history"
        assert "state.pendingDiscussContext = " not in fn, f"{name}() sets the context around enterThread()"
    assert "_busyStreaming()" in _function(chat, "discussInChat")
    exit_ = _function(chat, "exitDiscussion")
    assert "_busyStreaming()" in exit_ and "await _showThread('general')" in exit_
    show = _function(chat, "enterThread")
    assert "_showThread(_threadIdForContext(ctx), opener)" in show


def test_a_history_load_never_paints_over_a_newer_thread():
    chat = _read("chat.js")
    show = re.search(r"async function _showThread\(.*?^\}$", chat, re.M | re.S).group(0)
    assert show.index("++_threadSeq") < show.index("await _loadThread") < show.index("seq !== _threadSeq")
    assert "_threadSeq++" in _function(chat, "newChat")
    restore = _function(chat, "restoreChatHistory")
    assert "seq !== _threadSeq" in restore and ".msg.user" in restore, \
        "the boot restore must not clobber a turn the user already sent"
    bubble = re.search(r"function _historyBubble\(.*?^\}$", chat, re.M | re.S).group(0)
    assert "renderMarkdown(" in bubble and "div.textContent = m.content" in bubble, \
        "saved replies are model output: sanitized markdown; user turns are text"


def test_the_pane_is_restored_on_boot():
    auth = _read("auth.js")
    assert "restoreChatHistory } from './chat.js'" in auth
    assert "restoreChatHistory();" in _function(auth, "showApp")


def test_a_protection_from_chat_reaches_the_deletions_cache():
    send = _function(_read("chat.js"), "sendMessage")
    assert "me.threadId.startsWith('deletion_proposal:')" in send
    assert "state._delProposalsAll = null" in send and "loadUnreadMessages()" in send


def test_a_slow_deletions_load_cannot_overwrite_a_newer_one():
    fn = _function(_read("deletions.js"), "loadDeletions")
    assert fn.count("seq !== _delLoadSeq") >= 2 and "const seq = ++_delLoadSeq;" in fn


# ── the task stream ─────────────────────────────────────────────────────────

def test_only_one_task_stream_opens():
    act = _read("activity.js")
    start = _function(act, "startTaskStream")
    assert "_taskStreamOpening" in start, "two callers could both pass the guard"
    opener = re.search(r"async function _openTaskStream\(.*?^\}$", act, re.M | re.S).group(0)
    after_await = opener[opener.index("await r.json()"):]
    assert "if (state.taskEventSource) return;" in after_await, "no re-check after the ticket await"
    assert "if (state.taskEventSource !== es) return;" in opener, \
        "a stale onerror must not clear a newer stream"
    assert "r.status === 401" in opener, "an expired session retried every 3 s forever"
    assert "setTimeout(startTaskStream" not in opener, "retries go through the one retry timer"
    for name in ("auth.js", "curation.js"):
        src = _read(name)
        assert "stopTaskStream()" in src and "taskEventSource.close()" not in src, name


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    print(f"\n{'all passed' if not fails else f'{fails} failed'}")
    sys.exit(1 if fails else 0)
