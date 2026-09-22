"""Registers, continuity with earlier answers, the morning line.

2026-09-22, owner: "die ideen sind alle drei gut bau das" — every message
drew the same provocative voice, never referred to what he had answered
last time, and a nudge about an evening arrived days later. Real modules,
no mocks.

    python tests/test_proactive_alive.py
"""
import pathlib
import random
import sys
from datetime import datetime, timedelta, time

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import proactive_messages as pm  # noqa: E402
from src.services.viewing_sessions import utc_offset  # noqa: E402

EP_MS = 1_422_083


def _row(title, media_type, at, *, series=None, ep=None, dur=EP_MS, off=None, done=True):
    return {"title": title, "series_title": series, "media_type": media_type, "season": 1, "episode": ep,
            "viewed_at": at, "duration_ms": dur, "view_offset_ms": dur if off is None else off,
            "completed": done, "genres": ""}


def test_registers_vary_and_never_repeat_the_previous_one():
    rng = random.Random(3)
    seen = {pm._pick_register(None, rng) for _ in range(400)}
    assert seen == {"provocative", "curious", "dry", "warm", "analytic"}
    assert all(pm._pick_register("dry", rng) != "dry" for _ in range(300))
    assert pm._register_line({"register": "warm"}).startswith("Register: warm")
    assert pm._register_line({}) == pm._REGISTER_LINES["curious"], "unknown or missing register reads as curious"
    assert pm._register_of('{"register": "dry"}') == "dry" and pm._register_of("nope") is None


def test_the_prompt_carries_the_register_and_no_baked_in_tone():
    trigger = {"type": "binge_episode", "series": "Frieren", "count": 4, "rhythm": "4 episodes in one sitting", "register": "warm"}
    p = pm._compose_prompt(trigger, "")
    assert p.endswith(pm._REGISTER_LINES["warm"])
    body = p[: -len(pm._REGISTER_LINES["warm"])]          # the register line is the only tone
    for baked in ("slightly teasing", "maybe provocative", "No filter", "brutally honest", "cheeky", "confrontational"):
        assert baked not in body, baked
    assert "NAME the specific title" in p
    for t in ({"type": "guilty_pleasure", "title": "X", "rating": 4.1}, {"type": "genre_rut", "genre": "horror", "count": 12},
              {"type": "procrastinator", "series": "Y", "days": 120, "episodes": 5},
              {"type": "attention_deficit", "titles": ["A", "B"]}):
        q = pm._compose_prompt(t, "")
        assert "garbage" not in q and "trash" not in q and "confrontational" not in q, t["type"]
    assert pm._compose_prompt({"type": "unknown_type"}, "") is None


def test_the_prompt_builds_on_the_earlier_answer():
    trigger = {"type": "series_completion", "series": "Frieren", "episodes_watched": 9, "rhythm": "9 episodes over 2 sittings",
               "register": "curious",
               "prior": {"asked": "Nine episodes of Frieren?", "answer": "Episode 3 was slow, then it clicked.",
                         "reply": "Fair.", "when": "2026-09-10"}}
    p = pm._compose_prompt(trigger, "")
    assert "EARLIER EXCHANGE about this (2026-09-10)" in p
    assert "Episode 3 was slow, then it clicked." in p and "you replied \"Fair.\"" in p
    assert "never ask the same thing again" in p
    assert "EARLIER EXCHANGE" not in pm._compose_prompt({**trigger, "prior": None}, "")


def test_prior_exchange_finds_the_newest_thread_the_user_answered_in():
    d1, d2 = datetime(2026, 9, 10, 20, 0), datetime(2026, 9, 10, 20, 5)
    asked = {"tracks": set(), "titles": set(), "series": {}, "subjects": {},
             "ids": {"series:frieren": [77, 42]}}     # 77 newest, 42 older

    def fetch(user_id, ids):
        assert user_id == 1 and ids == [77, 42]
        return {"messages": {77: "Still on Frieren?", 42: "Nine episodes of Frieren?"},
                "turns": [(42, "user", "Episode 3 was slow, then it clicked.", d1), (42, "assistant", "Fair.", d2),
                          (77, "assistant", "Still on Frieren?", d2)]}   # 77: the user never answered

    trigger = {"type": "binge_episode", "series": "Frieren", "count": 4}
    prior = pm._prior_exchange(1, trigger, asked, fetch=fetch)
    assert prior["message_id"] == 42 and prior["answer"] == "Episode 3 was slow, then it clicked."
    assert prior["asked"] == "Nine episodes of Frieren?" and prior["reply"] == "Fair." and prior["when"] == "2026-09-10"
    assert pm._prior_exchange(1, {"type": "binge_episode", "series": "Other", "count": 3}, asked, fetch=fetch) is None
    assert pm._prior_exchange(1, trigger, None, fetch=fetch) is None


