"""Silent-failure sweep, round 3: the frontend.

Each check fails on the code before this change:
  * boot: a failed library-config or history-status read dropped a
    configured admin into onboarding (auth.showApp); a failed Plex discover
    in onboarding was an empty table whose Save stored an empty mapping
  * recs: refresh-cache errors were discarded under "updates on its own",
    the poll swallowed every error, the 5-minute kill switch left that note
    up for good, and the 'none' sentinel let the OLD cache count as fresh
  * libraries: a failed Plex discover was "No Plex libraries found", cached
    for the session (SWR noAutoReplace)
  * onboarding: the sync poll swallowed errors for 30 min and stopped with
    the button stuck on "Syncing…"; the library save had no error path; a
    failed warm-up skipped the GPU-fit warning
  * history: taste-tab loads were .catch(()=>{})
  * KB: status / custodian failures read as "not running" and stopped the
    refresh; a failed backfill read removed the panel (and its Stop);
    smooth scroll ignored prefers-reduced-motion
  * game badge: a failed status read froze "Game mode" on screen
  * deletions: "Back after deletion" vanished on error; a lost Delete/Keep
    reason hid under a success toast; analysis errors were raw JSON with no
    Retry; Why? had no Escape and no guard-only variant
  * Spotify: the pending-files check never read r.ok
  * Plex sign-in: a failed PIN request did nothing; poll outages were silent
  * modals return focus on close; menus closed by Escape / outside click
    reset aria-expanded

    python tests/test_silent_failures_r3_frontend.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_JS = ROOT / "frontend" / "js"

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def _read(name: str) -> str:
    return (_JS / name).read_text(encoding="utf-8")


def _fn(src: str, name: str) -> str:
    """The source of a top-level ``[export] [async] function name(...)``,
    without the export keyword. A one-line function ends on its own line."""
    m = re.search(r"^(?:export )?(?:async )?function %s\(" % re.escape(name), src, re.M)
    assert m, f"{name}() not found"
    first = src[m.start():src.index("\n", m.start())]
    if first.count("{") and first.count("{") == first.count("}"):
        return first.removeprefix("export ")
    end = re.compile(r"^\}$", re.M).search(src, m.start())
    return src[m.start():end.end()].removeprefix("export ")


def _const(src: str, name: str) -> str:
    m = re.search(r"^(?:export )?const %s = .*?;$" % re.escape(name), src, re.M)
    assert m, f"const {name} not found"
    return m.group(0).removeprefix("export ")


def _nospace(s: str) -> str:
    return re.sub(r"\s+", "", s)


def _node(js: str):
    node = shutil.which("node")
    if not node:
        print("  (node not installed - checked statically)")
        return None
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        print(r.stderr[:800])
        return {}
    return json.loads(r.stdout.strip().splitlines()[-1])


_UI_STUBS = r"""
const esc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const escAttr = esc;
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const emptyHtml = (html, label, action) => `<div class="empty"><p>${html}</p>${label ? `<button ${action || ''}>${label}</button>` : ''}</div>`;
const _errMsg = e => { try { const j = JSON.parse(e?.message || ''); if (j?.detail) return j.detail; } catch {} return e?.message || 'Something went wrong'; };
const _errHtml = (e, retry) => `<p class="load-err">Couldn't load — ${esc(_errMsg(e))}</p>` + (retry ? `<button ${retry}>Retry</button>` : '');
const SVG_WARN = '<svg/>';
const EL = '$el';
const haptic = () => {};
"""


# ── 1. boot: unknown is not a first run ─────────────────────────────────────

def test_show_app_does_not_assume_first_run():
    show = _nospace(_fn(_read("auth.js"), "showApp"))
    check("library-config failure no longer reads as configured:false",
          ".catch(()=>({configured:false" not in show)
    check("history-status failure no longer reads as 0 entries",
          ".catch(()=>({watch_history_entries:0" not in show)
    check("onboarding only on a real answer",
          "if(cfg&&(!cfg.configured" in show and "if(hist&&hist.watch_history_entries===0)" in show)
    check("a failed check is toasted (amber) and the app opens",
          show.count("toast(") >= 2 and "'amber'" in show and "routeFromHash(" in show)

    step = _fn(_read("setup.js"), "renderOnboardingStep")
    check("onboarding discover failure is an error with Retry, not an empty table",
          ".catch(()=>({sections:[]}))" not in _nospace(step)
          and "_errHtml(e, act('retryOnboardingStep'))" in step)


# ── 2. recommendations ──────────────────────────────────────────────────────

def test_recs_refresh_and_poll_report_failures():
    recs = _read("recs.js")
    check("refresh-cache errors are not discarded",
          "refresh-cache', 'POST').catch(()=>{})" not in recs and "refresh-cache', 'POST').catch(() => {})" not in recs)
    start = _fn(recs, "_startRefresh")
    check("...409 (already running) keeps polling, anything else shows a Regenerate retry",
          "e.status === 409" in start and "_errHtml(e, act('regenerateRecs'))" in start)
    check("the 'none' sentinel is gone", "'none'" not in _fn(recs, "regenerateRecs"))
    poll = _fn(recs, "_pollRecsUntilFresh")
    check("the poll does not swallow errors", "catch {}" not in poll and "RECS_POLL_MAX_FAILS" in poll)

    js = _UI_STUBS + """
