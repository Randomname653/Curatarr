"""Synchronous database calls inside coroutines may only go down.

A coroutine that opens ``get_db_session()``, calls ``get_state`` /
``set_state``, runs ``db.query(...)`` or calls the vector store directly
runs SQLite on the event-loop thread: every request waits for it, and up to busy_timeout when
another writer holds the lock. So does a coroutine that calls a SYNC HELPER
doing any of that: a module-level function of src/ (same module, or imported
from another), or a nested ``def`` it then calls itself instead of handing
to a worker. This counts such calls per file in ``src/`` (not those handed
to ``run_read`` / ``run_write`` / ``run_chroma`` / ``asyncio.to_thread``)
against ``tests/loop_db_baseline.json``. A file may go down, never up; a file not in
the baseline must have none. When a file goes down, lower its number in the
baseline in the same change (the test says so).

The runtime side is src/database/loop_guard.py (GET /api/system/loop-guard).
src/ also opens its other SQLite files only through
``loop_guard.sqlite_connect``, so the guard sees those statements as well.

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
    "MetadataCache", "sqlite_connect",
}
# ... and methods on a session held in one of these names.
SESSION_NAMES = {"db", "_db"}
SESSION_METHODS = {
    "query", "execute", "commit", "add", "add_all", "flush", "delete", "get",
    "scalar", "scalars", "merge", "refresh", "rollback",
}
# ... and the vector store: a wrapper method on a handle held in one of these
# names, or on get_chroma_db() / ChromaDBWrapper(...) directly. Each one is a
# Chroma (SQLite + HNSW) round trip.
CHROMA_NAMES = {"chroma", "chroma_db", "_chroma"}
CHROMA_FACTORIES = {"get_chroma_db", "ChromaDBWrapper"}
CHROMA_METHODS = {
    "facets_add", "facets_query", "facets_delete_by_parent", "facets_count",
    "add_documents", "upsert_documents", "query", "get_by_id", "get_by_ids",
    "delete_by_id", "update_metadata", "embeddings_for_domain", "get_count",
    "count_by_id_prefix", "all_ids", "get_all",
}
# Sync functions that reach the database but never do so on the event loop,
# so calls to them are not counted and do not make their callers count:
LOOP_SAFE = {
    # on a loop thread they answer from the last verdict (process_monitor._on_loop)
    ("src.services.process_monitor", "game_process_running"),
    ("src.services.process_monitor", "gpu_pressure"),
    # cached in-process; the lifespan fills it before serving (the Chroma
    # prewarm reads it), re-read once after a profile flip
    ("src.services.embed_service", "get_profile"),
}
# Coroutines that run while no request can be waiting: the lifespan's
# startup (before its yield) and teardown. loop_guard counts them as
# "lifecycle", not as sites.
LIFECYCLE = {("src.main", "lifespan")}
# A call nested in one of these runs on a worker, not the loop.
OFFLOAD = {"to_thread", "run_read", "run_write", "run_chroma", "run_in_executor"}


def _name(func) -> str:
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _chroma_handle(v) -> bool:
    return ((isinstance(v, ast.Name) and v.id in CHROMA_NAMES)
            or (isinstance(v, ast.Call) and _name(v.func) in CHROMA_FACTORIES))


def _is_chroma(call: ast.Call) -> bool:
    f = call.func
    if not isinstance(f, ast.Attribute):
        return False
    if f.attr in CHROMA_METHODS and _chroma_handle(f.value):
        return True
    # the raw collection under a wrapper: chroma.collection.get(...)
    return (isinstance(f.value, ast.Attribute) and f.value.attr == "collection"
            and _chroma_handle(f.value.value))


def _is_sync_db(call: ast.Call) -> bool:
    f = call.func
    if _name(f) in SYNC_DB or _is_chroma(call):
        return True
    return (isinstance(f, ast.Attribute) and f.attr in SESSION_METHODS
            and isinstance(f.value, ast.Name) and f.value.id in SESSION_NAMES)


def _calls_in(fn) -> list:
    """Calls in a function body that run when it runs: not inside a nested
    def/lambda, not handed to an offload."""
    out = []

    def visit(node, offloaded):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            off = offloaded
            if isinstance(ch, ast.Call):
                if _name(ch.func) in OFFLOAD:
                    off = True
                elif not offloaded:
                    out.append(ch)
            visit(ch, off)

    visit(fn, False)
    return out


def _module_name(path: Path) -> str:
    return ".".join(path.resolve().relative_to(ROOT).with_suffix("").parts)


_INDEX: dict = {}


def _db_helpers() -> dict:
    """{module: {function name}} for module-level SYNC functions of src/ that
    reach the database when called: directly, or through another such
    function of the same module (to a fixed point)."""
    if _INDEX:
        return _INDEX
    calls, imports = {}, {}
    for p in sorted(SRC.rglob("*.py")):
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        mod = _module_name(p)
        imports[mod] = _imported_helpers(tree)
        calls[mod] = {n.name: _calls_in(n) for n in tree.body if isinstance(n, ast.FunctionDef)}
        _INDEX[mod] = {name for name, cs in calls[mod].items()
                       if (mod, name) not in LOOP_SAFE and any(_is_sync_db(c) for c in cs)}

    def _reaches(mod, c) -> bool:
        if not isinstance(c.func, ast.Name):
            return False
        nm = c.func.id
        if nm in calls[mod] and nm in _INDEX[mod]:
            return True
        src = imports[mod].get(nm)
        return bool(src) and src[1] in _INDEX.get(src[0], ())

    changed = True
    while changed:              # to a fixed point, across modules
        changed = False
        for mod, fns in calls.items():
            for name, cs in fns.items():
                if (name not in _INDEX[mod] and (mod, name) not in LOOP_SAFE
                        and any(_reaches(mod, c) for c in cs)):
                    _INDEX[mod].add(name)
                    changed = True
    return _INDEX


def _imported_helpers(tree) -> dict:
    """{local name: (module, name)} for every ``from src... import`` in the file."""
    out = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("src"):
            for a in n.names:
                out[a.asname or a.name] = (n.module, a.name)
    return out


def sites(path: Path) -> list:
    """[(line, coroutine, call)] for one file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    index = _db_helpers()
    own = index.get(_module_name(path), set())
    imported = _imported_helpers(tree)
    out = []
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.AsyncFunctionDef) or (_module_name(path), fn.name) in LIFECYCLE:
            continue
        # sync defs nested in this coroutine that reach the database
        nested = {n.name for n in ast.walk(fn)
                  if isinstance(n, ast.FunctionDef) and any(_is_sync_db(c) for c in _calls_in(n))}
        for c in _calls_in(fn):
            if _is_sync_db(c):
                out.append((c.lineno, fn.name, _name(c.func)))
            elif isinstance(c.func, ast.Name):
                nm = c.func.id
                src = imported.get(nm)
                if nm in nested:
                    out.append((c.lineno, fn.name, f"{nm} (nested def, called on the loop)"))
                elif nm in own or (src and src[1] in index.get(src[0], ())):
                    out.append((c.lineno, fn.name, f"{nm} (sync helper)"))
    return out


def counts() -> dict:
    out = {}
    for p in sorted(SRC.rglob("*.py")):
        n = len(sites(p))
        if n:
            out[p.relative_to(ROOT).as_posix()] = n
    return out


def raw_connects() -> list:
    """src/ files that open SQLite without the guard: every other file
    (enrichment cache, plex_music.db, offline indexes, the lease) goes
    through ``loop_guard.sqlite_connect`` so its statements are seen too."""
    out = []
    for p in sorted(SRC.rglob("*.py")):
        if p.name == "loop_guard.py":
            continue
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect" and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in ("sqlite3", "dbapi2")):
                out.append(f"{p.relative_to(ROOT).as_posix()}:{node.lineno}")
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
    raw = raw_connects()
    for site in raw:
        print(f"  FAIL  {site}: sqlite3.connect - use loop_guard.sqlite_connect")
    fail += len(raw)
    passed += not raw
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
