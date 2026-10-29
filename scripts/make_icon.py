"""Render every app icon from the brand masters in assets/brand/. Run from
the repo root after a brand file changes:

    python scripts/make_icon.py

The masters (1024 px PNG beside each SVG) come from the designer's files.
Written here:
- frontend/icons/: favicon.svg and favicon-classic.svg, icon-192/512
  (rounded tile, purpose "any"), icon-maskable-512 and apple-touch-icon (full bleed: Android masks and iOS
  rounds them itself; the mark sits inside the 80 % safe circle),
  icon-monochrome-512 (the mark's shape only, for themed icons and badges);
- assets/curatarr.ico (the autostart shortcut; the tray reads the mark PNG);
- docs/brand/social-preview.png (GitHub's social preview, 1280x640).
"""
import shutil
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "assets" / "brand"
ICONS = ROOT / "frontend" / "icons"
DOCS = ROOT / "docs" / "brand"
BG = (26, 29, 35, 255)          # --bg, the app's page colour


def _master(name: str) -> Image.Image:
    return Image.open(BRAND / f"{name}.png").convert("RGBA")


def _size(im: Image.Image, px: int) -> Image.Image:
    return im.resize((px, px), Image.LANCZOS)


def _rounded(im: Image.Image, px: int) -> Image.Image:
    """The designer's tile: corners at 22 % of the side, as in the app-icon file."""
    out = _size(im, px)
    mask = Image.new("L", (px * 4, px * 4), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, px * 4 - 1, px * 4 - 1], radius=round(px * 4 * 0.221), fill=255)
    out.putalpha(mask.resize((px, px), Image.LANCZOS))
    return out


def _monochrome(px: int) -> Image.Image:
    """White mark on transparent, inside the safe circle (purpose "monochrome"
    uses the alpha channel only)."""
    mark = _master("curatarr-mark-mono")
    inner = round(px * 0.62)
    tile = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    tile.alpha_composite(_size(mark, inner), ((px - inner) // 2, (px - inner) // 2))
    return tile


def _social() -> Image.Image:
    logo = Image.open(BRAND / "curatarr-logo.png").convert("RGBA")
    w = 860
    logo = logo.resize((w, round(logo.height * w / logo.width)), Image.LANCZOS)
    card = Image.new("RGBA", (1280, 640), BG)
    card.alpha_composite(logo, ((1280 - logo.width) // 2, (640 - logo.height) // 2))
    return card


def main() -> None:
    ICONS.mkdir(exist_ok=True)
    DOCS.mkdir(parents=True, exist_ok=True)
    app = _master("curatarr-app-icon")
    _rounded(app, 192).save(ICONS / "icon-192.png", optimize=True)
    _rounded(app, 512).save(ICONS / "icon-512.png", optimize=True)
    _size(app, 512).save(ICONS / "icon-maskable-512.png", optimize=True)
    _size(app, 180).convert("RGB").save(ICONS / "apple-touch-icon.png", optimize=True)
    _monochrome(512).save(ICONS / "icon-monochrome-512.png", optimize=True)
    shutil.copyfile(BRAND / "curatarr-mark.svg", ICONS / "favicon.svg")
    shutil.copyfile(BRAND / "curatarr-mark-classic.svg", ICONS / "favicon-classic.svg")
    _master("curatarr-mark").save(ROOT / "assets" / "curatarr.ico",
                                  sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (256, 256)])
    _social().convert("RGB").save(DOCS / "social-preview.png", optimize=True)
    print(f"wrote {ICONS}, assets/curatarr.ico, {DOCS / 'social-preview.png'}")


if __name__ == "__main__":
    main()
