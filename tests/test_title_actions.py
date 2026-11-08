"""One-tap actions under a chat reply and on recommendation cards (stage 2 of
docs/UX_RETENTION_DESIGN.md, 2.1 / 2.2, C4-C6): the reply's **bold** library
titles get Open on the server, + Watchlist and Keep; an owned recommendation
opens on the member's server. Deterministic: exact library names, the
ledger's server key, no model.

Every title here is invented.

    python tests/test_title_actions.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime
from types import SimpleNamespace

conn = app_db()

from src.database.models import CachedRecommendation, LibraryItem, ProtectedMedia, User  # noqa: E402
from src.services import title_actions as ta  # noqa: E402

# ── finding the titles ──────────────────────────────────────────────────────

reply = ("**The Lantern Room** fits best, the same patient mystery. Or **Gull Point**, lighter. "
         "**The Lantern Room** again, and **Signal** on its own, and **Not In The Library**.")
check("the bold spans, in order, once each",
      ta.bold_titles(reply) == ["The Lantern Room", "Gull Point", "Signal", "Not In The Library"])
INDEX = [("the lantern room", "The Lantern Room", "movie"), ("gull point", "Gull Point", "show"),
         ("north signal", "North Signal", "movie"), ("the hollow pines", "The Hollow Pines", "music")]
check("a span must BE a library title - 'Signal' is not 'North Signal', unknown names get nothing",
      ta.match(ta.bold_titles(reply), INDEX) == [("The Lantern Room", "movie"), ("Gull Point", "show")])
check("at most four rows", len(ta.match([f"t{i}" for i in range(9)],
                                        [(f"t{i}", f"T{i}", "movie") for i in range(9)])) == ta.MAX_TITLES)
check("no bold, no rows", ta.for_reply(1, "Plain words about The Lantern Room.", lambda: INDEX) == [])

with conn.get_db_session() as db:
    u = User(plex_user_id="ta-1", plex_username="Member")
    other = User(plex_user_id="ta-2", plex_username="Owner", is_admin=True)
    db.add_all([u, other])
    db.commit()
    UID, OWNER = u.id, other.id
    db.add(LibraryItem(plex_rating_key="4711", media_type="movie", title="The Lantern Room"))
    db.add(LibraryItem(plex_rating_key="999", media_type="movie", title="Gull Point",
                       gone_at=datetime(2026, 1, 1)))                     # left the library
    db.add(ProtectedMedia(user_id=UID, identifier="Gull Point", title="Gull Point", category="show",
                          source="manual"))
    db.commit()

got = ta.for_reply(UID, reply, lambda: INDEX)
check("each match carries the ledger's server key and whether the member keeps it already",
      got == [{"title": "The Lantern Room", "category": "movie", "key": "4711", "protected": False},
              {"title": "Gull Point", "category": "show", "key": None, "protected": True}], got)
check("music has no ledger key, a title the ledger lost has none either",
      ta.for_reply(UID, "**The Hollow Pines**", lambda: INDEX)[0]["key"] is None)

# ── Keep, in one tap ────────────────────────────────────────────────────────

from src.routers import home  # noqa: E402
from src.services import pillars  # noqa: E402

first = asyncio.run(home.keep(home.KeepBody(title="The Lantern Room", category="movie"),
                              user=SimpleNamespace(id=UID)))
again = asyncio.run(home.keep(home.KeepBody(title="The Lantern Room", category="movie"),
                              user=SimpleNamespace(id=UID)))
with conn.get_db_session() as db:
    rows = [(r.source, r.category) for r in db.query(ProtectedMedia).filter(
        ProtectedMedia.user_id == UID, ProtectedMedia.identifier == "The Lantern Room")]
    judge_sees = pillars._other_users_keeps(db, OWNER, {"title": "The Lantern Room"}, "movie")
check("Keep writes one protection, as the chat's keep-detector does, and is idempotent",
      first == {"ok": True, "already": False} and again == {"ok": True, "already": True}
      and rows == [("manual", "movie")], rows)
check("the deletion judge reads a member's Keep as the household's", judge_sees == ["Member"], judge_sees)

# ── links: the chat frame and the recommendation cards ──────────────────────

import src.services.media_server as media_server  # noqa: E402
import src.services.plex_playlists as plex_playlists  # noqa: E402
from src.services import server_links  # noqa: E402


async def _mid(force=False):
    return "MACHINE"

_saved = (plex_playlists.get_machine_identifier, media_server.active_kind)
plex_playlists.get_machine_identifier = _mid
media_server.active_kind = lambda settings=None: "plex"
try:
    items = [{"key": "4711"}, {"key": None}, {"key": "radarr:12"}]
    asyncio.run(server_links.attach_links(items))
    with conn.get_db_session() as db:
        db.add_all([CachedRecommendation(user_id=UID, category="movie", lane="library", title="The Lantern Room",
                                         confidence=0.9, plex_rating_key="4711"),
                    CachedRecommendation(user_id=UID, category="movie", lane="discovery", title="Shop Window",
                                         confidence=0.8, plex_rating_key="5150")])
        db.commit()
    from src.routers import recommendations as recs_router
    out = asyncio.run(recs_router._get_recommendations_impl("movie", 8, False, "cache", None,
                                                            SimpleNamespace(id=UID, is_admin=False)))
finally:
    plex_playlists.get_machine_identifier, media_server.active_kind = _saved
check("a Plex key links through app.plex.tv; no key, or an arr key, no link",
      items[0]["link"] == server_links.PLEX_WEB.format(mid="MACHINE", key="4711")
      and "link" not in items[1] and "link" not in items[2], items)
by = {r["title"]: r for r in out["recommendations"]}
check("an owned recommendation opens on the server; a discovery pick has no link; no key leaks",
      by["The Lantern Room"]["link"].endswith("key=%2Flibrary%2Fmetadata%2F4711")
      and "link" not in by["Shop Window"] and all("_key" not in r for r in out["recommendations"]), by)

check.done()
