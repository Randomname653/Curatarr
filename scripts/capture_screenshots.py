"""Capture the README screenshots from a running Curatarr.

    pip install playwright && python -m playwright install chromium
    python scripts/capture_screenshots.py --url http://localhost:8000 --token <token>
    python scripts/capture_screenshots.py --only phone       # one viewport
    python scripts/capture_screenshots.py --only wrapup      # one view, both viewports

The token is the session your browser already holds: sign in as the admin,
open the browser console and run ``localStorage.curatarr_token``. It can also
come from the ``CURATARR_TOKEN`` environment variable, which keeps it out of
the shell history. It is only ever sent to ``--url``.

Writes ``docs/screenshots/desktop/*.png`` and ``docs/screenshots/mobile/*.png``
(the shot list is ``SHOTS`` below; docs/screenshots/README.md says which
README section embeds which file). Every run produces the same frames:

* **fixed viewports** — 1440x900 at 1x for the desktop, 390x844 at 3x for
  the phone (an iPhone 14/15 frame; <= 768 px is where the app shell with
  the glass dock takes over, <= 640 px where tables become card stacks);
* **reduced motion**, so springs and fades are captured at rest rather than
  mid-flight;
* **the account name replaced** with ``--name`` before every shot. The
  maintainer's username was scrubbed from this repo's history, and a raw
  capture would put it straight back on the front page (the same reason
  prep_screenshots.py blurs the pill on hand-made captures).

Read-only: the script navigates, opens menus and steps through the Wrap-Up
story; it never approves,
rejects, sends or starts anything. Run it against an instance whose library
you are happy to publish — every title on screen ends up in the README.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

VIEWPORTS = {
    "desktop": {"viewport": {"width": 1440, "height": 900}, "device_scale_factor": 1},
    "phone": {"viewport": {"width": 390, "height": 844}, "device_scale_factor": 3,
              "is_mobile": True, "has_touch": True},
}

# name, viewport, hash route, selector that means "rendered", optional step
# before the shot ("click:<selector>" or "js:<expression>"). Views are
# #<name>-view (nav.showView marks the shown one .active); "settle" then
# waits for the posters on screen to fade in. The bottom sheet is the
# "Add to Home Screen" steps: read-only, and on a phone every dialog is
# the same sheet.
# The Wrap-Up opens on the calendar year, with its story closed for the
# bento; the story shots step through the slides to the one named.
_YEAR = "localStorage.setItem('curatarr_wrapup', JSON.stringify({scope: 'all', period: 'year', year: 2026}));"
_CLOSE_STORY = "js:document.querySelector('.wus-close')?.click()"


def _slide(cls: str) -> str:
    return ("js:(async () => { for (let i = 0; i < 40 && !document.querySelector('.wus-stage.wus-s-"
            + cls + "'); i++) { document.querySelector('.wus-next').click();"
            " await new Promise(r => setTimeout(r, 120)); } })()")


SHOTS = [
    ("wrapup", "desktop", "#wrapup", "#wrapup-view .wu-hero, #wrapup-view .wu-empty", _CLOSE_STORY, _YEAR),
    ("deletions", "desktop", "#deletions", "#deletions-view.active", None),
    ("chat", "desktop", "#chat", "#chat-view.active #chat-input-area", None),
    ("recommendations", "desktop", "#recs", "#recs-view.active",
     "click:#recs-view .cat-tab:nth-child(2)"),
    ("knowledge-base", "desktop", "#enrich", "#enrich-view.active", None),
    ("chat", "phone", "#chat", "#mobile-tabbar", None),
    ("wrapup", "phone", "#wrapup", "#wrapup-view .wu-hero, #wrapup-view .wu-empty", _CLOSE_STORY, _YEAR),
    ("story-clock", "phone", "#wrapup", ".wus .wus-stage", _slide("clock"), _YEAR),
    ("story-river", "phone", "#wrapup", ".wus .wus-stage", _slide("river"), _YEAR),
    ("story-consensus", "phone", "#wrapup", ".wus .wus-stage", _slide("consensus"), _YEAR),
    ("story-pair", "phone", "#wrapup", ".wus .wus-stage", _slide("pair"), _YEAR),
    ("swipe", "phone", "#swipe", "#swipe-view.active", None),
    ("deletions", "phone", "#deletions", "#deletions-view.active", None),
    ("history-cards", "phone", "#history", "#history-view.active", None),
    ("more-drawer", "phone", "#recs", "#mobile-tabbar", "click:#mt-more"),
    # The install button is hidden where the browser offers no install; its
    # data-action still runs on a click, and without a prompt event it opens
    # the steps dialog (mobile.installApp).
    ("bottom-sheet", "phone", "#chat", "#mobile-tabbar",
     "js:document.getElementById('pwa-install').click()"),
]

OUT_DIR = {"desktop": "desktop", "phone": "mobile"}


def _args():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--url", default="http://localhost:8000", help="the running Curatarr")
    p.add_argument("--token", default=os.environ.get("CURATARR_TOKEN", ""),
                   help="session token (default: $CURATARR_TOKEN)")
    p.add_argument("--name", default="Jane Doe", help="account name shown in the shots")
    p.add_argument("--only", default="", help="a shot name or a viewport (desktop / phone)")
    p.add_argument("--out", default=str(OUT), help="output root (default: docs/screenshots)")
    p.add_argument("--chromium", default="", help="Chromium binary, when not Playwright's own")
    p.add_argument("--timeout", type=float, default=20.0, help="seconds to wait for a view")
    return p.parse_args()


# The README shows phone frames 200 px wide: 3x is what the phone renders,
# 1.5x is plenty to store (a quarter of the bytes).
PHONE_STORED_WIDTH = 585


def _store_at(path: Path, width: int) -> None:
    from PIL import Image
    with Image.open(path) as im:
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
            im.save(path, optimize=True)


def _pseudonymise(page, name: str) -> None:
    page.evaluate("""(name) => {
        const n = document.getElementById('user-name');
        const a = document.getElementById('user-avatar');
        if (n) n.textContent = name;
        if (a) a.textContent = (name || '?')[0].toUpperCase();
    }""", name)


def _settle(page, timeout_ms: int) -> None:
    """Posters fade in once loaded (img.poster-img.is-loaded); wait for the
    ones in the viewport, then one more frame for the glow to take its tint."""
    try:
        page.wait_for_function("""() => {
            const imgs = [...document.querySelectorAll('img.poster-img')].filter(i => {
                const r = i.getBoundingClientRect();
                return r.bottom > 0 && r.top < innerHeight && r.width > 0;
            });
            return imgs.every(i => i.classList.contains('is-loaded') || i.complete);
        }""", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - a poster that never loads still leaves a usable frame
        pass
    page.wait_for_timeout(600)


def main() -> int:
    a = _args()
    if not a.token:
        print("No token: pass --token or set CURATARR_TOKEN "
              "(browser console: localStorage.curatarr_token).", file=sys.stderr)
        return 2
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed: pip install playwright && "
              "python -m playwright install chromium", file=sys.stderr)
        return 2

    shots = [s for s in SHOTS if not a.only or a.only in (s[0], s[1])]
    if not shots:
        print(f"No shot matches --only {a.only!r}.", file=sys.stderr)
        return 2
    base = a.url.rstrip("/")
    timeout_ms = int(a.timeout * 1000)
    failed = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**({"executable_path": a.chromium} if a.chromium else {}))
        for name, vp, route, ready, step, *init in shots:
            ctx = browser.new_context(**VIEWPORTS[vp], reduced_motion="reduce",
                                      color_scheme="dark", locale="en-US")
            # The token goes in before the app's first script runs, so the
            # first paint is the signed-in app, not the login screen.
            ctx.add_init_script(
                f"localStorage.setItem('curatarr_token', {a.token!r});" + (init[0] if init else ""))
            page = ctx.new_page()
            target = Path(a.out) / OUT_DIR[vp] / f"{name}.png"
            try:
                page.goto(base + "/" + route, wait_until="domcontentloaded")
                # auth.setUser fills the pill, then routes to the hash unless
                # the admin's library/sync onboarding takes the page instead.
                # A refused token lands on the login overlay.
                page.wait_for_function(
                    "() => document.getElementById('user-name')?.textContent !== 'Not signed in'"
                    " || !document.getElementById('auth-overlay')?.classList.contains('hidden')",
                    timeout=timeout_ms)
                page.wait_for_timeout(300)
                if page.locator("#auth-overlay:not(.hidden)").count():
                    raise RuntimeError("the login screen is showing: the token was refused")
                if not page.locator(f"#{route.lstrip('#')}-view.active").count():
                    raise RuntimeError(f"{route} did not open (is the instance still in "
                                       "first-run onboarding?)")
                page.wait_for_selector(ready, state="visible", timeout=timeout_ms)
                if step:
                    kind, _, arg = step.partition(":")
                    if kind == "click":
                        page.click(arg)
                    else:
                        page.evaluate(arg)
                    page.wait_for_timeout(500)
                _settle(page, timeout_ms)
                _pseudonymise(page, a.name)
                target.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(target), animations="disabled")
                if vp == "phone":
                    _store_at(target, PHONE_STORED_WIDTH)
                print(f"  ok  {target.relative_to(ROOT) if target.is_relative_to(ROOT) else target}")
            except Exception as e:  # noqa: BLE001 - one view failing should not cost the others
                failed += 1
                print(f"FAIL  {vp}/{name}: {type(e).__name__}: {str(e).splitlines()[0]}",
                      file=sys.stderr)
            finally:
                ctx.close()
        browser.close()
    print(f"{len(shots) - failed}/{len(shots)} captured.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
