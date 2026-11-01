"""The new logo (2026-10-29): one set of brand files, every icon rendered
from it, the UI drawing the mark from the same paths.

    python tests/test_brand.py
"""
import json
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _checks import check  # noqa: E402


BRAND = ROOT / "assets" / "brand"
names = ["curatarr-mark", "curatarr-mark-dark", "curatarr-mark-mono", "curatarr-mark-classic",
         "curatarr-logo", "curatarr-logo-dark", "curatarr-logo-inline", "curatarr-logo-inline-dark",
         "curatarr-app-icon", "curatarr-app-icon-classic"]
check("every brand file is there as SVG and PNG master",
      all((BRAND / f"{n}.svg").is_file() and (BRAND / f"{n}.png").is_file() for n in names))
svgs = {n: (BRAND / f"{n}.svg").read_text(encoding="utf-8") for n in names}
check("the SVGs carry no style attribute (the app's CSP refuses inline styles)",
      not any("style=" in s for s in svgs.values()))
check("the marks are square tiles", all(
    (lambda v: abs(float(v[2]) - float(v[3])) < 0.5)(re.search(r'viewBox="([^"]+)"', svgs[n]).group(1).split())
    for n in names if n.startswith("curatarr-mark")))

manifest = json.loads((ROOT / "frontend/manifest.json").read_text(encoding="utf-8"))
icons = {i["purpose"]: i["src"] for i in manifest["icons"]}
check("the manifest names an any, a maskable and a monochrome icon, and each exists",
      {"any", "maskable", "monochrome"} <= set(icons)
      and all((ROOT / "frontend" / src.lstrip("/")).is_file() for src in icons.values()))
mask = Image.open(ROOT / "frontend/icons/icon-maskable-512.png")
check("the maskable icon is full bleed (no transparent corner)", mask.convert("RGBA").getpixel((2, 2))[3] == 255)
anyi = Image.open(ROOT / "frontend/icons/icon-512.png").convert("RGBA")
check("the 'any' icon is the rounded tile (transparent corner)", anyi.getpixel((2, 2))[3] == 0)
apple = Image.open(ROOT / "frontend/icons/apple-touch-icon.png")
check("the iOS icon is opaque and 180 px (iOS rounds it itself)", apple.mode == "RGB" and apple.size == (180, 180))

html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
check("the favicon is the brand mark's file", '<link rel="icon" type="image/svg+xml" href="/icons/favicon.svg">' in html
      and (ROOT / "frontend/icons/favicon.svg").read_text(encoding="utf-8") == svgs["curatarr-mark"])
check("sidebar and login draw the wordmark, coloured by the brand tokens",
      html.count('class="brand-logo') >= 3 and 'id="brand-mark"' in html
      and 'class="brand-diamond"' in html and "fill=" not in html.split('id="logo"')[1][:5000])
css = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
check("...and the tokens exist", all(t in css for t in ("--brand-amber:", "--brand-diamond:", "--brand-ruby:")))
cur = (ROOT / "frontend/js/curation.js").read_text(encoding="utf-8")
check("the shutdown screen clones the mark instead of drawing the old eye",
      "brandMark('sd-logo')" in cur and 'viewBox="0 0 64 64"' not in cur)

from src import tray_app  # noqa: E402

img = tray_app._brand_icon(64)
check("the tray icon is the mark, 64 px", img.size == (64, 64) and img.getbbox() is not None)
check("the README shows the wordmark and says the brand is not AGPL",
      "assets/brand/curatarr-logo.svg" in (readme := (ROOT / "README.md").read_text(encoding="utf-8"))
      and "not covered by the AGPL-3.0" in readme)

# ── stage 2: classic, the thinking ring, the busy pulse, the watermark ──────
brand_js = (ROOT / "frontend/js/brand.js").read_text(encoding="utf-8")
check("the classic wordmark and ring are templates, the favicon has a classic twin",
      'id="brand-logo-classic"' in html and 'id="brand-ring"' in html
      and (ROOT / "frontend/icons/favicon-classic.svg").read_text(encoding="utf-8")
      == (BRAND / "curatarr-mark-classic.svg").read_text(encoding="utf-8"))
