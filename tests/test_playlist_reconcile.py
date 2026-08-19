"""Playlist reconcile + stale-key self-heal (SoulSync port, MIT).

Old behavior: find-delete-recreate every push — playlist ratingKey
changed weekly, client pins and custom art died, and a Plex re-key
(metadata refresh/optimize) silently dropped items whose cached
plex_rating_key had gone stale. Now: reconcile edits the existing
playlist in place (delta add/remove), and stale keys are probed and
re-resolved by title with the healed key written back to the cached
rec row (SoulSync rescue pattern).

    python tests/test_playlist_reconcile.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


import src.services.plex_playlists as pp

# ── reconcile delta math (stub the HTTP layer) ───────────────────────────────

calls = {"deleted": [], "added_uri": None}


async def _fake_items(token, key):
    return [{"item_id": "101", "rating_key": "a"},
            {"item_id": "102", "rating_key": "b"},
            {"item_id": "103", "rating_key": "b"},   # dupe row
            {"item_id": "104", "rating_key": "c"}]


class _FakeResp:
    status_code = 200


class _FakeClient:
    def __init__(self, *a, **k): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def delete(self, url, headers=None):
        calls["deleted"].append(url.rsplit("/", 1)[1])
        return _FakeResp()

    async def put(self, url, headers=None, params=None):
        calls["added_uri"] = (params or {}).get("uri", "")
        return _FakeResp()


async def _fake_mid(force=False):
    return "MID"

pp.playlist_items = _fake_items
pp.get_machine_identifier = _fake_mid
pp.httpx.AsyncClient = _FakeClient

ok = asyncio.run(pp.reconcile_playlist("tok", "999", ["b", "d", "e"]))
check("reconcile succeeds via delta", ok is True)
check("removes what fell out of the recs (incl. dupe rows) by playlistItemID",
      set(calls["deleted"]) == {"101", "104"})
check("adds only the NEW keys in wanted order",
      calls["added_uri"].endswith("/library/metadata/d,e"))

# ── wiring ───────────────────────────────────────────────────────────────────

src = (Path(__file__).resolve().parents[1] / "src/services/plex_playlists.py").read_text(encoding="utf-8")
check("video push is reconcile-first with loud one-time recreate fallback",
      "Reconcile-first (SoulSync #792)" in src
      and "recreating this once" in src)
check("stale keys probed and healed back onto the cached rec row",
      "key_exists(" in src and "_heal_rec_key(" in src
      and "resolve_video_key(" in src)
check("extra same-title copies from the old era are removed",
      "for pl in existing[1:]:" in src)
check("music push documents WHY it stays delete+recreate (album→track expansion)",
      "DELIBERATELY still" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
