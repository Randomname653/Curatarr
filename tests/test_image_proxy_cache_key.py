"""The image proxy keys its cache by the parsed URL, the one it fetches.

Jules "Sentinel" opened four PRs (#141-#145, 2026-10-06..09) calling the
raw-string cache key an SSRF parser differential. It was not one: the host
check and the fetch both used httpx.URL(src). Keying the cache by the same
parsed URL lets two spellings of one URL share one cache file, and the
pattern no longer reads like a differential.

    python tests/test_image_proxy_cache_key.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.routers.image_proxy as ip  # noqa: E402

CANON = "https://image.tmdb.org/t/p/w92/a.jpg"


def test_spellings_of_one_url_share_one_cache_file():
    seen = []
    hit = pathlib.Path(tempfile.mkdtemp()) / "a.jpg"
    hit.write_bytes(b"\xff\xd8\xff\xe0")

    def find(url):
        seen.append(url)
        return hit
    real = ip._find_existing
    ip._find_existing = find
    try:
        for src in ("https://IMAGE.TMDB.ORG/t/p/w92/a.jpg", "https://image.tmdb.org:443/t/p/w92/a.jpg",
                    "HTTPS://image.tmdb.org/t/p/w92/a.jpg", CANON):
            asyncio.run(ip.proxy_image(src=src))
    finally:
        ip._find_existing = real
    assert seen == [CANON] * 4, seen


def test_the_fetch_and_the_cache_use_the_same_url():
    src = (_ROOT / "src" / "routers" / "image_proxy.py").read_text(encoding="utf-8")
    body = src[src.index("async def proxy_image"):]
    assert body.index("src = str(parsed)") < body.index("cached = _find_existing(src)")
    assert "next_url = str(parsed)" in body


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
    sys.exit(1 if fails else 0)