ring = html[html.index('id="brand-ring"'):]
ring = ring[:ring.index("</template>")]
check("the ring's four segments are numbered clockwise, once each",
      all(ring.count(f"brand-ring-{i} brand-seg brand-seg-{i}\"") == 1 for i in range(1, 5)))
check("Settings -> This device offers Modern / Classic, saved per browser",
      'name="icon-style"' in html and 'data-on-change="setIconStyle"' in html
      and "localStorage.setItem(ICON_KEY, v)" in brand_js and "/icons/favicon-classic.svg" in brand_js)
check("the logo slots switch, and the page applies the choice at start",
      html.count("data-brand-slot") == 2
      and "applyIconStyle();" in (ROOT / "frontend/js/app.js").read_text(encoding="utf-8"))
chat_js = (ROOT / "frontend/js/chat.js").read_text(encoding="utf-8")
check("a streaming reply marks a model at work, and its end clears it (finally)",
      "modelWorkStarted();" in chat_js
      and chat_js.index("} finally {\n    modelWorkEnded();".replace("\n", "\r\n") if "\r\n" in chat_js
                        else "} finally {\n    modelWorkEnded();") > chat_js.index("modelWorkStarted();"))
check("...and the server's status says so too",
      "if (!statusR._err) setServerModelBusy(statusR.model_busy);" in (ROOT / "frontend/js/game.js").read_text(encoding="utf-8")
      and '"model_busy": model_busy' in (ROOT / "src/routers/process_monitor.py").read_text(encoding="utf-8"))
check("one logo in the chat: the avatar is the favicon file (classic when chosen), no old eye",
      'background-image: url("/icons/favicon.svg");' in css
      and 'html.icon-classic .msg.assistant::before { background-image: url("/icons/favicon-classic.svg"); }' in css
      and "data:image/svg+xml" not in css.split(".msg.assistant::before {")[1][:600]
      and "classList.toggle('icon-classic'" in brand_js)