let timers = [], kills = [], loads = 0, answers = [];
const state = {currentRecsCategory: 'movie', _recsPollTimer: null, _recsPollKillswitch: null};
const el = {innerHTML: 'Regenerating…'};
globalThis.document = {getElementById: id => id === 'recs-content' ? el : null};
globalThis.setInterval = fn => { timers.push(fn); return timers.length; };
globalThis.clearInterval = () => {};
globalThis.setTimeout = fn => { kills.push(fn); return 99; };
globalThis.clearTimeout = () => {};
const api = async () => { const a = answers.shift(); if (a instanceof Error) throw a; return a; };
const loadRecs = () => { loads++; };
const _laneParam = () => '';
%s
%s
%s
%s
(async () => {
  const out = {};
  // stamp read failed (undefined): the old cache must not count as fresh
  answers = [{cached_at: 'OLD', recommendations: [1]}, {cached_at: 'OLD', recommendations: [1]}, {cached_at: 'NEW', recommendations: [1]}];
  _pollRecsUntilFresh('movie', null, undefined);
  let tick = timers[timers.length - 1];
  await tick(); out.afterFirst = loads; await tick(); out.afterSecond = loads; await tick(); out.afterNew = loads;
  // five errors in a row: the view says so with a Reload
  loads = 0; el.innerHTML = 'Regenerating…';
  answers = [1,2,3,4,5].map(() => Object.assign(new Error('{"detail":"Server down"}'), {status: 500}));
  _pollRecsUntilFresh('movie', null, 'OLD');
  tick = timers[timers.length - 1];
  for (let i = 0; i < 4; i++) await tick();
  out.after4 = el.innerHTML;
  await tick();
  out.after5 = el.innerHTML;
  // the 5-minute kill switch replaces the note with a Reload
  el.innerHTML = 'Regenerating…';
  _pollRecsUntilFresh('movie', null, 'OLD');
  kills[kills.length - 1]();
  out.killed = el.innerHTML;
  console.log(JSON.stringify(out));
})();
""" % (_const(recs, "RECS_POLL_MAX_FAILS"), _fn(recs, "_stopRecsPoll"), _fn(recs, "_recsFresh"), poll)
    out = _node(js)
    if out is None:
        check("(static) kill switch renders a Reload", "act('reloadRecs')" in poll)
        return
    check("unknown stamp: the first answer is the baseline, not fresh",
          out.get("afterFirst") == 0 and out.get("afterSecond") == 0)
    check("...a moved stamp after that reloads", out.get("afterNew") == 1)
    check("four poll errors stay quiet", out.get("after4") == "Regenerating…")
    check("the fifth shows the error with a Reload",
          "Server down" in out.get("after5", "") and 'data-action="reloadRecs"' in out.get("after5", ""))
    check("the kill switch replaces 'updates on its own' with a Reload",
          'data-action="reloadRecs"' in out.get("killed", "") and "Regenerating" not in out.get("killed", ""))


# ── 3. libraries ────────────────────────────────────────────────────────────

def test_libraries_plex_down_is_not_no_libraries():
    lib = _read("libraries.js")
    load = _fn(lib, "loadLibraryConfig")
    check("discover failure carries its error", "_err: _errMsg(e)" in load and ".catch(()=>({sections:[]}))" not in _nospace(load))
    check("...and is not cached for the session", "_swrInvalidate('libraries')" in load)
    check("Save refuses an empty section list", "if (!state.discoverSections.length)" in _fn(lib, "saveLibraries"))
    js = _UI_STUBS + _fn(lib, "_plexDownHtml") + """
