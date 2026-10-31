"""Shared bits for the stdlib suites (no pytest — project convention).

    from _harness import app_db, check, wait_until   # FIRST, before any src import

* Every store the app opens points at a private temp dir: a suite never
  touches the developer's data/ (live curatarr.db, chromadb, enrichment.db,
  plex_music.db) or their .env. Set through the environment, not
  ``settings.X = ...`` after the fact: src/services/lyrics.py derives
  PLEX_MUSIC_DB_PATH from ENRICHMENT_CACHE at import time, so a later
  settings patch is too late. tests/run_all.py sets the same variables per
  suite; this module makes a directly-run suite just as hermetic.
* ``app_db()`` — the app's REAL engine on that temp file: WAL,
  foreign_keys=ON, the loop guard and every migration, not a bare
  ``create_engine("sqlite://")`` that skips all four.
* ``check`` — the PASS/FAIL runner every suite used to copy, plus
  ``check.table`` (the stdlib stand-in for ``@pytest.mark.parametrize``).
* ``wait_until`` — poll a condition instead of sleeping a guessed 50 ms.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

if "src.config" in sys.modules:
    raise RuntimeError("import tests/_harness.py before any src module: "
                       "settings already read the live paths")

if os.environ.get("CURATARR_SUITE_TMP"):
    # Under tests/run_all.py: the runner already pointed every store at this
    # suite's own temp dir.
    TMP = Path(os.environ["CURATARR_SUITE_TMP"])
else:
    TMP = Path(tempfile.mkdtemp(prefix="curatarr-test-"))
    (TMP / "cache").mkdir()
    os.environ["CURATARR_SUITE_TMP"] = str(TMP)
    os.environ["DATABASE_URL"] = f"sqlite:///{(TMP / 'curatarr.db').as_posix()}"
    os.environ["CHROMADB_PATH"] = str(TMP / "chromadb")
    os.environ["ENRICHMENT_CACHE"] = str(TMP / "cache" / "enrichment.db")
    os.environ["CURATARR_ENV_FILE"] = str(TMP / ".env")
os.environ.setdefault("JWT_SECRET", "test-only-secret-never-shipped-32-bytes-min-per-rfc7518")


class _Checks:
    """``check(name, cond, detail="")``; ``check.done()`` prints the summary
    run_all reads and exits non-zero on any failure."""

    def __init__(self):
        self.passed = self.failed = 0

    def __call__(self, name, cond, detail=""):
        ok = bool(cond)
        if ok:
            self.passed += 1
            print(f"  PASS  {name}")
        else:
            self.failed += 1
            print(f"  FAIL  {name}" + (f" — {detail}" if detail else ""))
        return ok

    def table(self, name, rows, fn):
        """rows = [(label, *args, expected), ...]: one check per row."""
        for label, *args, want in rows:
            try:
                got = fn(*args)
            except Exception as e:  # noqa: BLE001 - reported as the row's failure
                got = f"raised {type(e).__name__}: {e}"
            self(f"{name}: {label}", got == want, f"got {got!r}, want {want!r}")

    def done(self):
        print(f"\n{self.passed} passed, {self.failed} failed")
        sys.exit(1 if self.failed else 0)


check = _Checks()


def app_db():
    """Create and migrate the app's own database on the temp file and hand
    back ``src.database.connection`` (get_db_session, engine, ...)."""
    from src.database import connection
    connection.init_db()
    return connection


async def wait_until(pred, timeout=5.0, what="condition"):
    """Return as soon as ``pred()`` is true; fail loudly after ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.002)
