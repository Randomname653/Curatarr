"""Cross-member ownership: no member acts on another member's row.

Every member-reachable route that takes the id of a per-user row filters by
the caller — but that was pinned by nothing: a refactor dropping
``ProactiveMessage.user_id == user_id`` stayed green. One table, run against
the app's real (temp, migrated) database through the real handlers:

  * another member is refused (and learns nothing: the same answer as a
    missing id),
  * the owner succeeds,
  * an id prefix never matches a longer id (user 1 vs user 12).

A static check keeps the table complete: every route with a path parameter
that a plain member can reach must be listed here as owned or as not-owned.

    python tests/test_member_ownership.py
"""
from _harness import ROOT, app_db, check  # noqa: I001 - first: temp stores

import asyncio
import importlib
import pkgutil
import re
from types import SimpleNamespace

from fastapi import HTTPException

conn = app_db()
from src.database.models import ChatStarter, MusicWish, ProactiveMessage, User  # noqa: E402
import src.routers as routers_pkg  # noqa: E402
import src.routers.chat as chat  # noqa: E402
import src.routers.library as library  # noqa: E402
import src.routers.messages as messages  # noqa: E402
import src.routers.wrapup as wrapup  # noqa: E402
from src.services.task_monitor import task_monitor  # noqa: E402

with conn.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="2", plex_username="member"),
                User(id=3, plex_user_id="3", plex_username="other"),
                User(id=12, plex_user_id="12", plex_username="twelve")])


def who(uid):
    return SimpleNamespace(id=uid, is_admin=uid == 1)


def refused(fn):
    """True when the handler answered 404 (or a falsy ok) — never a 403 that
    would confirm the id exists."""
    try:
        out = fn()
    except HTTPException as e:
        return e.status_code == 404
    return isinstance(out, dict) and out.get("ok") is False


def done(fn):
    try:
        out = fn()
    except HTTPException:
        return False
    return not isinstance(out, dict) or out.get("ok", True) is not False


# ── seeds: one row owned by ``owner``, its id back ───────────────────────────
def _add(row):
    with conn.get_db_session() as db:
        db.add(row)
        db.flush()
        return row.id


def seed_message(owner):
    return _add(ProactiveMessage(user_id=owner, trigger_type="test", message="hi"))


def seed_starter(owner):
    return _add(ChatStarter(user_id=owner, text="Ask me about Heat"))


def seed_wish(owner):
    return _add(MusicWish(user_id=owner, title="Some Album", source="test"))


def seed_wrapup_task(owner):
    tid = f"wrapup-{owner}-all-30d"
    task_monitor.create("Wrap-up: test", "wrapup", total=6, task_id=tid)
    return tid


def read(uid, mid):
    return lambda: asyncio.run(messages.read_message(mid, who(uid)))


def starter(uid, sid):
    return lambda: chat.starter_used(sid, who(uid))


def wish(uid, wid):
    return lambda: library.remove_wish(wid, who(uid))


def wstatus(uid, tid):
    return lambda: wrapup.status(tid, who(uid))


# (label, seed, act, an id that does not exist, the household owner may act too)
OWNED = [
    ("proactive message read", seed_message, read, 999_999, False),
    ("chat starter used", seed_starter, starter, 999_999, False),
    ("music wish removal", seed_wish, wish, 999_999, True),       # owner decision 2026-10-20
    ("wrap-up generation status", seed_wrapup_task, wstatus, "wrapup-3-all-30d", False),
]

for label, seed, act, missing, owner_may in OWNED:
    rid = seed(2)
    check(f"{label}: another member is refused", refused(act(3, rid)))
    check(f"{label}: the owner of the row succeeds", done(act(2, rid)))
    check(f"{label}: a missing id answers like a foreign one", refused(act(3, missing)))
    if owner_may:
        check(f"{label}: the household owner may act on it (by design)", done(act(1, seed(2))))
    else:
        check(f"{label}: even the household owner is refused", refused(act(1, seed(2))))
        check(f"{label}: user 1 never matches user 12's row", refused(act(1, seed(12))))

check("wrap-up status: user 1 cannot read user 12's task by prefix",
      refused(wstatus(1, seed_wrapup_task(12))))

# ── completeness: every member-reachable {param} route is classified ────────
# Routes whose path parameter names a per-user row are OWNED above; the rest
# take a parameter that is not a per-user id. A new member route with a path
# parameter fails here until someone decides which it is.
NOT_OWNED = {
    "auth GET /plex/poll/{pin_id}": "PIN nonce checked in the handler, pre-login",
    "library GET /items/{service}": "an arr service name",
    "library GET /search/{service}": "an arr service name",
}
OWNED_ROUTES = {
    "chat POST /starters/{starter_id}/used",
    "library DELETE /wish/{wish_id}",
    "messages POST /{message_id}/read",
    "wrapup GET /status/{task_id}",
}

main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
admin_routers = set(re.findall(r"include_router\((\w+)\.router[^)]*?dependencies=_ADMIN_ONLY",
                               main_src, re.S))
check("main.py: the admin-only routers are recognised", {"enrichment", "process_monitor",
                                                         "model_updates"} <= admin_routers,
      sorted(admin_routers))

from fastapi.routing import APIRoute  # noqa: E402


def _deps(d, acc):
    for sub in d.dependencies:
        acc.add(getattr(sub.call, "__name__", str(sub.call)))
        _deps(sub, acc)
    return acc


member_param_routes = set()
for m in pkgutil.iter_modules(routers_pkg.__path__):
    if m.name in admin_routers:
        continue
    r = getattr(importlib.import_module(f"src.routers.{m.name}"), "router", None)
    for rt in getattr(r, "routes", []):
        if isinstance(rt, APIRoute) and "{" in rt.path and "require_admin" not in _deps(rt.dependant, set()):
            member_param_routes.add(f"{m.name} {sorted(rt.methods)[0]} {rt.path}")

unclassified = sorted(member_param_routes - OWNED_ROUTES - set(NOT_OWNED))
check(f"every member route with a path parameter is classified {unclassified or ''}",
      not unclassified)
stale = sorted((OWNED_ROUTES | set(NOT_OWNED)) - member_param_routes)
check(f"the classification lists no route that is gone {stale or ''}", not stale)

check.done()