console.log(JSON.stringify({html: _plexDownHtml({libraries: [{key: '1', title: 'Films', plex_type: 'movie', category: 'movie'}]}, 'timed out')}));
"""
    out = _node(js)
    if out is None:
        check("(static) banner text", "Plex unreachable — showing saved mapping" in lib)
        return
    html = out.get("html", "")
    check("amber banner: Plex unreachable — showing saved mapping",
          'banner warn' in html and "Plex unreachable — showing saved mapping" in html and "timed out" in html)
    check("...the saved mapping is listed, with a Retry", "Films" in html and 'data-action="loadLibraryConfig"' in html)


# ── 4. onboarding ───────────────────────────────────────────────────────────

def test_onboarding_sync_and_saves_report_failures():
    setup = _read("setup.js")
    sync = _fn(setup, "startOnboardingSync")
    poll = sync[sync.index("_onboardingPoll = setInterval"):]
    check("the sync poll does not swallow errors", "catch {}" not in poll)
    check("five misses give up with a message and a Retry",
          "ONBOARDING_POLL_MAX_FAILS" in poll and "giveUp(" in poll
          and "btn.textContent = 'Retry'" in sync)
    check("the 30-minute deadline also gives the button back",
          re.search(r"Date\.now\(\) > deadline\)\s*\{\s*giveUp\(", sync) is not None)
    save = _fn(setup, "saveOnboardingLibraries")
    check("saveOnboardingLibraries has an error path", "try {" in save and "toast(" in save and "btnDone(btn)" in save)
    build = _fn(setup, "buildOnboardingModels")
    # The warm-up became the mini-bench (2026-10-20); its failure text lives
    # in renderMinibench, which the build step always renders.
    render = _fn(setup, "renderMinibench")
    check("a failed warm-up / mini-bench is said, not skipped",
          "catch {}" not in build and "renderMinibench(mb, mbErr)" in build
          and "mini-bench could not run" in render and "is unknown" in render)
    check("retryOnboardingStep is registered", "  retryOnboardingStep," in _read("app.js"))


# ── 5. history ──────────────────────────────────────────────────────────────

def test_taste_tab_errors_render():
    hist = _read("history.js")
    tab = _fn(hist, "showTasteTab")
    check("taste tab loads are not .catch(()=>{})", ".catch(()=>{})" not in _nospace(tab))
    check("...each half renders _errHtml", "'taste-content'" in tab and "'recent-history'" in tab
          and "_errHtml(e, retry)" in _fn(hist, "_tasteErr"))


# ── 6. knowledge base ───────────────────────────────────────────────────────

def test_kb_status_unknown_and_backfill_kept():
    kb = _read("kb.js")
    load = _fn(kb, "loadEnrichStatus")
    check("status / custodian failures are not {} / null",
          ".catch(() => ({}))" not in load and ".catch(() => null)" not in load and "_err: e" in load)
    check("overview shows a status-unknown badge", "Status unknown" in _fn(kb, "_renderKbOverview"))
    check("custodian shows status unknown", "status unknown" in _fn(kb, "_renderCustodianUnknown"))
    out = _node(_fn(kb, "_kbStatusDelay") + """
console.log(JSON.stringify([
  _kbStatusDelay({_err: 1}, {}), _kbStatusDelay({}, {_err: 1}), _kbStatusDelay({running: true}, {}),
  _kbStatusDelay({}, {ticking: true}), _kbStatusDelay({}, {})]));
""")
    if out is not None:
        check("re-poll: 30 s while unknown, 8 s while running, none when idle", out == [30000, 30000, 8000, 8000, 0])
    bf = _fn(kb, "loadBackfillPanel")
    catch = bf[bf.index("catch (e)"):bf.index("if (!d || !d.any_offer)")]
    check("a failed backfill read keeps the panel and adds an error line",
          "el.innerHTML = ''" not in catch and "backfill-err" in catch)
    check("drilldown scroll honours prefers-reduced-motion",
          "prefers-reduced-motion: reduce" in _fn(kb, "loadKbItems") and "{behavior: 'smooth'" not in kb)


# ── 7. game badge ───────────────────────────────────────────────────────────

def test_game_badge_does_not_freeze():
    game = _read("game.js")
    check("status failure is not null", "/api/processes/status').catch(() => null)" not in game)
    js = """
const classes = new Set(['show']);
const indicator = {title: '', classList: {contains: c => classes.has(c), add: c => classes.add(c),
  toggle: (c, on) => on ? classes.add(c) : classes.delete(c)}};