def _css_rules(text: str) -> dict:
    """{selector: {property: value}} for the stylesheet's plain rules, later
    rules winning like the cascade. CSS has no runtime in CI, so the badge
    checks read the file, but per rule: what a selector declares, not how
    a line happens to be spelled or where a brace falls."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    rules: dict = {}
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", text):
        for sel in m.group(1).split(","):
            decls = rules.setdefault(" ".join(sel.split()), {})
            for d in m.group(2).split(";"):
                prop, sep, val = d.partition(":")
                if sep:
                    decls[prop.strip()] = " ".join(val.split())
    return rules


rules = _css_rules(css)
keyframes = set(re.findall(r"@keyframes\s+([\w-]+)", css))


def _decl(selector: str, prop: str) -> str:
    return rules.get(selector, {}).get(prop, "")


check("the badge itself thinks: the C turns while its arcs breathe out, the ring folds and spins",
      _decl(".msg.assistant:has(> .thinking)::before", "display") == "none"
      and {"brand-turn", "brand-step", "brand-twinkle", *(f"brand-fold-{i}" for i in range(1, 5))} <= keyframes
      and _decl(".thinking-mark .brand-arcs", "transform-origin") == "1029.04px 1000px"
      and _decl(".thinking-mark .brand-ring.brand-seg", "transform-origin") == "1000px 989.62px"
      and "thinking-orbit" not in css
      and "thinkingIndicator()" in chat_js
      and "iconStyle() === 'classic' ? 'brand-ring' : 'brand-mark'" in brand_js)
# The Claude app's webview dropped a rotate animation while a transform
# animation ran on the same element: the turn lives on the arcs' group,
# the step on each arc. And paint containment clipped the stepped arcs.
_turn = _decl(".thinking-mark .brand-arcs", "animation")
_step = _decl(".thinking-mark .brand-arcs .brand-seg", "animation")
check("...turn and step run on different elements, and nothing clips the arcs",
      _turn.startswith("brand-turn ") and "brand-step" not in _turn
      and _step.startswith("brand-step ") and "brand-turn" not in _step
      and "paint" not in _decl(".thinking-mark", "contain"))
mark_t = html[html.index('<template id="brand-mark">'):]
mark_t = mark_t[:mark_t.index("</template>")]
check("...both marks carry four numbered segments and a sparkle group",
      all(mark_t.count(f"brand-seg-{i}\"") == 1 for i in range(1, 5)) and 'class="brand-sparkle"' in mark_t
      and all(ring.count(f"brand-seg-{i}\"") == 1 for i in range(1, 5)) and 'class="brand-sparkle"' in ring
      and mark_t.count('<g class="brand-arcs">') == 1
      and all(f"brand-seg-{i}\"" in mark_t[mark_t.index('<g class="brand-arcs">'):mark_t.index('<g class="brand-sparkle">')]
              for i in range(1, 5)))
check("the modern mark is square on its circle's centre (the C opens right, its box does not)",
      'viewBox="322.6 293.6 1413.0 1413.0"' in svgs["curatarr-mark"] and 'viewBox="322.6 293.6 1413.0 1413.0"' in mark_t)
_badge = re.fullmatch(r"(\d+)px", _decl(":root", "--chat-badge"))
_mark_px = re.fullmatch(r"(\d+)px", _decl(":root", "--chat-badge-mark"))
check("the avatar's mark has whole-pixel size, so it sits centred in the badge",
      _badge and _mark_px and int(_badge[1]) > int(_mark_px[1])
      and _decl(".msg.assistant::before", "background-size") == "var(--chat-badge-mark) var(--chat-badge-mark)")
# 2026-11-01: the thinking mark sat in the bubble at 24 px, the badge beside
# the finished reply at 14 px - two sizes in two places for one mark.
_slot = {p: _decl(".msg.assistant::before", p) for p in ("left", "top", "width", "height", "border")}
check("the thinking mark is the badge: same slot, size and circle, so the reply lands without a jump",
      all(_slot.values()) and {p: _decl(".thinking-mark", p) for p in _slot} == _slot
      and "padding-left" not in rules.get(".msg.assistant:has(> .thinking)", {}))
check("the user's badge mirrors it on the right: initial, or their picture once it loaded",
      _decl(".msg.user::after", "right") == _slot["left"]
      and _decl(".msg.user::after", "width") == _slot["width"]
      and _decl(".msg.user::after", "content") == "var(--me-initial, '')"
      and _decl("html.me-avatar .msg.user::after", "background-image") == "var(--me-avatar)")
check("the waiting text shimmers until the server's status lines take over, on the C's clock",
      _decl(".msg.assistant:has(.thinking-status) .thinking-label", "display") == "none"
      and _decl(".thinking-label", "animation") == "thinking-shimmer var(--think-cycle) linear infinite"
      and _turn.startswith("brand-turn var(--think-cycle) "))
check("the busy pulse breathes slowly and holds still under reduced motion",
      _decl("body.model-busy #logo .brand-spark", "animation") == "brand-breathe 3.2s ease-in-out infinite"
      and _decl("body.model-busy #logo .brand-diamond", "fill") == "var(--brand-ruby)"
      and _decl("body.model-busy #logo .brand-spark", "fill") == "var(--brand-ruby)")
check("the wrap-up carries the mark: the summary card and every story slide",
      "brandMarkHtml('wu-mark')" in (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
      and "brandMark('wus-mark')" in (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8"))

import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    tray_app._TRAY_PREFS = Path(tmp) / "tray.json"
    plain = tray_app._brand_icon(64).tobytes()

    class _Icon:
        icon = None
    ic = _Icon()
    tray_app._toggle_classic_icon(ic, None)
    check("the tray menu's switch saves the choice and swaps the icon",
          tray_app._classic_icon() is True and ic.icon is not None and ic.icon.tobytes() != plain)
    tray_app._toggle_classic_icon(ic, None)
    check("...and back", tray_app._classic_icon() is False and ic.icon.tobytes() == plain)

check.done()
