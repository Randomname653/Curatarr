"""Prepare UI screenshots for the public docs.

    python scripts/prep_screenshots.py shot1.png shot2.png ...

For each input it writes an optimised copy into docs/screenshots/:

* **redacts the account pill** in the top-right corner. The maintainer's
  username was deliberately scrubbed from this repo's history — a raw
  capture would put it straight back on the front page.
* **trims the dead space** below the content. Measured on the main pane
  only, and by counting pixels per row rather than taking a bounding box:
  the nav rail spans the full window height and the window has a 1 px
  border, either of which drags a naive bounding box to the last row and
  trims nothing.
* **downscales** to a width that stays readable when GitHub renders a
  README image at roughly 850 px.

Idempotent per input file — re-run it whenever the UI changes.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageChops, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"

MAX_WIDTH = 1600          # plenty for a 2x display, still a small file
PILL_W, PILL_H = 190, 34  # top-right account pill, in source pixels
SIDEBAR_MAX = 260         # nav rail width; it runs the full window height
FULL_WINDOW_MIN = 800     # narrower than this = a cropped panel, not a window
EDGE = 14                 # ignore window borders / scrollbars at the sides
PAD = 16                  # breathing room kept below the last content row


def _background(pane: Image.Image):
    """The pane's most common colour.

    Not a corner sample: a capture's outermost row can carry a window
    border or desktop bleed, and sampling that makes every genuine
    background row read as content — which silently disables trimming.
    """
    flat = np.asarray(pane).reshape(-1, 3).astype(np.int32)
    packed = (flat[:, 0] << 16) | (flat[:, 1] << 8) | flat[:, 2]
    values, counts = np.unique(packed, return_counts=True)
    mode = int(values[counts.argmax()])
    return (mode >> 16) & 255, (mode >> 8) & 255, mode & 255


def _content_bottom(im: Image.Image) -> int:
    """Last row of the main pane that actually carries content."""
    x0 = SIDEBAR_MAX if im.width >= FULL_WINDOW_MIN else 0
    pane = im.crop((x0 + EDGE, 0, max(x0 + EDGE + 1, im.width - EDGE),
                    max(1, im.height - EDGE)))
    mask = ImageChops.difference(pane, Image.new(pane.mode, pane.size,
                                                 _background(pane)))
    hits = (np.asarray(mask.convert("L")) > 12).sum(axis=1)
    rows = np.nonzero(hits >= max(8, pane.width // 200))[0]
    return int(rows[-1]) if len(rows) else im.height


def trim_dead_space(im: Image.Image) -> Image.Image:
    bottom = min(im.height, _content_bottom(im) + PAD)
    return im.crop((0, 0, im.width, bottom)) if bottom < im.height else im


def redact_pill(im: Image.Image) -> Image.Image:
    """Blur the account pill so no username ships in the docs.

    Skipped for cropped panels: they have no title bar, and the blur would
    land on whatever happens to sit in their top-right corner.
    """
    if im.width < FULL_WINDOW_MIN:
        return im
    box = (max(0, im.width - PILL_W), 0, im.width, min(PILL_H, im.height))
    im.paste(im.crop(box).filter(ImageFilter.GaussianBlur(9)), box)
    return im


def main(paths) -> int:
    if not paths:
        print(__doc__)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for raw in paths:
        src = Path(raw)
        if not src.exists():
            print(f"  skip   {src} (not found)")
            continue
        im = Image.open(src).convert("RGB")
        before = im.size
        im = redact_pill(trim_dead_space(im))
        if im.width > MAX_WIDTH:
            im = im.resize((MAX_WIDTH, round(im.height * MAX_WIDTH / im.width)),
                           Image.LANCZOS)
        dst = OUT / f"{src.stem.lower().replace(' ', '-')}.png"
        im.save(dst, optimize=True)
        print(f"  wrote  {dst.relative_to(ROOT)}  {before[0]}x{before[1]}"
              f" -> {im.width}x{im.height}  ({dst.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