const label = {textContent: 'Game mode'};
globalThis.document = {getElementById: id => id === 'game-indicator' ? indicator : id === 'game-indicator-label' ? label : null};
%s
%s
const out = {};
_renderLaneBadge({_err: 'timed out'});
out.label = label.textContent; out.title = indicator.title; out.busy = classes.has('busy');
_renderLaneBadge({lane: 'free'});
out.recovered = !classes.has('show');
classes.clear(); label.textContent = 'x';
_renderLaneBadge({_err: 'timed out'});
out.hiddenStays = !classes.has('show') && label.textContent === 'x';
console.log(JSON.stringify(out));
""" % (game[game.index("const LANE_BADGE"):game.index("\n};", game.index("const LANE_BADGE")) + 3],
       _fn(game, "_renderLaneBadge"))
    out = _node(js)
    if out is None:
        check("(static) unknown branch", "Status unknown" in game)
        return
    check("a shown badge says 'Status unknown' when the read fails",
          out.get("label") == "Status unknown" and "timed out" in out.get("title", "") and out.get("busy"))
    check("...and clears once the status answers", out.get("recovered") is True)
    check("a hidden badge is not raised by a failure", out.get("hiddenStays") is True)


# ── 8. deletions ────────────────────────────────────────────────────────────

def test_deletions_failures_are_said():
    dl = _read("deletions.js")
    ret = _fn(dl, "_loadReturned")
    check("Back-after-deletion failure is an error line, not an empty list",
          "catch { items = []; }" not in ret and "_renderReturnedError(" in ret)
    check("...which renders a load-err line", "Couldn't check for titles back after deletion" in _fn(dl, "_renderReturnedError"))
    for name in ("approveDelete", "rejectDelete"):
        fn = _fn(dl, name)
        check(f"{name}: the reason save is not .catch(()=>{{}})", ".catch(()=>{})" not in _nospace(fn) and "_saveReason(" in fn)
        check(f"{name}: the toast admits a lost reason", "+ lost" in fn or "${lost}" in fn)
    check("REASON_LOST suffix", "your reason couldn't be saved" in _const(dl, "REASON_LOST"))
    load = _fn(dl, "loadDeletions")
    check("analysis failure: _errHtml with a Run-analysis retry, not raw JSON in .loading",
          "esc(e.message || 'Analyse failed')" not in load
          and "_errHtml(e, act('loadDeletions', category || null, null, true))" in load)
    check("one delegated Escape listener for Why?",
          dl.count("addEventListener('keydown', _whyEscape)") == 1)

    js = _UI_STUBS + "\n".join(_fn(dl, n) for n in ("_whyButton", "_whyPanel", "_yourCallWhy", "toggleWhy", "_whyEscape")) \
        + "\n" + _const(dl, "_WHY_TOP") + "\n" + _const(dl, "_SHORTLIST_SCORE") + """
const out = {};
const guardOnly = {id: 7, score_factors: {factors: [], guard: 'household_floor'}};
out.btn = _whyButton(guardOnly);
out.panel = _whyPanel(guardOnly);
out.none = _whyButton({id: 8, score_factors: {factors: []}});
// Escape from inside the open panel closes it and focuses the button
const attrs = {'aria-expanded': 'true', 'aria-controls': 'why-7'};
let focused = false;
const panel = {id: 'why-7', hidden: false, closest: s => s.startsWith('.why-panel') ? panel : null};
const btn = {getAttribute: k => attrs[k], setAttribute: (k, v) => { attrs[k] = v; }, focus: () => { focused = true; }};
globalThis.document = {getElementById: id => id === 'why-7' ? panel : null, querySelector: () => btn};
let prevented = false;
_whyEscape({key: 'Escape', target: {closest: s => s.startsWith('.why-panel') ? panel : null}, preventDefault: () => { prevented = true; }});
out.closed = panel.hidden === true && attrs['aria-expanded'] === 'false' && focused && prevented;
console.log(JSON.stringify(out));
"""
    out = _node(js)
    if out is None:
        check("(static) guard shows Why?", "p.score_factors?.guard" in _fn(dl, "_whyButton"))
        return
    check("Why? renders for a guard-only row", "btn-why" in out.get("btn", ""))
    check("...its panel carries the household-floor sentence", "someone else in your household" in out.get("panel", ""))
    check("no factors and no guard: still no button", out.get("none") == "")
    check("Escape inside an open panel closes it and returns focus", out.get("closed") is True)


# ── 9. Spotify pending files ────────────────────────────────────────────────

def test_spotify_pending_checks_ok():
    sp = _read("spotify_import.js")
    check("refreshSpotifyPending reads r.ok", "r.ok" in _fn(sp, "refreshSpotifyPending"))
    out = _node(_fn(sp, "_spotifyPendingText") + """
