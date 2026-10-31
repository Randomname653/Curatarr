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


def _off_loop(fn, *args):
    """Run a suite's own bookkeeping read/write off the event-loop thread.
    A fake coroutine that snapshots ``state`` must not show up in the loop
    guard as the app's statement (the guard attributes it to the src
    coroutine that called the fake); the app's own app_state calls never
    come through here, so the guard still sees every one of those."""
    import asyncio
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return fn(*args)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(fn, *args).result()


class RealState(dict):
    """The real app_state table, read and written like the dict the suites
    used to fake it with: ``state["k"]``, ``state.get("k")``, ``k in
    state``, ``state.clear()``. Every access goes through app_state's own
    get_state / set_state, so the string-only values, the upsert and the
    session factory the app uses are what the suite exercises — a dict
    stored whatever it was given and could not tell.

    Subclasses dict only so ``isinstance(state, dict)`` and ``**state`` in
    old suites keep working; the dict storage itself is never used."""

    def __init__(self):
        super().__init__()
        from src.database.models import AppState

        def _create():
            with self._session() as s:
                AppState.__table__.create(s.get_bind(), checkfirst=True)
        _off_loop(_create)

    @staticmethod
    def _session():
        """The session factory app_state itself uses — a suite may have
        pointed it at its own engine, and this view must read that one."""
        from src.services import app_state
        return app_state.get_db_session()

    @staticmethod
    def _rows():
        def _read():
            from src.database.models import AppState
            with RealState._session() as s:
                return dict(s.query(AppState.key, AppState.value).all())
        return _off_loop(_read)

    def __getitem__(self, key):
        from src.services.app_state import get_state
        v = _off_loop(get_state, key)
        if v is None:
            raise KeyError(key)
        return v

    def get(self, key, default=None):
        from src.services.app_state import get_state
        v = _off_loop(get_state, key)
        return default if v is None else v

    def __setitem__(self, key, value):
        from src.services.app_state import set_state
        _off_loop(set_state, key, value)

    def __delitem__(self, key):
        def _delete():
            from src.database.models import AppState
            with self._session() as s:
                n = s.query(AppState).filter(AppState.key == key).delete()
                s.commit()
                return n
        if not _off_loop(_delete):
            raise KeyError(key)

    def pop(self, key, *default):
        try:
            v = self[key]
        except KeyError:
            if default:
                return default[0]
            raise
        del self[key]
        return v

    def __contains__(self, key):
        return self.get(key) is not None

    def __iter__(self):
        return iter(self._rows())

    def __len__(self):
        return len(self._rows())

    def keys(self):
        return self._rows().keys()

    def items(self):
        return self._rows().items()

    def values(self):
        return self._rows().values()

    def clear(self):
        def _clear():
            from src.database.models import AppState
            with self._session() as s:
                s.query(AppState).delete()
                s.commit()
        _off_loop(_clear)

    def update(self, other=(), **kw):
        for k, v in dict(other, **kw).items():
            self[k] = v

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def __repr__(self):
        return f"RealState({self._rows()!r})"


def real_state() -> RealState:
    """The real app_state table as a dict-like view (see RealState)."""
    return RealState()
