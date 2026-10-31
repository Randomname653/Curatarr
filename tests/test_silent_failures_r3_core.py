"""Silent-failure sweep, round 3 (docs/SILENT_FAILURE_SWEEP_2026_10.md): the
walker queue, setup addresses, and route handlers off the event loop.

  * a walker row that keeps failing goes to the back of the queue: the
    reception walker took the first ``limit`` rows in a fixed order, so a
    systematic failure filled every pass with the same titles
  * setup refuses a host NAME that resolves to a link-local / metadata
    address before saving it (the model pulls and builds that follow would
    otherwise be pointed at it)
  * a memoized endpoint with a blocking body runs that body on a worker
    thread; every route handler that awaits nothing is a plain ``def``
    (FastAPI's threadpool), so its synchronous SQLAlchemy work no longer
    freezes the loop

    python tests/test_silent_failures_r3_core.py
"""
import ast
import asyncio
import inspect
import json
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi import HTTPException  # noqa: E402

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.services.app_state as app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── walker attempt stamps ───────────────────────────────────────────────────

def test_walker_attempts_send_failures_back():
    from src.services import enrichment_state as es
    from _checks import real_state  # noqa: E402
    state = real_state()          # the real app_state table, not a dict
    saved = (app_state.get_state, app_state.set_state)
    try:
        es.save_walker_attempts("w", {"a": "2026-10-15T10:00:00", "gone": "x"}, keep={"a", "b"})
        got = es.load_walker_attempts("w")
        state["w"] = "{not json"
        broken = es.load_walker_attempts("w")
    finally:
        app_state.get_state, app_state.set_state = saved
    check("attempt stamps round-trip, pruned to rows still waiting", got == {"a": "2026-10-15T10:00:00"})
    check("an unreadable stamp store is an empty one (fixed order, logged)", broken == {})

    rows = [("x", "anime", k, {}) for k in ("a", "b", "c")]
    attempts = {"a": "2026-10-15T10:00:00", "c": "2026-10-14T10:00:00"}
    order = [r[2] for r in sorted(rows, key=lambda x: attempts.get(x[2], ""))]
    check("never-tried rows first, then the least recently tried", order == ["b", "c", "a"])

    from src.services import reception
    src = inspect.getsource(reception.run_reception_backfill)
    check("the reception walker orders its queue by attempt stamps",
          "run_read(load_walker_attempts, _RECEPTION_ATTEMPTS_KEY)" in src
          and "key=lambda x: attempts.get(x[2], \"\")" in src)
    check("...stamps every attempt and stores them after the pass",
          "attempts[id_key] = " in src and "run_write(save_walker_attempts," in src)

    from src.services import media_enricher as me
    sig = inspect.getsource(me.run_significance_backfill)
    check("the significance walker orders, stamps and stores the same way",
          "run_read(load_walker_attempts, _SIG_ATTEMPTS_KEY)" in sig
          and "attempts[_row_key(row)] = " in sig and "run_write(save_walker_attempts," in sig)


# ── setup refuses metadata-resolving names ──────────────────────────────────

def test_setup_refuses_resolved_metadata():
    import src.routers.setup as st

    async def fake_refusal(url):
        return "meta.example resolves to 169.254.169.254, a link-local / metadata address" \
            if "meta.example" in url else None

    real = st.resolved_refusal
    st.resolved_refusal = fake_refusal
    try:
        try:
            asyncio.run(st._refuse_resolved({"plex_url": "http://plex.lan:32400",
                                             "ollama_endpoint": "http://meta.example:11434"}))
            refused = None
        except HTTPException as e:
            refused = e
        asyncio.run(st._refuse_resolved({"plex_url": "http://plex.lan:32400"}))
        allowed = True
    finally:
        st.resolved_refusal = real
    check("a metadata-resolving endpoint is refused with 422",
          refused is not None and refused.status_code == 422
          and "ollama_endpoint" in refused.detail)
    check("a LAN name passes", allowed)
    src = inspect.getsource(st)
    for fn in ("complete_setup", "reconfigure", "warmup"):
        body = inspect.getsource(getattr(st, fn))
        check(f"/{fn} checks names before saving or connecting", "await _refuse_resolved(" in body)
    check("the checked fields cover every service URL",
          all(f in src for f in ('"plex_url"', '"ollama_endpoint"', '"radarr_url"',
                                 '"sonarr_url"', '"lidarr_url"', '"soulsync_url"')))


# ── handlers off the loop ───────────────────────────────────────────────────

def test_memo_runs_sync_bodies_off_the_loop():
    from src.services.ttl_memo import ttl_response
    seen = {}

    @ttl_response(60, shared=True)
    def blocking(user=None):
        seen["thread"] = threading.get_ident()
        return {"n": 1}

    async def main():
        seen["loop"] = threading.get_ident()
        a = await blocking(user=None)
        b = await blocking(user=None)
        return a, b

    a, b = asyncio.run(main())
    check("a memoized plain def runs on a worker thread", seen["thread"] != seen["loop"])
    check("...and is still memoized", a == b == {"n": 1})


# Route handlers that may stay async without awaiting anything: they hand a
# coroutine to the running loop (create_task / track_task), which a
# threadpool thread does not have.
_LOOP_BOUND = {"start_embedding_migration", "compare", "evaluate", "bulk_approve_deletions",
               "start_wrapup"}


def test_no_await_free_async_route_handlers():
    offenders = []
    for p in sorted((ROOT / "src" / "routers").glob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, ast.AsyncFunctionDef) or node.name in _LOOP_BOUND:
                continue
            routed = any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                         and d.func.attr in ("get", "post", "put", "delete", "patch")
                         for d in node.decorator_list)
            if routed and not any(isinstance(n, (ast.Await, ast.AsyncFor, ast.AsyncWith))
                                  for n in ast.walk(node)):
                offenders.append(f"{p.name}:{node.lineno} {node.name}")
    check(f"every await-free route handler is a plain def ({offenders})", not offenders)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)