console.log(JSON.stringify([
  _spotifyPendingText(false, {detail: 'Admin only'}, 403),
  _spotifyPendingText(false, null, 500),
  _spotifyPendingText(true, {pending: []}, 200),
  _spotifyPendingText(true, {pending: [{name: 'a.json'}]}, 200)]));
""")
    if out is None:
        return
    check("a 403 is an error with its reason", out[0] == {"text": "Couldn't check for uploaded files — Admin only", "err": True})
    check("a 500 without JSON is an error", out[1]["err"] is True and "HTTP 500" in out[1]["text"])
    check("ok + nothing pending is empty", out[2] == {"text": "", "err": False})
    check("ok + files lists them", out[3]["text"] == "1 file(s) waiting: a.json")


# ── 10. Plex sign-in ────────────────────────────────────────────────────────

def test_plex_login_failures_are_said():
    auth = _read("auth.js")
    start = _fn(auth, "startPlexLogin")
    check("PIN request failure is toasted", start.index("try {") < start.index("/api/auth/plex/pin")
          and "toast(`Couldn't start the Plex sign-in" in start)
    check("poll counts non-429/403 failures", "else if (++misses >= PLEX_POLL_MISSES) showMisses();" in start)
    check("...and writes to #poll-status", "getElementById('poll-status')" in start)
    out = _node(_const(auth, "PLEX_POLL_MISSES") + "\n" + _fn(auth, "_plexPollNote")
                + "\nconsole.log(JSON.stringify([_plexPollNote(0), _plexPollNote(2), _plexPollNote(3)]));")
    if out is not None:
        check("three misses: 'Plex sign-in is not answering — retrying'",
              out[0] == out[1] == "Waiting for authentication…" and out[2] == "Plex sign-in is not answering — retrying")


# ── 11. modal focus, menu aria-expanded ─────────────────────────────────────

def test_modal_returns_focus_and_menus_reset_aria():
    ui, app = _read("ui.js"), _read("app.js")
    js = _UI_STUBS + """
const state = {_modal: null};
const nodes = {};
const mkEl = () => ({classList: {add(){}, remove(){}}, set innerHTML(v) { this._h = v; this.firstElementChild = v ? {querySelector: () => null} : null; }, get innerHTML() { return this._h; }});
let focusedWith = [];
const opener = {isConnected: true, focus() { focusedWith.push('opener'); }};
globalThis.setTimeout = () => {};
globalThis.document = {
  activeElement: opener, body: {appendChild(el) { nodes[el.id] = el; }},
  getElementById: id => nodes[id] || null, createElement: () => mkEl(),
};
%s
%s
%s
%s
const out = {};
openModal({title: 'A'});
document.activeElement = {isConnected: true, focus() { focusedWith.push('inside-A'); }};
openModal({title: 'B'});                 // replaces A: no focus move yet
out.afterReplace = focusedWith.slice();
closeModal();
out.afterClose = focusedWith.slice();
out.modalCleared = state._modal === null;
// closeMenus resets the button
const btnAttrs = {'aria-expanded': 'true'};
const menu = {classList: {remove(c) { this.removed = c; }}, querySelector: () => ({setAttribute: (k, v) => { btnAttrs[k] = v; }})};
document.querySelectorAll = () => [menu];
closeMenus();
out.aria = btnAttrs['aria-expanded'];
console.log(JSON.stringify(out));
""" % (_fn(ui, "_mount"), _fn(ui, "openModal"), _fn(ui, "closeModal") + "\n" + _fn(ui, "_closeModal"), _fn(ui, "closeMenus"))
    out = _node(js)
    if out is None:
        check("(static) opener stored", "opener" in _fn(ui, "openModal"))
    else:
        check("replacing a modal does not move focus", out.get("afterReplace") == [])
        check("closing returns focus to the FIRST opener", out.get("afterClose") == ["opener"] and out.get("modalCleared"))
        check("closeMenus resets aria-expanded", out.get("aria") == "false")
    init = app[app.index("if (ev.key !== 'Escape') return;"):app.index("const actions = {")]   # init() is not indented
    check("Escape and outside clicks close menus through closeMenus",
          init.count("closeMenus(") >= 3 and "m.classList.remove('open')" not in init
          and "menu.classList.remove('open')" not in init)
    check("toggleMenu closes the others through closeMenus too", "closeMenus();" in _fn(ui, "toggleMenu"))


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            try:
                fn()
            except Exception as e:  # noqa: BLE001 - a crashed test is a failure, not a stop
                FAIL += 1
                print(f"  FAIL  {name} crashed: {type(e).__name__}: {e}")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
