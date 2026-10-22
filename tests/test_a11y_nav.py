"""Accessibility and navigation: the last UI/UX items of the 2026-10 sweep
(docs/SILENT_FAILURE_SWEEP_2026_10.md, row "UI/UX").

  * #messages had no live region: a new reply was never announced. It is a
    role="log" (polite, additions); a streaming reply is aria-busy until it
    is complete, and a repaint (thread switch, restored history) is quiet
  * modals returned focus but did not trap it: Tab walked out of an open
    dialog into the page behind. ui.trapFocus cycles Tab/Shift+Tab inside
    the open dialog (openModal's or the onboarding overlay)
  * only Settings panes were in the hash; the Deletions category, the
    Knowledge Base tab and the arr page tabs are now #<view>/<tab> too,
    through one registry (nav.registerTabRoute) any view can join
  * --danger-fill was defined and never used
  * .thinking-status-log was --text3 under 0.55 x 0.85 opacity (~2.3:1)

The trap and the router run in node against the real functions with a
small DOM stub; markup and CSS are checked at source. Each check fails on
the code before this change.

    python tests/test_a11y_nav.py
"""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.frontend_files import markup, styles  # noqa: E402

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
    without the export keyword (same extractor as the r3 frontend suite)."""
    m = re.search(r"^(?:export )?(?:async )?function %s\(" % re.escape(name), src, re.M)
    if not m:
        return ""
    first = src[m.start():src.index("\n", m.start())]
    if first.count("{") and first.count("{") == first.count("}"):
        return first.removeprefix("export ")
    end = re.compile(r"^\}$", re.M).search(src, m.start())
    return src[m.start():end.end()].removeprefix("export ")


def _node(js: str):
    node = shutil.which("node")
    if not node:
        print("  (node not installed - behavioural checks skipped)")
        return None
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8", timeout=30)
    if r.returncode != 0:
        print(r.stderr[:800])
        return {}
    return json.loads(r.stdout.strip().splitlines()[-1])


# ── 1. #messages is a live log, quiet while streaming / repainting ──────────

def test_messages_live_region():
    m = markup()
    tag = re.search(r'<div id="messages"[^>]*>', m)
    tag = tag.group(0) if tag else ""
    check("#messages is role=log, polite, additions only",
          'role="log"' in tag and 'aria-live="polite"' in tag and 'aria-relevant="additions"' in tag)
    chat = _read("chat.js")
    send = _fn(chat, "sendMessage")
    busy_on = send.find("setAttribute('aria-busy', 'true')")
    reader = send.find("resp.body.getReader()")
    fin = send.find("} finally {")
    busy_off = send.find("setAttribute('aria-busy', 'false')")
    check("the streaming reply is aria-busy from the first frame until the turn ends",
          -1 < busy_on < reader and -1 < fin < busy_off)
    check("the thinking placeholder is announced as 'Thinking' (a labelled img, not a bare div)",
          'class="thinking-dots" role="img" aria-label="Thinking"' in chat)
    # landing panels inside the log are not conversation
    for pid in ("glance-panel", "last-played-panel", "suggested-prompts"):
        check(f"#{pid} is not read out as it fills",
              re.search(r'id="%s"[^>]*aria-live="off"' % pid, m) is not None
              and re.search(r'id="%s"[^>]*aria-live="off"' % pid, chat) is not None)
    quiet = _fn(chat, "_quietRepaint")
    out = _node("""