def test_all_subject_keys_join_pattern_and_title_keys():
    assert "series:frieren" in pm._all_subject_keys("binge_episode", {"series": "Frieren"})
    assert "title:the matrix" in pm._all_subject_keys("rewatch", {"title": "The Matrix", "is_series": False})
    assert "series:frieren" in pm._all_subject_keys("rewatch", {"title": "Frieren", "media_type": "anime"})
    keys = pm._all_subject_keys("track_obsession", {"track": "Influencer", "artist": "K.I.Z"})
    assert "track:influencer" in keys and "artist:k.i.z" in keys, "a music thread also keys on the artist"
    assert "artist:k.i.z" in pm._all_subject_keys("music_marathon", {"artist": "K.I.Z", "hours": 3.2})
    assert pm._all_subject_keys("new_genre", {"genre": "Latin"}) == ["new_genre:latin"]
    assert pm._all_subject_keys("last_night", {"date": "2026-09-22", "series": "Frieren"}) == ["last_night:2026-09-22", "series:frieren"]


def _local_to_utc(local_dt):
    return local_dt - utc_offset()


def test_the_morning_line_sums_up_last_night_and_only_in_the_morning():
    today = datetime(2026, 9, 22)
    ev = today - timedelta(days=1)
    entries = [
        _row("Frieren E5", "anime", _local_to_utc(datetime.combine(ev, time(20, 0))), series="Frieren", ep=5),
        _row("Frieren E6", "anime", _local_to_utc(datetime.combine(ev, time(20, 25))), series="Frieren", ep=6),
        _row("Heat", "movie", _local_to_utc(datetime.combine(ev, time(22, 0))), dur=10_000_000),
        _row("Some track", "music", _local_to_utc(datetime.combine(ev, time(15, 0))), series="Otis Redding", dur=200_000),   # afternoon: outside
        _row("Old", "anime", _local_to_utc(datetime.combine(ev - timedelta(days=3), time(21, 0))), series="Old", ep=1),      # days ago
    ]
    morning = _local_to_utc(datetime.combine(today, time(8, 30)))
    hit = pm.detect_last_night(entries, morning)
    assert hit and hit["type"] == "last_night" and hit["date"] == "2026-09-22"
    assert "\"Frieren\": 2 episodes" in hit["summary"] and "the film \"Heat\"" in hit["summary"], hit["summary"]
    assert "Otis Redding" not in hit["summary"] and "Old" not in hit["summary"]
    assert hit["ended"] == "22:00" and hit["series"] == "Frieren"
    assert pm.detect_last_night(entries, _local_to_utc(datetime.combine(today, time(14, 0)))) is None, "afternoon: no morning line"
    asked = {"tracks": set(), "titles": set(), "series": {}, "subjects": {"last_night:2026-09-22": morning - timedelta(hours=1)}}
    assert pm.detect_last_night(entries, morning, asked) is None, "one per day"
    assert pm.detect_last_night([], morning) is None
    exp = pm._expiry_for(hit, morning)
    assert exp == _local_to_utc(datetime.combine(today, time(12, 0))), "gone by local noon"
    assert pm._expiry_for({"type": "rewatch"}, morning) == morning + timedelta(days=7)


def test_the_pick_puts_the_morning_line_right_after_a_watched_recommendation():
    now = datetime(2026, 9, 22, 6, 30)
    hits = [("rewatch", {"type": "rewatch"}), ("last_night", {"type": "last_night"})]
    assert pm._pick_trigger(hits, {}, now, random.Random(1))["type"] == "last_night"
    hits.append(("recommendation_followup", {"type": "recommendation_followup"}))
    assert pm._pick_trigger(hits, {}, now, random.Random(1))["type"] == "recommendation_followup"
    assert "last_night" in pm.TRIGGER_TYPE_NAMES, "the toggle exists in Settings → Notifications"


def test_load_asked_subjects_indexes_message_ids_per_subject():
    from unittest.mock import patch

    class Q:
        def __init__(self, rows): self.rows = rows
        def filter(self, *a, **k): return self
        def order_by(self, *a, **k): return self
        def limit(self, *a, **k): return self
        def all(self): return self.rows

    class S:
        def __init__(self, rows): self.rows = rows
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def query(self, *a, **k): return Q(self.rows)

    rows = [("binge_episode", '{"series": "Frieren", "count": 4}', datetime(2026, 9, 20), 77),
            ("new_genre", '{"genre": "latin"}', datetime(2026, 9, 19), 76),
            ("binge_episode", '{"series": "Frieren", "count": 3}', datetime(2026, 9, 1), 42),
            ("track_obsession", '{"track": "good song"}')]                       # the old 2-tuple shape still parses
    with patch("src.services.proactive_messages.get_db_session", lambda: S(rows)):
        res = pm._load_asked_subjects(1)
    assert res["ids"]["series:frieren"] == [77, 42] and res["ids"]["new_genre:latin"] == [76]
    assert res["subjects"]["new_genre:latin"] == datetime(2026, 9, 19)
    assert "good song" in res["tracks"]


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
