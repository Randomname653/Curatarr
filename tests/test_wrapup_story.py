"""Wrap-up story (services/wrapup_story): the fact sheet, the stand-in, the task.

Pinned here: the fact sheet numbers every fact and item; the deterministic
story; the chat thread for "Ask your wrap-up" gets the same sheet; the
generation task walks its phases and stores the story the writer
(services/wrapup_writer, tests/test_wrapup_writer.py) produced; the season.

    python tests/test_wrapup_story.py
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.wrapup_story as st  # noqa: E402

from _checks import check  # noqa: E402


FACTS = {
    "scope": "all", "eligible": True, "facts_hash": "h1",
    "period": {"key": "30d", "label": "Last 30 days"},
    "tempo": {"hours": 13.5, "plays": 22, "active_days": 8, "longest_streak_days": 2,
              "peak_hour": 21, "dayparts": {"night": 0.1, "evening": 0.7}, "weekend_share": 0.3,
              "biggest_day": {"date": "2026-10-12", "hours": 5.0, "plays": 6},
              "hours_estimated_share": 0.13},
    "top": [{"title": "Dark", "media_type": "show", "hours": 5.0, "plays": 6},
            {"title": "Heat", "media_type": "movie", "hours": 5.7, "plays": 2},
            {"title": "Bebop", "media_type": "anime", "hours": 0.4, "plays": 1},
            {"title": "Old Film", "media_type": "movie", "hours": 1.8, "plays": 1}],
    "contrarian": {"title": "Dark", "media_type": "show", "you": 9.0, "community": 6.0,
                   "votes": 1000, "gap": 3.0, "basis": "rated"},
    "dismissal": {"title": "Cats", "media_type": "movie", "how": "dropped", "community": 8.5, "votes": 50000},
    "curatarr": {"graveyard": {"deleted": 1, "gb_freed": 2.0,
                               "biggest": {"title": "Junk", "gb": 2.0, "media_type": "movie"}},
                 "dust_collector": {"title": "Avatar", "media_type": "movie", "gb": 58.6}},
    "look_ahead": [{"title": "Alien", "media_type": "movie", "year": 1979, "reason": "Your kind of dread"}],
    "traits": [{"key": "contrarian", "strength": 0.3, "evidence": "9.0 vs 6.0 on Dark"},
               {"key": "archivist", "strength": 0.2, "evidence": "viewing age 1995"}],
    "posters": {"Dark": "https://image.tmdb.org/t/p/w500/dark.jpg"},
}

sheet, idx = st.render_fact_sheet(FACTS)
print("\nfact sheet")
check("every fact numbered", sheet.startswith("F1: ") and "F10:" in sheet)
check("titles become T# items with their artwork",
      any(v["title"] == "Dark" and v["poster"] for v in idx["items"].values()))
check("owned-unwatched picks become U#", {v["title"] for v in idx["picks"].values()} == {"Avatar", "Alien"})
check("traits offered to the model", idx["traits"] == ["contrarian", "archivist"] and "TRAITS" in sheet)
check("its numbers are the allowed set", {"13.5", "58.6", "1979", "8.5"} <= idx["numbers"])
check("number normalisation: thousands, decimal comma, .0",
      st.numbers_in("50,000 and 13,5 and 120.0") == {"50000", "13.5", "120"})

T = {v["title"]: k for k, v in idx["items"].items()}
U = {v["title"]: k for k, v in idx["picks"].items()}


def story(**over):
    s = {
        "archetype_title": "Loyalist With Exceptions",
        "archetype_subhead": "Nine stars for a 6.0, a walkout on an 8.5.",
        "archetype_trait": "contrarian",
        "era_chapters": [
            {"title": "The Dark Weekend", "kicker": "Six episodes, one evening",
             "commentary": "You gave Dark a 9.0 while everyone else settled on 6.0.",
             "item_refs": [T["Dark"]], "fact_refs": ["F1"]},
            {"title": "Old Friends", "kicker": "Heat, again",
             "commentary": "Heat came back for 2 more evenings.",
             "item_refs": [T["Heat"], T["Old Film"]], "fact_refs": ["F2"]}],
        "roast_and_toast": {"roast": "You walked out on Cats at 8.5.", "roast_refs": ["F3"],
                            "toast": "When you commit, you commit.", "toast_refs": ["F4"]},
        "look_ahead_hook": {"challenge": "Alien has been waiting on the server.", "item_ref": U["Alien"],
                            "fact_refs": []},
        "cluster_names": [],
        "share_line": "13.5 hours, one contrarian streak",
    }
    s.update(over)
    return json.dumps(s)


print("\nresolution and fallback")
done = st.finish_story(json.loads(story()), idx)
check("refs resolve to title + artwork for the page", done["refs"][T["Dark"]]["poster"].endswith("dark.jpg")
      and done["refs"][U["Alien"]]["title"] == "Alien")
fb = st.fallback_story(FACTS, idx)
check("fallback names the strongest trait", fb["archetype_title"] == "The Contrarian")
check("fallback chapters anchor on the top titles", fb["era_chapters"][0]["item_refs"][0] == T["Dark"])
check("fallback says only facts", "5.0 hours" in fb["era_chapters"][0]["commentary"])
check("fallback hook is an owned-unwatched pick", fb["look_ahead_hook"]["item_ref"] in idx["picks"])

calls = []


async def fake_bg(owner, call, **kw):
    return await call()


def fake_post_factory(answers):
    async def fake_post(url, payload, *, read_timeout):
        calls.append(payload)
        return {"message": {"content": answers.pop(0)}}
    return fake_post


import src.services.llm_errors as le  # noqa: E402
import src.services.llm_priority as lp  # noqa: E402
import src.services.ollama_hosts as oh  # noqa: E402

lp.background_curator_call = fake_bg
oh.host_for = lambda role: type("H", (), {"url": "http://x", "profile": None, "num_ctx": 16384, "gpu": {}})()

print("\nask your wrap-up")
block = st.wrapup_discussion_block(FACTS, done)
check("the chat context carries the fact sheet and the page's words",
      "F1:" in block and "Loyalist With Exceptions" in block and "only numbers you may quote" in block)
from src.routers.chat import _thread_id_for  # noqa: E402
from src.schemas.chat import DiscussContext  # noqa: E402
check("one chat thread per wrap-up",
      _thread_id_for(DiscussContext(kind="wrapup", wrapup_scope="all", wrapup_period="year:2026"))
      == "wrapup:all:year:2026")
js = (ROOT / "frontend/js/chat.js").read_text(encoding="utf-8")
check("the frontend derives the same thread id", "`wrapup:${ctx.wrapup_scope}:${ctx.wrapup_period}`" in js)

print("\nthe generation task")
import src.routers.wrapup as wr  # noqa: E402
import src.services.task_monitor as tm  # noqa: E402
import src.services.wrapup_facts as wf  # noqa: E402
import src.services.wrapup_store as ws  # noqa: E402

saved = {}
wf.build_wrapup_facts = lambda uid, scope, period, is_admin=False: dict(FACTS)


async def no_drift(*a):
    return {}


async def no_posters(facts):
    facts.setdefault("posters", {})
wf.taste_drift = no_drift
wr.attach_posters = no_posters
ws.save_facts = lambda *a: saved.setdefault("facts", a)
ws.save_story = lambda uid, scope, period, h, story, source, model=None: saved.update(story=story, source=source, hash=h)
ws.save_error = lambda *a: saved.setdefault("error", a)
import src.services.wrapup_insights as wi  # noqa: E402

CARDS = wi.mine(FACTS)
st._voice = lambda uid, cards: ("en", [])
EDIT = json.dumps({"arc": [c["id"] for c in CARDS[:3]], "roast": CARDS[0]["id"],
                   "toast": CARDS[1]["id"], "archetype_trait": "contrarian",
                   "archetype_title": "Loyalist With Exceptions",
                   "archetype_subhead": "Your standards are yours.", "share_line": "One contrarian streak",
                   "next_pick": "", "cluster_names": []})
PROSE = "\n".join(f"## {c['id']} | Beat {i}\nIt happened [{c['id']}]." for i, c in enumerate(CARDS[:3], 1))
PROSE += f"\n## ROAST\nA blind spot [{CARDS[0]['id']}].\n## TOAST\nA real taste [{CARDS[1]['id']}]."
le.post_chat = fake_post_factory([EDIT, PROSE])
task = tm.task_monitor.create("Wrap-up: test", "wrapup", total=len(st.PHASES), task_id="wrapup-1-all-30d")
tm.task_monitor.start(task)
asyncio.run(st.generate_wrapup(1, "all", "30d", True, task))
check("the task walks every phase and finishes", task.processed == 5 and str(task.status.value) == "done")
check("the writer's story is stored for this facts hash", saved.get("source") == "llm"
      and saved.get("hash") == "h1" and saved["story"]["version"] == 2 and saved["story"]["refs"]
      and len(saved["story"]["slides"]) == 3)
wf.build_wrapup_facts = lambda uid, scope, period, is_admin=False: {"empty": True, "eligible": False}
task2 = tm.task_monitor.create("Wrap-up: empty", "wrapup", total=6, task_id="wrapup-1-all-6m")
tm.task_monitor.start(task2)
asyncio.run(st.generate_wrapup(1, "all", "6m", False, task2))
check("not enough played → the task is skipped, nothing written", str(task2.status.value) == "skipped")

print("\nthe season")
from datetime import datetime  # noqa: E402
fresh = {"story": {"x": 1}, "story_at": "2026-12-10T00:00:00", "story_hash": "a", "facts_hash": "a"}
check("nothing stored → the month is due", st.due_wrapups(lambda s, p: None, datetime(2026, 10, 1))
      == [("all", "30d")])
check("a fresh month is left alone outside the season",
      st.due_wrapups(lambda s, p: dict(fresh, story_at="2026-10-01T00:00:00"), datetime(2026, 10, 3)) == [])
check("a story for older numbers is due", ("all", "30d") in st.due_wrapups(
    lambda s, p: dict(fresh, story_at="2026-10-01T00:00:00", facts_hash="b"), datetime(2026, 10, 3)))
check("December: the current year is written", ("all", "year:2026") in st.due_wrapups(
    lambda s, p: None if p.startswith("year") else fresh, datetime(2026, 12, 11)))
jan = st.due_wrapups(lambda s, p: fresh, datetime(2027, 1, 3))
check("early January: the closed year once more (written before it closed)", ("all", "year:2026") in jan)
check("...and only once", ("all", "year:2026") not in st.due_wrapups(
    lambda s, p: dict(fresh, story_at="2027-01-02T00:00:00"), datetime(2027, 1, 5)))
dc = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("the custodian writes them ahead, waiting for a game like any curator work",
      '"wrapup_pregen"' in dc and "needs_llm=True" in dc.split('"wrapup_pregen"')[1][:160])

print("\nthe Plex playlist")
import src.services.plex_playlists as pp  # noqa: E402
made = []


async def _mid(force=False):
    return "m"


async def _video_key(title, cat, sections=None):
    return {"Heat": "11", "Dark": "22"}.get(title)


async def _next_ep(token, key):
    return key + "e1"


async def _track_key(track, artist, sections=None):
    return "33" if track == "Song" else None


async def _list(token, kind="video"):
    return [{"title": "Curatarr Wrap-Up · Last 30 days", "ratingKey": "old"}] if kind == "video" else []


async def _delete(token, key):
    made.append(("delete", key))


async def _create(token, title, keys, playlist_type="video"):
    made.append((title, playlist_type, keys))
    return {"ratingKey": "new", "leafCount": len(keys)}


async def _sections(fn, *a):
    return ["1"]
pp.get_machine_identifier, pp.resolve_video_key, pp.first_unwatched_episode_key = _mid, _video_key, _next_ep
pp.resolve_track_key, pp.list_playlists, pp.delete_playlist, pp.create_playlist = _track_key, _list, _delete, _create
pp.run_read = _sections
user = type("U", (), {"plex_token": "t", "plex_username": "owner"})()
out = asyncio.run(pp.push_wrapup_playlist(
    user, "Last 30 days",
    [{"title": "Heat", "media_type": "movie"}, {"title": "Dark", "media_type": "show"},
     {"title": "Nope", "media_type": "movie"}],
    [{"track": "Song", "artist": "Artist"}]))
check("a film is its own key, a series its next unwatched episode",
      ("Curatarr Wrap-Up · Last 30 days", "video", ["11", "22e1"]) in made)
check("music goes into its own audio playlist", any(m[1] == "audio" and m[2] == ["33"] for m in made if len(m) == 3))
check("the previous same-title playlist is replaced, not doubled", ("delete", "old") in made)
check("titles Plex does not have are reported", out["missing"] == ["Nope"])
no_tok = asyncio.run(pp.push_wrapup_playlist(type("U", (), {"plex_token": None})(), "x", [], []))
check("no stored token → skipped, nothing written", no_tok == {"skipped": "no_token"})

check.done()
