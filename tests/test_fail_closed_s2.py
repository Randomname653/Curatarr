"""Guards that let an action through when they could not decide, closed.

  * the game flag: four copies of _gaming() read an unreadable flag as
    "no game" (scheduler's copy failed closed); all now go through
    app_state.game_active, which counts it as a game
  * the DNS check on service URLs: a resolver that timed out or errored
    answered "no refusal", so a slow resolver walked past it; now a refusal
    (a name that does not resolve still fails the connect by itself)
  * the enrichment status memo was keyed on ``quick`` alone, so a member
    saw another user's "watched titles" numbers for 10 s

Each check fails on the code before this change.

    python tests/test_fail_closed_s2.py
"""
import asyncio
import os
import socket
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry  # noqa: E402
from src.services import app_state  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── 1. game flag ────────────────────────────────────────────────────────────
print("game flag")
_real_get = app_state.get_state


def _broken(key):
    raise RuntimeError("database is locked")


import src.services.data_custodian as dc  # noqa: E402
import src.services.discogs_offline as dg  # noqa: E402
import src.services.scheduler as sched  # noqa: E402

app_state.get_state = _broken
try:
    got = {"app_state": app_state.game_active(), "data_custodian": dc._gaming(),
           "discogs_offline": dg._gaming(), "scheduler": sched._gaming()}
finally:
    app_state.get_state = _real_get
check(f"an unreadable flag counts as a game everywhere ({got})", all(got.values()))
app_state.set_state("game_active", "0")
check("a readable flag is read as it is",
      app_state.game_active() is False and dc._gaming() is False)
for f in ("reception.py", "media_enricher.py"):
    src = (ROOT / "src" / "services" / f).read_text(encoding="utf-8")
    body = src[src.index("    def _gaming() -> bool:"):][:300]
    check(f"{f}'s nested _gaming goes through game_active",
          "game_active()" in body and "return False" not in body)

# ── 2. DNS check ────────────────────────────────────────────────────────────
print("DNS check on service URLs")
from src.services import endpoint_policy as ep  # noqa: E402


def _with_resolver(fake):
    async def run():
        loop = asyncio.get_running_loop()
        loop.getaddrinfo = fake
        return await ep.resolved_refusal("http://nas.example:8989")
    return asyncio.run(run())


async def _slow(host, port):
    await asyncio.sleep(10)


async def _broken_resolver(host, port):
    raise OSError("resolver exploded")


async def _no_such_name(host, port):
    raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")


async def _private(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0))]


async def _metadata(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 0))]


ep._RESOLVE_TIMEOUT_S = 0.05
check("a resolver that times out is a refusal (was: allowed)",
      "in time" in (_with_resolver(_slow) or ""))
check("a resolver that errors is a refusal (was: allowed)",
      "could not be resolved" in (_with_resolver(_broken_resolver) or ""))
check("a name that does not resolve is left to fail the connect",
      _with_resolver(_no_such_name) is None)
check("a LAN address still passes", _with_resolver(_private) is None)
check("a metadata address is still refused", "169.254.169.254" in (_with_resolver(_metadata) or ""))

# ── 3. enrichment status memo ───────────────────────────────────────────────
print("enrichment status memo")
import src.routers.enrichment as en  # noqa: E402

with conn_mod.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="u1", plex_username="a"),
                User(id=2, plex_user_id="u2", plex_username="b")])
    db.flush()
    for i in range(3):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="u1", plex_item_id=f"m{i}",
                                 title=f"Film {i}", media_type="movie",
                                 viewed_at=datetime(2026, 10, 1)))


async def _both():
    a = await en.enrichment_status(user=SimpleNamespace(id=1, is_admin=True), quick=True)
    b = await en.enrichment_status(user=SimpleNamespace(id=2, is_admin=False), quick=True)
    return a, b


a, b = asyncio.run(_both())
check("user 1 sees their own watched titles",
      a["categories"]["movie"]["total_unique"] == 3)
check("user 2, asking within the memo window, does not see user 1's numbers",
      b["categories"]["movie"]["total_unique"] == 0)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