let _quietTimer = 0; const timers = [];
globalThis.setTimeout = f => { timers.push(f); return timers.length; };
globalThis.clearTimeout = () => {};
%s
const attrs = {'aria-live': 'polite'};
const msgs = {setAttribute(k, v) { attrs[k] = v; }};
const out = {};
_quietRepaint(msgs, () => { out.during = attrs['aria-live']; });
out.right_after = attrs['aria-live'];
timers.forEach(f => f());
out.later = attrs['aria-live'];
console.log(JSON.stringify(out));
""" % quiet) if quiet else {}
    if out is not None:
        check("a repaint runs with the log off, and it is live again afterwards",
              out.get("during") == "off" and out.get("right_after") == "off" and out.get("later") == "polite")
    check("thread switches and the restored history repaint quietly",
          "_quietRepaint(msgs" in _fn(chat, "_showThread") and "_quietRepaint(msgs" in _fn(chat, "restoreChatHistory"))


# ── 2. focus trap ────────────────────────────────────────────────────────────

_TRAP_HARNESS = r"""
const state = {_modal: null};
const mk = (name, visible = true) => ({name, hidden: false, getClientRects: () => visible ? [1] : [], focus() { document.activeElement = this; }});
const a = mk('close'), b = mk('input'), c = mk('ok'), ghost = mk('hidden-btn', false), outside = mk('page-link');
const box = {isConnected: true, querySelectorAll: () => [a, ghost, b, c], contains: n => [a, b, c, ghost].includes(n), getClientRects: () => [1]};
globalThis.document = {activeElement: null, querySelectorAll: () => []};
%s
%s
%s
const press = (at, shiftKey = false) => {
  document.activeElement = at;
  let prevented = false;
  trapFocus({key: 'Tab', shiftKey, preventDefault() { prevented = true; }}, box);
  return {to: document.activeElement.name, prevented};
};
const out = {};
out.wrap = press(c);                   // Tab on the last control
out.back = press(a, true);             // Shift+Tab on the first
out.middle = press(a);                 // Tab inside: the browser moves on
out.pulled = press(outside);           // focus outside the dialog
out.pulledBack = press(outside, true);
out.noModal = activeDialog();
state._modal = {el: box};
out.modal = activeDialog() === box;
state._modal = null;
const onboarding = {getClientRects: () => [1]}, hiddenDlg = {getClientRects: () => []};
document.querySelectorAll = sel => sel === '[role="dialog"][aria-modal="true"]' ? [hiddenDlg, onboarding] : [];
out.page = activeDialog() === onboarding;
console.log(JSON.stringify(out));
"""


def test_modal_focus_trap():
    ui, app = _read("ui.js"), _read("app.js")
    focusable = re.search(r"^const FOCUSABLE = .*;$", ui, re.M)
    trap, active = _fn(ui, "trapFocus"), _fn(ui, "activeDialog")
    out = _node(_TRAP_HARNESS % (focusable.group(0) if focusable else "", trap, active)) \
        if (focusable and trap and active) else {}
    if out is not None:
        check("Tab on the last control wraps to the first",
              out.get("wrap") == {"to": "close", "prevented": True})
        check("Shift+Tab on the first control wraps to the last",
              out.get("back") == {"to": "ok", "prevented": True})
        check("Tab between controls is left to the browser (hidden ones skipped)",
              out.get("middle") == {"to": "close", "prevented": False})
        check("a Tab from outside the open dialog lands inside it",
              out.get("pulled") == {"to": "close", "prevented": True}
              and out.get("pulledBack") == {"to": "ok", "prevented": True})
        check("the open dialog is openModal's, else a visible aria-modal one, else none",
              out.get("modal") is True and out.get("page") is True and out.get("noModal") is None)
    keydown = app[app.index("document.addEventListener('keydown', ev => {"):app.index("const actions = {")]
    check("the app's keydown listener traps every Tab", "trapFocus(ev, activeDialog())" in keydown)
    om = _fn(ui, "openModal")
    check("openModal renders role=dialog aria-modal and focuses the X when nothing else is focusable",
          'role="dialog" aria-modal="true"' in om and "el.querySelector('.modal-close')" in om)
    m = markup()
    onb = m[m.index('<div id="onboarding-overlay"'):][:400]
    check("the onboarding overlay (the one dialog openModal does not draw) is an aria-modal dialog",
          'role="dialog" aria-modal="true" aria-labelledby="onboarding-title"' in onb)
    check("opening onboarding moves focus into it",
          "focusDialog(overlay)" in _fn(_read("setup.js"), "showOnboarding"))


# ── 3. in-view tabs in the hash ──────────────────────────────────────────────

_ROUTER_HARNESS = r"""
const calls = [];
const log = {push: [], replace: []};
const state = {currentUser: {is_admin: true}, currentDelCategory: null, _kbTab: 'overview', currentRecsCategory: null, currentRecsLane: 'all'};
const _arrTab = {sonarr: 'all', radarr: 'all', lidarr: 'all'};
// The view modules' own setters, reduced to what the router relies on:
// they open the tab and report it with tabToHash (as deletions/kb/arr do).
function loadDeletions(cat) { state.currentDelCategory = cat; calls.push(['del', cat]); tabToHash('deletions', cat); }
function showKbTab(t) { state._kbTab = t || 'overview'; calls.push(['kb', state._kbTab]); tabToHash('enrich', state._kbTab); }
function loadArrPage(svc, tab) { if (tab) _arrTab[svc] = tab; calls.push(['arr', svc, tab]); tabToHash('arr-' + svc, _arrTab[svc]); }
function arrActiveTab(svc) { return _arrTab[svc]; }
let pane = 'account';
function openSettingsPane(p) { pane = p; calls.push(['settings', p]); }
function loadRecs() {} function searchLibrary() {} function loadHistoryStatus() {} function loadTaskHistory() {}
function loadReclassify() {} function loadReport() {}
const REC_LANES = ['all', 'library', 'discovery'];
function openRecLane(lane) { state.currentRecsLane = lane; calls.push(['lane', lane]); tabToHash('recs', lane); }
globalThis.CSS = {escape: s => s};
globalThis.location = {hash: ''};
globalThis.history = {
  pushState: (_, __, u) => { log.push.push(u); location.hash = u; },
  replaceState: (_, __, u) => { log.replace.push(u); location.hash = u; },
};
const cls = () => { const s = new Set(); return {add: c => s.add(c), remove: c => s.delete(c), contains: c => s.has(c), toggle: (c, on) => (on ?? !s.has(c)) ? s.add(c) : s.delete(c)}; };
const views = {};
for (const v of ['chat', 'deletions', 'enrich', 'settings', 'recs', 'arr-sonarr', 'arr-lidarr', 'demo']) views[v + '-view'] = {id: v + '-view', classList: cls(), style: {}, querySelector: () => null};
const chrome = {sidebar: {classList: cls()}, 'sb-backdrop': {classList: cls()}};
// ui.js motion helpers: the swap runs at once, no vibration motor here
const withTransition = fn => (fn(), Promise.resolve());
const haptic = () => {};
const tabs = (key, ids) => ids.map(id => ({dataset: {[key]: id}}));
globalThis.document = {
  getElementById: id => views[id] || chrome[id] || null,
  querySelector: sel => sel === '.view.active' ? Object.values(views).find(v => v.classList.contains('active')) || null
    : sel === '.settings-pane:not([hidden])' ? {dataset: {pane}} : null,
  querySelectorAll: sel => sel === '.view' ? Object.values(views)
    : sel === '#deletions-view .cat-tab[data-cat]' ? tabs('cat', ['', 'movie', 'show', 'anime', 'music'])
    : sel === '#kb-tabs .cat-tab[data-kbtab]' ? tabs('kbtab', ['overview', 'attention', 'maintenance', 'music', 'profiles'])
    : sel === '.settings-tab[data-pane]' ? tabs('pane', ['account', 'users', 'maintenance'])
    : [],
};
// A view module joining from its own top level, which can run before
// nav.js's body has (the modules import each other): one call.
let demoTab = 'a';
registerTabRoute('demo', {open: t => { demoTab = t; calls.push(['demo', t]); tabToHash('demo', t); },
                          current: () => demoTab, tabs: () => ['a', 'b'], fallback: 'a'});
