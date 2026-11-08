"""[TONIGHT CANDIDATES] (stage 2 of docs/UX_RETENTION_DESIGN.md, C2): what
the member could start tonight goes into the chat prompt - the series they
are in the middle of, their stored recommendations in the library lanes and
their Swipe "want to watch" answers - instead of the vector neighbours of
the sentence "what should I watch tonight?". No model call, no new source,
nothing the member has played since it was stored.

Every title here is invented.

    python tests/test_tonight_candidates.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import json
from datetime import datetime, timedelta

conn = app_db()

from src.database.models import (  # noqa: E402
    CachedRecommendation, SwipeRating, User, WatchHistoryEntry)
from src.services import chat_prompt as cp  # noqa: E402
from src.services import tonight  # noqa: E402

NOW = datetime(2026, 11, 10, 19, 0)
HOUR = 3_600_000

with conn.get_db_session() as db:
    u = User(plex_user_id="tn-1", plex_username="Member")
    db.add(u)
    db.commit()
    UID = u.id

    def play(title, mt, days, series=None, season=None, episode=None, key=None):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="tn-1", plex_item_id=key or f"{title}-{days}",
                                 title=title, series_title=series, media_type=mt, season=season,
                                 episode=episode, duration_ms=HOUR, view_offset_ms=HOUR, completed=True,
                                 viewed_at=NOW - timedelta(days=days)))

    for n in (1, 2, 3, 4):                                    # in the middle of it
        play(f"Episode {n}", "anime", 4 - n * 0.5, series="Starfall Archive", season=1, episode=n)
    for n in (1, 2):                                          # two plays: not "in progress"
        play(f"Episode {n}", "show", 2, series="Harbour Lights", season=1, episode=n)
    play("Gull Point", "movie", 1)                            # played since it was recommended
    play("The Quiet Harbour", "movie", 900)                   # loved once, long ago

    def rec(title, lane, cat="movie", conf=0.8, anchors=None, cached=NOW - timedelta(days=3), reason=None):
        sf = json.dumps({"kind": "recommendation", "lane": lane, "anchors": anchors,
                         "factors": []}) if anchors else None
        db.add(CachedRecommendation(user_id=UID, category=cat, lane=lane, title=title,
                                    reason=reason or f"Stored reason for {title}.", confidence=conf,
                                    year=2019, cached_at=cached, score_factors=sf))

    rec("The Lantern Room", "because", anchors=["Gull Point", "Tide Clock", "Salt Road"], conf=0.95)
    rec("Gull Point", "library", conf=0.9)                    # played since: out
    rec("North Signal", "library", conf=0.85, reason="A patient coastal mystery " + "with a long tail " * 20)
    rec("The Quiet Harbour", "rediscover", conf=0.8, cached=NOW - timedelta(days=3))
    rec("Far Away Store", "discovery", conf=0.99)             # not in the library: out
    rec("The Hollow Pines", "library", cat="music", conf=0.99)  # music only when asked for music
    for i in range(5):
        rec(f"Extra Film {i}", "library", conf=0.5 - i / 100)

    def swipe(title, want=True, seen=False, days=5, cat="movie"):
        db.add(SwipeRating(user_id=UID, category=cat, doc_id=f"doc-{title}", title=title, seen=seen,
                           want=want, updated_at=NOW - timedelta(days=days), created_at=NOW - timedelta(days=days)))

    swipe("Tide Clock")
    swipe("Gull Point")                                       # played: out
    swipe("Salt Road", days=400)                              # a want from over half a year ago: out
    swipe("Paper Moon Hotel", want=False, seen=True)          # an answer, not a want
    db.commit()

got = tonight.candidates(UID, now=NOW)
titles = [c["title"] for c in got]
check("the series in progress leads, with how far the member is", got[0]["title"] == "Starfall Archive"
      and got[0]["source"] == "in progress" and "episode 4" in got[0]["note"], got[0])
check("two plays are not a series in progress", "Harbour Lights" not in titles, titles)
check("the stored lanes in turn - because, library, rediscover - at most four",
      [c["title"] for c in got if c["source"].startswith(("recommended", "rediscover"))]
      == ["The Lantern Room", "North Signal", "The Quiet Harbour", "Extra Film 0"], titles)
check("a because pick names the three titles it is close to",
      next(c for c in got if c["title"] == "The Lantern Room")["source"]
      == "recommended because they watched Gull Point, Tide Clock and Salt Road")
check("a rediscover pick stays though it was played - before it was stored",
      "The Quiet Harbour" in titles)
check("a title played since it was stored, the discovery lane and music are left out",
      not {"Gull Point", "Far Away Store", "The Hollow Pines"} & set(titles), titles)
check("the stored reason is kept short", all(len(c["note"]) <= 160 for c in got))
check("the Swipe wants: the recent one, not the played one, the old one or a seen answer",
      [c["title"] for c in got if "Swipe" in c["source"]] == ["Tide Clock"], titles)
check("only music when the chat is about music",
      [c["title"] for c in tonight.candidates(UID, domain="music", now=NOW)] == ["The Hollow Pines"])
check("a domain narrows it: films only for a film question",
      {c["category"] for c in tonight.candidates(UID, domain="movie", now=NOW)} == {"movie"})

text = tonight.block(got)
lines = text.splitlines()
check("the block: the header says when to use it, one line per candidate",
      lines[0] == tonight.HEADER and len(lines) == len(got) + 1
      and lines[2] == "- The Lantern Room (2019, film) - recommended because they watched Gull Point, "
                      "Tide Clock and Salt Road: Stored reason for The Lantern Room.", lines[:3])
check("the header leaves a targeted ask to the curator's own knowledge (the owner's measured wording)",
      "when they name a mood, genre or title to match, use one only if its stored reason matches it"
      in tonight.HEADER and "otherwise answer as you would without this list" in tonight.HEADER
      and tonight.HEADER.endswith("on any other turn leave them out."))
check("no candidates, no block", tonight.block([]) == ""
      and tonight.tonight_block(424242, now=NOW) == "")
sp = cp.build_system_prompt(voice="neutral", user_lang="en", tonight_context=text)
check("the chat prompt carries it after the library items",
      text in sp and sp.index("RELEVANT LIBRARY ITEMS") < sp.index("[TONIGHT CANDIDATES]"))

# ── the landing's Tonight card ──────────────────────────────────────────────

from zoneinfo import ZoneInfo  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def local(y, m, d, h):
    return datetime(y, m, d, h)

check("the moment: a Tuesday evening, a Friday evening, a Sunday afternoon, late, a morning",
      [tonight._moment(local(2026, 11, 10, 20)), tonight._moment(local(2026, 11, 13, 20)),
       tonight._moment(local(2026, 11, 15, 15)), tonight._moment(local(2026, 11, 10, 23)),
       tonight._moment(local(2026, 11, 10, 9))]
      == ["weeknight", "film_night", "sunday", "late", "later"])


def card_at(utc):
    return tonight.tonight_card(UID, now=utc, zone=BERLIN)

tue = card_at(datetime(2026, 11, 10, 19, 0))            # 20:00 in Berlin, a Tuesday
check("a weeknight: the series in progress first, its line names the weekday",
      tue["moment"] == "weeknight" and tue["items"][0]["title"] == "Starfall Archive"
      and tue["items"][0]["headline"] == "A Tuesday evening: carry on with **Starfall Archive**"
      and tue["items"][0]["why"] == "You are in the middle of it - up to season 1, episode 4.", tue["items"][0])
whys = {i["title"]: i["why"] for i in tue["items"]}
check("the card's second line is facts said to the member: never the stored pitch (the "
      "curator's voice), never the wording meant for the model",
      whys["The Lantern Room"] == "Because you watched Gull Point, Tide Clock and Salt Road · 2019"
      and whys["North Signal"] == "In your library, not watched yet · 2019"
      and whys["Tide Clock"] == "You marked it 'Want to watch' in Swipe."
      and not any("Stored reason for" in w and "Quiet Harbour" not in w for w in whys.values())
      and not any("they " in w or "do NOT" in w for w in whys.values()), whys)
check("a Rediscover pick shows its one fact line (the lane's own, in the member's language), "
      "not a second sentence that contradicts it",
      whys["The Quiet Harbour"] == "Stored reason for The Quiet Harbour.", whys)
de = tonight.tonight_card(UID, now=datetime(2026, 11, 10, 19, 0), zone=BERLIN, lang="de")
check("a German member reads the card in German: label, line and facts",
      de["label"] == "Heute Abend"
      and de["items"][0]["headline"] == "Dienstagabend: weiter mit **Starfall Archive**"
      and de["items"][0]["why"] == "Du bist mittendrin - bis Staffel 1, Folge 4."
      and {i["title"]: i["why"] for i in de["items"]}["The Lantern Room"]
      == "Weil du Gull Point, Tide Clock und Salt Road geschaut hast · 2019", de["items"][:2])
check("an English member's label", tue["label"] == "Tonight")
check("the card's 'Ask the curator' question in the member's language, the daytime one for later",
      tue["ask"] == "What should I watch tonight?" and de["ask"] == "Was soll ich heute Abend schauen?"
      and card_at(datetime(2026, 11, 10, 8, 0))["ask"] == "What could I put on later today?")
fri = card_at(datetime(2026, 11, 13, 19, 0))
check("Friday evening: a film first", fri["items"][0]["category"] == "movie"
      and fri["items"][0]["headline"] == "Friday - room for a film: **The Lantern Room**", fri["items"][0])
sun = card_at(datetime(2026, 11, 15, 14, 0))
check("Sunday afternoon: the rewatch first",
      sun["items"][0]["headline"] == "Sunday rewatch? **The Quiet Harbour**", sun["items"][0])
morning = card_at(datetime(2026, 11, 10, 8, 0))
check("in the day: a Swipe want for later first",
      morning["items"][0]["headline"] == "For later: **Tide Clock**, you wanted to watch it", morning["items"][0])
check("every candidate stays behind 'Something else', each once",
      sorted(i["title"] for i in tue["items"]) == sorted(i["title"] for i in morning["items"])
      and len(tue["items"]) == len(set(i["title"] for i in tue["items"])))
check("no candidates, no card", tonight.tonight_card(424242, now=NOW, zone=BERLIN) is None)

# the route: links into the member's server, and the watchlist add
import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import src.services.media_server as media_server  # noqa: E402
import src.services.plex_playlists as plex_playlists  # noqa: E402
import src.services.plex_watchlist as plex_watchlist  # noqa: E402
import src.services.tonight as tonight_mod  # noqa: E402
from src.routers import home  # noqa: E402

from src.database.models import LibraryItem  # noqa: E402

with conn.get_db_session() as db:
    db.query(CachedRecommendation).filter(CachedRecommendation.title == "The Lantern Room").update(
        {"plex_rating_key": "4711"})
    db.add(LibraryItem(plex_rating_key="777", media_type="anime", title="Starfall Archive"))
    db.commit()
series = next(c for c in tonight.candidates(UID, now=NOW) if c["kind"] == "series")
check("a series in progress links to the series (the ledger's key), not the episode already watched",
      series["key"] == "777", series)


async def _mid(force=False):
    return "MACHINE"

added = []


async def _add(uid, title, year=None, media_type=None):
    added.append((uid, title, year, media_type))
    return {"ok": True, "matched": title}

_saved = (plex_playlists.get_machine_identifier, media_server.active_kind, plex_watchlist.add_to_watchlist,
          tonight_mod.tonight_card)
plex_playlists.get_machine_identifier = _mid
media_server.active_kind = lambda settings=None: "plex"
plex_watchlist.add_to_watchlist = _add
_real_card = tonight_mod.tonight_card
tonight_mod.tonight_card = lambda uid: _real_card(uid, now=datetime(2026, 11, 13, 19, 0), zone=BERLIN)
try:
    out = asyncio.run(home.tonight(user=SimpleNamespace(id=UID)))
    w = asyncio.run(home.watchlist(home.WatchlistBody(title="The Lantern Room", category="movie",
                                                              year=2019), user=SimpleNamespace(id=UID)))
finally:
    (plex_playlists.get_machine_identifier, media_server.active_kind, plex_watchlist.add_to_watchlist,
     tonight_mod.tonight_card) = _saved
first = out["card"]["items"][0]
check("GET /api/home/tonight: the pick links into Plex for every account the server is shared with",
      first["title"] == "The Lantern Room"
      and first["link"] == "https://app.plex.tv/desktop/#!/server/MACHINE/details?key=%2Flibrary%2Fmetadata%2F4711"
      and all("key" not in i for i in out["card"]["items"]), first)
check("an item without a server key has no link", all("link" not in i for i in out["card"]["items"][1:]
                                                       if i["title"] != "Starfall Archive"))
check("POST /api/home/watchlist: the member's own watchlist, as a Swipe 'Want to watch'",
      w["ok"] is True and added == [(UID, "The Lantern Room", 2019, "movie")])

check.done()
