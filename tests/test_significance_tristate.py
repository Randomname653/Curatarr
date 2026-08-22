"""Significance walker: transient failures must not stamp 'checked'.

The Panic-Room catch (owner pitch round): significance_checked=True with
NO text on a David Fincher film — fetch_significance returned None for
BOTH 'nothing documented' and 'Wikipedia/summarizer failed', and
topup_significance stamped either as checked forever. 11,677 raw
entries sat in that state (7,681 music / 1,835 movie / 1,647 anime /
514 show); they were unstamped once on 2026-08-18 for re-check.

Contract now: str = significance, "" = DEFINITIVE nothing (stamp),
None = TRANSIENT (do NOT stamp — walker retries).

    python tests/test_significance_tristate.py
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


import src.services.media_enricher as me


class FakeCache:
    def __init__(self):
        self.store = {"raw:movie:4547": {"response": {"title": "Panic Room"}}}
        self.writes = []
        self.dropped = []

    def get_cache(self, key):
        return self.store.get(key)

    def set_cache(self, key, value, days=None):
        self.writes.append((key, dict(value)))

    # Walkers write only the fields they own, so a concurrent walker's work is
    # not discarded. The double has to model that or the code under test takes
    # a path production never takes.
    def patch_cache(self, key, updates, drop=(), days=None):
        row = self.store.get(key)
        if not row:
            return False
        row["response"].update(updates)
        for field in drop:
            row["response"].pop(field, None)
        self.writes.append((key, dict(updates)))
        self.dropped.append(tuple(drop))
        return True

    def close(self):
        pass


def _run(sig_value):
    cache = FakeCache()
    real = me.fetch_significance

    async def _fake(title, media_type, year=None, also_known_as=()):
        return sig_value
    me.fetch_significance = _fake
    try:
        added = asyncio.run(me.topup_significance(
            "Panic Room", "movie", tmdb_id=4547, cache=cache))
    finally:
        me.fetch_significance = real
    return added, cache.writes, cache.dropped


added, writes, dropped = _run(None)
check("TRANSIENT (None): nothing stamped, nothing written",
      added is False and writes == [])

added, writes, dropped = _run("")
check("DEFINITIVE nothing (''): stamped checked, no significance text",
      added is False and len(writes) == 1
      and writes[0][1].get("significance_checked") is True
      and "significance" not in writes[0][1])
check("...and any text a previous version left is explicitly removed",
      dropped == [("significance",)])

added, writes, dropped = _run("Nominated for two Saturn Awards.")
check("real significance: stamped AND stored",
      added is True and writes[0][1].get("significance") ==
      "Nominated for two Saturn Awards.")

src = (Path(__file__).resolve().parents[1] / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("fetch_significance distinguishes definitive '' from transient None",
      'return ""      # definitive' in src
      and "return None    # transient" in src)
check("no-match after a SUCCESSFUL search is definitive; failed search is not",
      'return ""      # definitive — search worked, nothing matches' in src
      and "transient — no search attempt succeeded" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
