"""What a new install meets in its first hour (fresh-install test 2026-11-14).

A clone of main, a fresh venv, the wizard, the owner's real Plex: the wizard
reloaded into itself until a restart, the onboarding sat 14 minutes on a
taste profile, nothing was enriched for half an hour and then only music,
an install without Radarr/Sonarr never enriched its unwatched library, the
Activity view listed Shoko and arr work nobody had set up, and the log
carried every account name of the Plex server.

    python tests/test_fresh_install.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
from datetime import datetime
from types import SimpleNamespace

from src.config import settings
from src.routers import enrichment as en


def _it(title, cat, source="watch_history", series=None):
    return {"title": title, "series_title": series, "media_type": cat, "source": source,
            "plex_rating_key": f"{cat}:{title}"}


# ── the order a run takes, before its budget cuts the list ──────────────────
music = [_it(f"artist {i}", "music") for i in range(50)]
films = [_it(f"film {i}", "movie") for i in range(5)]
lib_films = [_it(f"unwatched {i}", "movie", source="library") for i in range(5)]
items = music + films + lib_films           # music first, as the collector hands them over
order = en._enrichment_order(items, {})
first = order[:12]
check("a big music library no longer fills the budget: films are among the first",
      sum(1 for i in first if i["media_type"] == "movie") >= 5,
      [i["title"] for i in first])
check("watched titles come before the unwatched rest of the library",
      max(order.index(i) for i in films) < min(order.index(i) for i in lib_films))
check("...and the library rest still comes, behind all watched titles",
      all(i in order for i in lib_films)
      and min(order.index(i) for i in lib_films) > max(order.index(i) for i in music))
old = datetime(2026, 5, 1)
older = datetime(2026, 1, 1)
refresh = [_it("old a", "movie"), _it("old b", "movie")]
ts = {("old a", "movie"): old, ("old b", "movie"): older}
order = en._enrichment_order(refresh + films, ts)
check("never-enriched titles before refreshes; refreshes oldest first",
      [i["title"] for i in order[-2:]] == ["old b", "old a"], [i["title"] for i in order])


# ── the library as a source where no *arr lists it ──────────────────────────
_saved = {k: getattr(settings, k) for k in ("RADARR_URL", "RADARR_API_KEY",
                                           "SONARR_URL", "SONARR_API_KEY")}
for k in _saved:
    setattr(settings, k, None)
check("Plex alone: films, series and anime come from the library",
      en._library_categories_without_arr(["music", "movie", "show", "anime"]) == ["movie", "show", "anime"])
settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.local:7878", "k"
check("with Radarr: films from Radarr, series still from the library",
      en._library_categories_without_arr(["movie", "show", "anime"]) == ["show", "anime"])
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr.local:8989", "k"
check("with both: the library is no source", en._library_categories_without_arr(["movie", "show"]) == [])
for k, v in _saved.items():
    setattr(settings, k, v)


class _FakeAdapter:
    def __init__(self):
        self.kinds = []

    async def items(self, lib, *, kinds=()):
        self.kinds.append((lib.key, kinds))
        ids = SimpleNamespace(tmdb_id=603, tvdb_id=None, imdb_id="tt0133093")
        if kinds == ("movie",):
            yield SimpleNamespace(key="/library/metadata/1", title="The Matrix", year=1999, external_ids=ids)
        else:
            yield SimpleNamespace(key="/library/metadata/2", title="Dark", year=2017, external_ids=ids)


fake = _FakeAdapter()
import src.services.media_server as ms  # noqa: E402

_real_adapter, _real_sections, _real_cats = ms.get_adapter, en._library_sections, en._library_categories_without_arr
ms.get_adapter = lambda *a, **k: fake
en._library_sections = lambda cats: [("10", "movie"), ("11", "show")]
en._library_categories_without_arr = lambda cats: ["movie", "show"]
got = asyncio.run(en._collect_library_items(["movie", "show"]))
ms.get_adapter, en._library_sections, en._library_categories_without_arr = _real_adapter, _real_sections, _real_cats
check("the library walk asks films for a film library, series for a series one",
      fake.kinds == [("10", ("movie",)), ("11", ("series",))], fake.kinds)
check("a library film is one item with its ids, marked as library",
      got[0]["title"] == "The Matrix" and got[0]["tmdb_id"] == 603 and got[0]["source"] == "library"
      and got[0]["series_title"] is None)
check("a library series is keyed by its series title",
      got[1]["series_title"] == "Dark" and got[1]["media_type"] == "show")


# ── plex.tv: the account's own servers, best address first ──────────────────
from src.services.media_server import plex_connect as pc  # noqa: E402

conns = [
    {"protocol": "https", "address": "203.0.113.5", "port": 32400, "uri": "https://203-0-113-5.abc.plex.direct:32400",
     "local": False, "relay": False},
    {"protocol": "https", "address": "192.168.0.189", "port": 32400,
     "uri": "https://192-168-0-189.abc.plex.direct:32400", "local": True, "relay": False},
    {"protocol": "https", "address": "relay", "port": 8443, "uri": "https://relay.plex.direct:8443",
     "local": False, "relay": True},
]
check("addresses: LAN http first, then the LAN plex.direct name, then remote, never a relay",
      pc.candidate_urls(conns) == ["http://192.168.0.189:32400",
                                   "https://192-168-0-189.abc.plex.direct:32400",
                                   "https://203-0-113-5.abc.plex.direct:32400"],
      pc.candidate_urls(conns))
resources = [
    {"name": "Home", "provides": "server", "owned": True, "clientIdentifier": "abc",
     "accessToken": "server-token", "productVersion": "1.42", "connections": conns},
    {"name": "A friend's", "provides": "server", "owned": False, "clientIdentifier": "def",
     "accessToken": "t2", "connections": conns},
    {"name": "Phone", "provides": "client,player", "owned": True, "clientIdentifier": "ghi"},
]
servers = pc.owned_servers(resources)
check("only servers the account OWNS are offered (the owner becomes the admin)",
      [s.name for s in servers] == ["Home"])
servers[0].url = "http://192.168.0.189:32400"
check("the browser sees the servers without their tokens",
      "server-token" not in repr(pc.public(servers)) and pc.public(servers)[0]["reachable"])
ticket = pc.hold("user-token", "curatarr-x", servers)
check("a sign-in is held under a ticket and handed back once",
      pc.held(ticket)[0] == "user-token" and pc.held("guess") is None)
pc.release(ticket)
check("...and released after the finish", pc.held(ticket) is None)


# ── the custodian lists only what this install has ──────────────────────────
from src.services import data_custodian as dc  # noqa: E402

_saved = {k: getattr(settings, k, None) for k in ("SHOKO_URL", "SHOKO_API_KEY", "SOULSYNC_URL",
                                                   "SOULSYNC_API_KEY", "RADARR_URL", "RADARR_API_KEY",
                                                   "SONARR_URL", "SONARR_API_KEY", "LIDARR_URL",
                                                   "LIDARR_API_KEY")}
for k in _saved:
    setattr(settings, k, None)
active = {t.job_id for t in dc.active_registry()}
full = {t.job_id for t in dc._registry()}
check("without Shoko, SoulSync or an arr their tasks neither run nor show",
      not ({"shoko_watched", "shoko_metadata", "music_catalog_sync", "arr_pre_enrich",
            "arr_ledger", "editions_sync"} & active), sorted(full - active))
check("the full registry keeps every job id (stamps and reports resolve)",
      {"shoko_watched", "arr_pre_enrich"} <= full)
check("the enrichment cycle and the sync are always there",
      {"custodian_enrich", "plex_sync", "arr_sync"} <= active)
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr.local:8989", "k"
check("Sonarr set: the arr prefetch and the editions walk are back",
      {"arr_pre_enrich", "editions_sync"} <= {t.job_id for t in dc.active_registry()})
for k, v in _saved.items():
    setattr(settings, k, v)


# ── the first sync: taste to the model queue, maintenance at once ───────────
from src.routers import history as hist  # noqa: E402
from src.services import app_state  # noqa: E402
app_db()

calls = {}


async def _fake_sync(force=False, recompute_taste=True):
    calls["recompute_taste"] = recompute_taste
    return {"synced": 12}


async def _fake_tick(**kw):
    calls["tick"] = kw
    return {"started": ["model"]}


_real_sync, _real_tick = hist.sync_plex_history, dc.custodian_tick
_real_done = dict(dc._first_run_done)
hist.sync_plex_history, dc.custodian_tick = _fake_sync, _fake_tick
import src.services.media_server as _ms  # noqa: E402
_real_kind = _ms.active_kind
_ms.active_kind = lambda *a, **k: "plex"
for q in dc._first_run_done:
    dc._first_run_done[q] = False
asyncio.run(hist._run_sync(force=True))
check("the onboarding's sync leaves the taste half to the model queue",
      calls.get("recompute_taste") is False)
check("...and flags the follow-up, so it runs", bool(app_state.get_state("plex_followup_pending")))
check("a fresh install starts maintenance right after its first sync",
      calls.get("tick") == {"wait": False})
calls.clear()
for q in dc._first_run_done:
    dc._first_run_done[q] = True
asyncio.run(hist._run_sync(force=True))
check("an install that already ticked waits for its interval", "tick" not in calls)
hist.sync_plex_history, dc.custodian_tick, _ms.active_kind = _real_sync, _real_tick, _real_kind
dc._first_run_done.update(_real_done)


# ── the programs open during setup are answered, not asked ─────────────────
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import GameProcess  # noqa: E402
from src.routers import setup as setup_router  # noqa: E402

setup_router._remember_programs(["Zen.exe", "claude.exe", "notes.txt"], ["Fallout4.exe"])
with get_db_session() as db:
    rows = {r.process_name: r.is_game for r in db.query(GameProcess).all()}
check("what was open while setting up is remembered as no game, a ticked-off one as a game",
      rows.get("zen.exe") is False and rows.get("claude.exe") is False and rows.get("fallout4.exe") is True, rows)
check("...and only program names are stored", "notes.txt" not in rows)


# ── the arr apps, found by their own web page ───────────────────────────────
from src.services import arr_find  # noqa: E402
import httpx  # noqa: E402

check("an arr app is named by its page title, whatever the port",
      arr_find.which_app("<html><head><title>Sonarr</title>") == "sonarr"
      and arr_find.which_app("<title>Plex</title>") is None)
check("a host that is no plain name or address is not asked",
      arr_find.clean_host("192.168.0.189") == "192.168.0.189" and arr_find.clean_host("a b/c") is None)


def _lan(req):
    if req.url.host == "192.168.0.189" and req.url.port == 30025:
        return httpx.Response(200, text="<title>Radarr</title>")
    if req.url.host == "192.168.0.189" and req.url.port == 30113:
        return httpx.Response(200, text="<title>Sonarr</title>")
    return httpx.Response(404)


import src.services.media_server.jellyfin_connect as jc  # noqa: E402
_real_allowed = jc._allowed


async def _all_allowed(url):
    return True
jc._allowed = _all_allowed
found = asyncio.run(arr_find.find_arrs(["192.168.0.189"], transport=httpx.MockTransport(_lan)))
jc._allowed = _real_allowed
check("Radarr and Sonarr next to the media server are found on the TrueNAS ports",
      found == {"radarr": "http://192.168.0.189:30025", "sonarr": "http://192.168.0.189:30113"}, found)


# ── TMDB: the long token is named, not refused as a wrong key ────────────────
from src.services.setup_wizard import test_tmdb  # noqa: E402
r = asyncio.run(test_tmdb("eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ4In0.c2lnbmF0dXJl"))
check("the API Read Access Token is recognised and the short key asked for",
      r["ok"] is False and "short API Key" in r["error"])

# ── logos come from the services' own pages, the best icon first ────────────
from src.services import setup_icons  # noqa: E402
page = ('<link rel="icon" href="/f.svg"><link rel="icon" sizes="32x32" href="/32.png">'
        '<link rel="apple-touch-icon" sizes="180x180" href="/apple.png">')
check("a page's apple-touch-icon first, the root favicon last",
      setup_icons.icon_candidates(page, "https://x.org") ==
      ["https://x.org/apple.png", "https://x.org/f.svg", "https://x.org/32.png", "https://x.org/favicon.ico"],
      setup_icons.icon_candidates(page, "https://x.org"))


def _site(req):
    if req.url.path == "/":
        return httpx.Response(200, text='<link rel="apple-touch-icon" href="/a.png">')
    if req.url.path == "/a.png":
        return httpx.Response(200, content=b"png-bytes", headers={"content-type": "image/png"})
    return httpx.Response(404)


got = asyncio.run(setup_icons.service_logo("tmdb", transport=httpx.MockTransport(_site)))
check("a service's logo is fetched from its own site, an image only", got and got[1] == "image/png")
check("a key outside the list fetches nothing", asyncio.run(setup_icons.service_logo("evil")) is None)


# ── the onboarding's bar follows the sync ───────────────────────────────────
from src.services import plex_sync  # noqa: E402
plex_sync._progress("history", 12000, 38465)
check("the sync says where it is: the plays read of Plex's own total",
      hist.sync_state(user=SimpleNamespace(id=1))["progress"] == {"phase": "history", "done": 12000,
                                                                "total": 38465, "label": ""})
plex_sync.SYNC_PROGRESS.clear()

# ── the shelves wait until the library is known ─────────────────────────────
pushed = []


async def _push(designs):
    pushed.append(designs)
    return {"created": len(designs)}
import src.services.collection_designer as _cd  # noqa: E402
import src.services.plex_collections as _pc  # noqa: E402
_real = (_cd.design_collections, _pc.push_collections)
_pc.push_collections = _push


async def _design():
    return [{"title": "x"}]
_cd.design_collections = _design
check("a fresh install's queue does not replace the shelves before it knows the library",
      asyncio.run(dc._run_collections()) is False and pushed == [])
check("...ready once enough films and series have a profile",
      dc._collections_ready()[2] >= 20)
_cd.design_collections, _pc.push_collections = _real

check.done()
