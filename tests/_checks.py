"""The PASS/FAIL runner the stdlib suites share (no pytest — project
convention), and ``wait_until``. No side effects on import, so a suite may
import it anywhere; tests/_harness.py re-exports both and adds the store
isolation, which must come first.

    from _checks import check
    check("name", cond, "detail shown on failure")
    check.table("name", [(label, *args, expected), ...], fn)
    check.done()          # prints "N passed, M failed" and exits non-zero on a failure
"""
import asyncio
import sys


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


async def wait_until(pred, timeout=5.0, what="condition"):
    """Return as soon as ``pred()`` is true; fail loudly after ``timeout``."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not pred():
        if loop.time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.002)


def loop_guarded(coro_fn, *args, mine=(), **kwargs):
    """Run ``coro_fn(*args, **kwargs)`` with src/database/loop_guard.py in
    raise mode: a database statement on the event-loop thread raises
    LoopBlockingQuery there. Returns ``(result, error, sites)`` — ``sites``
    the guard recorded in a file whose path contains one of ``mine``."""
    import os
    from src.database import loop_guard
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    res = err = None
    try:
        res = asyncio.run(coro_fn(*args, **kwargs))
    except Exception as e:  # noqa: BLE001 - the caller's checks report it
        err = e
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)
    sites = [s["site"] for s in loop_guard.snapshot()["sites"]
             if any(m in s["site"] for m in mine)]
    return res, err, sites


def memory_engine():
    """An in-memory SQLite engine that behaves like the app's: one shared
    connection (StaticPool — without it every thread got its own empty
    database), the app's pragmas (foreign_keys=ON among them) and the loop
    guard, which a bare ``create_engine("sqlite://")`` all skipped."""
    from sqlalchemy import create_engine, event
    from sqlalchemy.pool import StaticPool
    from src.database import connection, loop_guard
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False},
                        poolclass=StaticPool)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_connection, connection_record):
        connection._apply_sqlite_pragmas(dbapi_connection)

    loop_guard.install(eng)
    return eng


def with_users(engine, *ids):
    """The household members a fixture's rows belong to. Foreign keys are
    enforced (memory_engine), so a play, proposal or message needs its user
    to exist first — as it must in the app. User 1 is the admin."""
    from sqlalchemy.orm import Session
    from src.database.models import Base, User
    Base.metadata.create_all(engine, tables=[User.__table__])
    with Session(engine) as db:
        for uid in sorted(set(ids) or {1}):
            if db.get(User, uid) is None:
                db.add(User(id=uid, plex_user_id=str(uid), plex_username=f"user{uid}",
                            is_admin=uid == 1))
        db.commit()
    return engine
