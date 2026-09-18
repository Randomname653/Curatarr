"""Regressions for the 2026-09-18 PR round: what the merged branches got
wrong, and the two findings that were worth acting on.

    python tests/test_merged_prs.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))


class _FakeORMRow:
    """Duck-types a WatchHistoryEntry: what db.query(Model) hands back."""
    _sa_instance_state = object()

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def test_the_row_converter_takes_both_shapes():
    """PR #102 rewrote _to_dicts to unpack column tuples but converted only
    one of its two callers — the series-progress path still queries whole
    ORM rows and would have raised on every run."""
    import types
    from unittest.mock import MagicMock
    for name in ("httpx", "sqlalchemy", "sqlalchemy.orm", "src.database.connection",
                 "src.database.models", "src.config"):
        sys.modules.setdefault(name, MagicMock())
    from src.services.proactive_messages import _ROW_FIELDS, _to_dicts

    assert _ROW_FIELDS == ("title", "series_title", "media_type", "viewed_at", "duration_ms",
                           "view_offset_ms", "completed", "season", "episode", "genres")

    tup = ("Ep", "Show A", "show", "2026-09-18", 1000, 900, True, 1, 3, "drama")
    from_tuple = _to_dicts([tup])[0]
    assert from_tuple["title"] == "Ep" and from_tuple["series_title"] == "Show A"
    assert from_tuple["last_viewed_at"] == from_tuple["viewed_at"] == "2026-09-18"
    assert from_tuple["rating"] is None, "not a column yet"

    orm = _FakeORMRow(title="Ep", series_title="Show A", media_type="show",
                      viewed_at="2026-09-18", duration_ms=1000, view_offset_ms=900,
                      completed=True, season=1, episode=3, genres="drama")
    from_orm = _to_dicts([orm])[0]
    assert from_orm == from_tuple, "both callers must produce the same dict"

    # A column the model may grow later is picked up from the ORM shape
    rated = _FakeORMRow(title="Ep", series_title=None, media_type="movie", viewed_at="x",
                        duration_ms=None, view_offset_ms=None, completed=False,
                        season=None, episode=None, genres=None, rating=7.5)
    assert _to_dicts([rated])[0]["rating"] == 7.5
    assert _to_dicts([]) == []


def test_a_shared_response_cache_has_to_be_asked_for():
    """Issue #100: the memo defaulted to one entry for everybody. Not a leak
    today — every global user returns library- or host-wide numbers — but a
    silent default is the wrong shape for a multi-user app."""
    import asyncio
    from src.services.ttl_memo import ttl_response

    try:
        ttl_response(10)
        raise AssertionError("a keyless, unshared memo must not be accepted")
    except ValueError as e:
        assert "shared=True" in str(e) and "key=" in str(e)

    calls = []

    @ttl_response(60, key=lambda **kw: kw["user_id"])
    async def per_user(user_id):
        calls.append(user_id)
        return f"body for {user_id}"

    async def drive():
        assert await per_user(user_id=1) == "body for 1"
        assert await per_user(user_id=1) == "body for 1"
        assert await per_user(user_id=2) == "body for 2"
    asyncio.run(drive())
    assert calls == [1, 2], "one body per user, each computed once"

    @ttl_response(60, shared=True)
    async def everyone():
        calls.append("shared")
        return "one body"

    async def drive_shared():
        assert await everyone() == "one body"
        assert await everyone() == "one body"
    asyncio.run(drive_shared())
    assert calls.count("shared") == 1


def test_every_memo_call_site_says_which_kind_it_is():
    import re
    for rel in ("src/routers/enrichment.py", "src/routers/history.py",
                "src/routers/libraries.py", "src/routers/process_monitor.py"):
        for m in re.finditer(r"@ttl_response\(([^)]*)\)", (_ROOT / rel).read_text(encoding="utf-8")):
            args = m.group(1)
            assert "key=" in args or "shared=True" in args, f"{rel}: @ttl_response({args})"


def test_the_sidebar_is_reachable_by_keyboard():
    """The point of PR #103, in this codebase's own idiom: the shared
    keyActivate handler, not a global document listener that would fire a
    second time on every element already carrying one."""
    html = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    items = [line for line in html.splitlines() if '<div class="sb-item' in line]
    assert len(items) >= 15, len(items)
    for line in items:
        assert 'role="button"' in line and 'tabindex="0"' in line, line[:120]
        assert 'data-on-keydown="keyActivate"' in line, line[:120]
        assert 'data-args-keydown=' in line, line[:120]
    app = (_ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
    assert "keyActivate" in app, "the handler stays registered"
    # Ctrl+K and Escape are legitimate global listeners. What PR #103 wanted
    # to add is not: a keydown that clicks every [role="button"] would fire a
    # second time on the cards and chips that already carry keyActivate.
    assert '[role="button"]' not in app, \
        "no global click-every-role-button listener — it would double-fire"
    assert "Object.assign(window" not in app, "the window block stayed gone"


def test_the_endpoint_privacy_check_parses_like_the_client_that_connects():
    """PR #104. Filed as a HIGH SSRF; it is a warning helper, not a gate —
    but validating with the same parser that performs the request is right,
    and it matches the image-proxy fix from #91."""
    wiz = (_ROOT / "src/services/setup_wizard.py").read_text(encoding="utf-8")
    assert "from urllib.parse import urlparse" not in wiz
    assert "httpx.URL(url).host" in wiz
    sys.modules.pop("src.services.setup_wizard", None)
    for name in ("httpx", "sqlalchemy", "sqlalchemy.orm", "src.database.connection",
                 "src.database.models", "src.config"):
        sys.modules.pop(name, None)
    from src.services.setup_wizard import is_private_endpoint
    for url in ("http://localhost:11434", "http://127.0.0.1:11434", "http://192.168.1.50:32400",
                "http://10.0.0.5", "http://172.16.0.9", "http://[::1]:11434",
                "http://nas.local:12279", "http://plex.lan", "http://user:pass@127.0.0.1:11434/"):
        assert is_private_endpoint(url) is True, url
    for url in ("https://api.example.com", "http://8.8.8.8", "", "not a url",
                "http://①②⑦.0.0.1/"):
        assert is_private_endpoint(url) is False, url


def test_the_spa_root_is_resolved_before_the_containment_check():
    """Code-scanning #55/#56. The traversal guard was already correct; an
    unresolved root could only reject a legitimate asset, never admit a
    traversal — resolving both sides removes the mismatch."""
    main = (_ROOT / "src/main.py").read_text(encoding="utf-8")
    assert "_FRONTEND_ROOT = frontend_root().resolve()" in main
    assert "candidate.relative_to(_FRONTEND_ROOT)" in main, "the guard stays"


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
