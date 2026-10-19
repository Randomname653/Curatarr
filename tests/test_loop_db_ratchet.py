"""Synchronous database calls inside coroutines may only go down.

A coroutine that opens ``get_db_session()``, calls ``get_state`` /
``set_state`` or runs ``db.query(...)`` directly runs SQLite on the
event-loop thread: every request waits for it, and up to busy_timeout when
another writer holds the lock. This counts such calls per file in ``src/``
(not those inside a nested ``def`` / ``lambda``, and not those handed to
``run_read`` / ``run_write`` / ``asyncio.to_thread``) against
``tests/loop_db_baseline.json``. A file may go down, never up; a file not in
the baseline must have none. When a file goes down, lower its number in the
baseline in the same change (the test says so).

The runtime side is src/database/loop_guard.py (GET /api/system/loop-guard).

    python tests/test_loop_db_ratchet.py            # check
    python tests/test_loop_db_ratchet.py --update   # rewrite the baseline (only ever lower)
"""
import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
BASELINE = Path(__file__).with_name("loop_db_baseline.json")

# Functions that run SQLite synchronously when called.
SYNC_DB = {
    "get_db_session", "write_session", "SessionLocal",
    "get_state", "set_state", "set_states_if_changed", "force_set_state",
    "acquire_state_lock", "release_state_lock", "get_datetime", "set_datetime",
    "MetadataCache",
}
# ... and methods on a session held in one of these names.
SESSION_NAMES = {"db", "_db"}
SESSION_METHODS = {
    "query", "execute", "commit", "add", "add_all", "flush", "delete", "get",
    "scalar", "scalars", "merge", "refresh", "rollback",
}
# A call nested in one of these runs on a worker, not the loop.
OFFLOAD = {"to_thread", "run_read", "run_write", "run_chroma", "run_in_executor"}


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _is_sync_db(call: ast.Call) -> bool:
    f = call.func
    if _name(f) in SYNC_DB:
        return True
    return (isinstance(f, ast.Attribute) and f.attr in SESSION_METHODS
            and isinstance(f.value, ast.Name) and f.value.id in SESSION_NAMES)


def sites(path: Path) -> list:
    """[(line, coroutine, call)] for one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out = []

    def visit(node, coro, offloaded):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            off = offloaded
            if isinstance(ch, ast.Call):
                if _name(ch.func) in OFFLOAD:
                    off = True
                elif not offloaded and _is_sync_db(ch):
                    out.append((ch.lineno, coro, _name(ch.func)))
            visit(ch, coro, off)

    for fn in ast.walk(tree):
        if isinstance(fn, ast.AsyncFunctionDef):
            visit(fn, fn.name, False)
    return out


def counts() -> dict:
    out = {}
    for p in sorted(SRC.rglob("*.py")):
        n = len(sites(p))
        if n:
            out[p.relative_to(ROOT).as_posix()] = n
    return out


def main() -> int:
    now = counts()
    base = json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}
    if "--update" in sys.argv:
        # Only ever lower: a file over its number (or new) keeps the old
        # number, so --update cannot paper over a regression.
        merged = now if not base else {
            f: min(n, base[f]) for f, n in now.items() if f in base}
        BASELINE.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"baseline written: {sum(merged.values())} sites in {len(merged)} files")
        return 0
    fail = passed = 0
    for f, n in sorted(now.items()):
        allowed = base.get(f, 0)
        if n > allowed:
            fail += 1
            print(f"  FAIL  {f}: {n} sync DB calls in coroutines (baseline {allowed})")
            for line, coro, call in sites(ROOT / f):
                print(f"          {f}:{line}  {coro}() -> {call}")
        else:
            passed += 1
    for f, allowed in sorted(base.items()):
        n = now.get(f, 0)
        if n < allowed:
            fail += 1
            print(f"  FAIL  {f}: down to {n} from {allowed} - lower it in "
                  f"{BASELINE.name} (python {Path(__file__).name} --update)")
    print(f"\nsync DB calls in coroutines: {sum(now.values())} in {len(now)} files "
          f"(baseline {sum(base.values())})")
    print(f"{passed} passed, {fail} failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