%s
const out = {};
const go = h => { calls.length = 0; location.hash = h; routeFromHash({focus: false}); return {calls: calls.slice(), hash: location.hash}; };
out.del = go('#deletions/movie');
out.delBogus = go('#deletions/bogus');
out.kb = go('#enrich/attention');
out.arr = go('#arr-sonarr/anime');
out.settings = go('#settings/users');
out.settingsBogus = go('#settings/nope');
out.lane = go('#recs/discovery');
out.demo = go('#demo/b');
// a plain #view keeps the tab last open (backward compatible) and the hash names it
go('#chat');
state._kbTab = 'maintenance';
out.plain = go('#enrich');
// same view, same tab: nothing re-renders
out.same = go('#enrich/maintenance');
// picking a tab replaces the address, never pushes
const before = {push: log.push.length, replace: log.replace.length};
showKbTab('profiles');
out.pick = {hash: location.hash, pushed: log.push.length - before.push, replaced: log.replace.length - before.replace};
showKbTab('overview');
out.dflt = location.hash;
// a background reload of another view's data leaves the address alone
loadDeletions('show');
out.background = location.hash;
console.log(JSON.stringify(out));
"""


def _nav_as_script() -> str:
    src = _read("nav.js")
    src = re.sub(r"^import .*?;\s*$", "", src, flags=re.M)
    return re.sub(r"^export ", "", src, flags=re.M)


def test_tabs_are_in_the_hash():
    out = _node(_ROUTER_HARNESS % _nav_as_script())
    if out is None:
        check("(static) tab registry present", "registerTabRoute(" in _read("nav.js"))
        return
    check("#deletions/movie opens the Movies category",
          out.get("del", {}).get("calls") == [["del", "movie"]] and out["del"]["hash"] == "#deletions/movie")
    check("an unknown category falls back to All and the hash is corrected",
          out.get("delBogus", {}).get("calls") == [["del", None]] and out["delBogus"]["hash"] == "#deletions")
    check("#enrich/attention opens the Needs attention tab",
          out.get("kb", {}).get("calls") == [["kb", "attention"]])
    check("#arr-sonarr/anime hands the tab to the arr page",
          out.get("arr", {}).get("calls") == [["arr", "sonarr", "anime"]] and out["arr"]["hash"] == "#arr-sonarr/anime")
    check("Settings panes still route, an unknown pane opens Account",
          out.get("settings", {}).get("calls") == [["settings", "users"]]
          and out.get("settingsBogus", {}).get("calls") == [["settings", "account"]])
    check("the recommendation lane routes through the same registry",
          out.get("lane", {}).get("calls") == [["lane", "discovery"]] and out["lane"]["hash"] == "#recs/discovery")
    check("a view registered from another module (before nav.js ran) routes too",
          out.get("demo", {}).get("calls") == [["demo", "b"]] and out["demo"]["hash"] == "#demo/b")
    check("a plain #view still opens, on the tab last open, and the hash names it",
          out.get("plain", {}).get("calls") == [["kb", "maintenance"]] and out["plain"]["hash"] == "#enrich/maintenance")
    check("the same view and tab again re-renders nothing", out.get("same", {}).get("calls") == [])
    check("picking a tab replaces the hash (Back leaves the view)",
          out.get("pick") == {"hash": "#enrich/profiles", "pushed": 0, "replaced": 1})
    check("the default tab is left out of the hash", out.get("dflt") == "#enrich")
    check("a background load of another view does not rewrite the address", out.get("background") == "#enrich")
    for mod, needle in (("deletions.js", "tabToHash('deletions', category)"),
                        ("kb.js", "tabToHash('enrich', state._kbTab)"),
                        ("arr.js", "tabToHash('arr-' + svc, tabId)")):
        check(f"{mod} reports the selected tab", needle in _read(mod))
    arr = _fn(_read("arr.js"), "loadArrPage")
    check("the arr page validates a tab from the hash against the tabs it shows",
          "async function loadArrPage(svc, tab)" in arr and "!tabs.some(t => t.id === _arrActiveTab[svc])" in arr)


# ── 4 / 5. CSS tokens ────────────────────────────────────────────────────────

def _tokens(css: str) -> dict:
    root = css[css.index(":root {"):css.index("}", css.index(":root {"))]
    return dict(re.findall(r"(--[\w-]+):\s*([^;]+);", root))


def _rule(css: str, selector: str) -> str:
    m = re.search(r"(?:^|\n)%s\s*\{([^}]*)\}" % re.escape(selector), css)
    return m.group(1) if m else ""


def _lum(hex_):
    hex_ = hex_.lstrip("#")
    rgb = [int(hex_[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _blend(fg, bg, alpha):
    f, b = fg.lstrip("#"), bg.lstrip("#")
    return "#" + "".join("%02x" % round(alpha * int(f[i:i + 2], 16) + (1 - alpha) * int(b[i:i + 2], 16)) for i in (0, 2, 4))


def _ratio(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def test_danger_fill_is_used():
    css = styles()
    body = css[css.index("}", css.index(":root {")):]     # outside the token block
    rule = re.search(r"([^\n{}]*)\{[^}]*background:\s*var\(--danger-fill\)[^}]*\}", body)
    hard = re.findall(r"background:\s*(?:#(?:c93a3a|e55353)|rgba?\(2[0-9]{2},\s*[0-9]{1,2},)", body)
    check("--danger-fill paints the solid danger fill (white text, no hard-coded red beside it)",
          rule is not None and "color: #fff" in rule.group(0) and "--danger-fg" not in rule.group(0) and not hard)


def test_thinking_status_log_contrast():
    css = styles()
    tok = _tokens(css)
    log = _rule(css, ".thinking-status-log")
    child = _rule(css, ".thinking-status-log div")
    colour = re.search(r"color:\s*var\((--text\d?)\)", log)
    if not colour:
        check("the status trail uses a text-ladder token, not a literal colour", False)
        return
    alpha = 1.0
    for block in (log, child):
        op = re.search(r"opacity:\s*([\d.]+)", block)
        if op:
            alpha *= float(op.group(1))
    bubble = re.search(r"background:\s*var\((--[\w-]+)\)", _rule(css, ".msg.assistant")).group(1)
    fg, bg = tok[colour.group(1)].strip(), tok[bubble].strip()
    ratio = _ratio(_blend(fg, bg, alpha), bg)
    print(f"    status trail {colour.group(1)} x opacity {alpha:.2f} on {bubble}: {ratio:.2f}:1")
    # The app ships one palette (dark; there is no light theme to check).
    check("the status trail (a text-ladder token) clears 4.5:1 on the reply bubble", ratio >= 4.5)


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
