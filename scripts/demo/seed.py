"""Turn the demo copy into Jane Doe's household (README screenshots).

Runs inside the demo copy (scripts/demo/build_demo.py starts it there with
the copy as the working directory), never against a live database:

1. Scrub: every row that belongs to a real person goes - watch history,
   chats, discussions, proposals, memories, principles, swipes, ratings,
   protections, messages, recommendations, taste vectors, Wrap-Ups - and
   the two accounts become Jane Doe (the owner) and Sam, with no Plex token.
   Any other table with a user_id column is emptied too, so a table added
   later cannot carry someone's data into the demo. The library, its
   enrichment and the vector store stay: the titles are the real library's.
2. Seed: an invented 2026 for both, from library titles. Jane (docs/
   CURATOR_EXAMPLES.md) likes slow-burn horror and sharp social satire,
   has little patience for long sitcoms, and listens to trip-hop, dream pop
   and dark wave; Sam watches The Big Bang Theory from the start and
   comes back to it. Episode lists and runtimes from TMDB (read-only).
   The same seed every time (Random(7)), so every rebuild gives the same
   household; a title the library no longer has is skipped and named.
"""
from __future__ import annotations

import json
import random
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
DB = ROOT / "data" / "curatarr.db"
MUSIC = ROOT / "data" / "cache" / "plex_music.db"
ENRICH = ROOT / "data" / "cache" / "enrichment.db"
TZ = ZoneInfo("Europe/Berlin")
R = random.Random(7)
YEAR_END = datetime(2026, 10, 25, 20, 0)

WIPE = ["cached_recommendations", "chat_interactions", "chat_starters", "conversation_messages",
        "curator_principles", "curator_resolution_log", "deletion_proposals", "encrypted_taste_vectors",
        "episodic_memories", "plex_ratings", "proactive_messages", "protected_media", "swipe_ratings",
        "taste_vectors", "watch_history", "judge_verdicts", "protection_history", "music_skips",
        "library_events", "wrapups", "wrapup_snapshots", "household_prefs"]
KEEP_STATE = ("job_last_run:", "embedding_profile", "facet_backfill_", "tmdb_trending:", "tvdb_",
              "enrichment_last_settings", "last_enrichment_at", "last_tech_sync_at", "llm_lane")
MISSING: list = []


def scrub(c):
    tables = {t for (t,) in c.execute("select name from sqlite_master where type='table'")}
    for t in WIPE:
        if t in tables:
            c.execute(f'DELETE FROM "{t}"')  # nosec B608 - table names are the literals above
    # anything else keyed to a user that the list above missed
    for t in sorted(tables):
        cols = [r[1] for r in c.execute(f'pragma table_info("{t}")')]  # nosec B608 - names from sqlite_master
        if "user_id" in cols and t not in WIPE and t != "users":
            n = c.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]  # nosec B608 - names from sqlite_master
            if n:
                print(f"  also wiped {t} ({n})")
                c.execute(f'DELETE FROM "{t}"')  # nosec B608 - names from sqlite_master
    for (k,) in c.execute("select key from app_state").fetchall():
        if not k.startswith(KEEP_STATE):
            c.execute("delete from app_state where key=?", (k,))
    if "media_match_overrides" in tables:
        c.execute("update media_match_overrides set created_by=NULL")
    c.execute("delete from users where id not in (1, 2)")
    cols = {r[1] for r in c.execute("pragma table_info(users)")}
    for uid, pid, name, admin in ((1, "1001", "Jane Doe", 1), (2, "1002", "Sam", 0)):
        c.execute("update users set plex_user_id=?, plex_username=?, is_admin=?, is_active=1, "
                  "plex_token=NULL, pin_hash=NULL, chat_history=NULL, taste_vectors=NULL, "
                  "last_login=? where id=?", (pid, name, admin, datetime.utcnow(), uid))
        for col, val in (("timezone", "Europe/Berlin"), ("locale", "en"), ("email", None),
                         ("avatar_url", None), ("display_name", name)):
            if col in cols:
                c.execute(f"update users set {col}=? where id=?", (val, uid))  # nosec B608 - col is a literal above


# ── the library ─────────────────────────────────────────────────────────────

def lib(c):
    out = {}
    for key, title, cat in c.execute("select plex_rating_key, title, media_category from enrichment_status "
                                     "where media_category != 'music'"):
        out[title.lower()] = (key, cat)
    tmdb = {}
    for s, a, t in c.execute("select service, arr_id, tmdb_id from arr_enrichment_status"):
        tmdb[f"{s}:{a}"] = t
    return out, tmdb


def profile(key, cat):
    e = sqlite3.connect(f"file:{ENRICH}?mode=ro", uri=True)
    r = e.execute("select response from api_cache where cache_key like ?", (f"%enriched:{cat}:{key}",)).fetchone()
    e.close()
    return json.loads(r[0]) if r else {}


def tmdb_get(path, **params):
    import httpx
    from src.config import settings
    key = settings.TMDB_API_KEY.get_secret_value() if hasattr(settings.TMDB_API_KEY, "get_secret_value") \
        else settings.TMDB_API_KEY
    r = httpx.get(f"https://api.themoviedb.org/3{path}",
                  params={"api_key": key, "language": "en-US", **params}, timeout=20)
    r.raise_for_status()
    return r.json()


_season_cache: dict = {}


def season(tmdb_id, n):
    k = (tmdb_id, n)
    if k not in _season_cache:
        d = tmdb_get(f"/tv/{tmdb_id}/season/{n}")
        _season_cache[k] = [(e["episode_number"], e.get("name") or f"Episode {e['episode_number']}",
                             (e.get("runtime") or 0) * 60_000) for e in d.get("episodes") or []]
    return _season_cache[k]


def movie_runtime(tmdb_id):
    return (tmdb_get(f"/movie/{tmdb_id}").get("runtime") or 110) * 60_000


# ── the plays ───────────────────────────────────────────────────────────────

def utc(local: datetime) -> datetime:
    return local.replace(tzinfo=TZ).astimezone(timezone.utc).replace(tzinfo=None)


def at(day: datetime, hour: float) -> datetime:
    return day.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=hour)


class Plays:
    def __init__(self):
        self.rows = []

    def add(self, user, local_end, **kw):
        if local_end > YEAR_END:
            return
        self.rows.append({"user_id": user, "plex_user_id": "1001" if user == 1 else "1002",
                          "viewed_at": utc(local_end), "completed": 1, "source": None, **kw})


def _find(L, title):
    hit = L.get(title.lower())
    if hit is None:
        MISSING.append(title)
    return hit


def watch_movie(p, user, L, title, local_start, player, part=1.0):
    hit = _find(L, title)
    if not hit:
        return
    key, cat = hit
    prof = profile(key, cat)
    run = movie_runtime(prof["tmdb_id"]) if prof.get("tmdb_id") else 110 * 60_000
    watched = int(run * part)
    p.add(user, local_start + timedelta(milliseconds=watched), plex_item_id=key, title=title,
          media_type="movie", duration_ms=run, view_offset_ms=watched, completed=int(part >= 0.9),
          tmdb_id=prof.get("tmdb_id"), genres=",".join(prof.get("genres") or []), player=player,
          platform="Samsung TV" if player == "Living Room TV" else "macOS")


def watch_series(p, user, L, T, title, seasons, start, per_night, nights_gap, hour, player,
                 stop_after=None, media_type=None, late=False):
    """Episodes in order, ``per_night`` a sitting, a sitting every
    ``nights_gap`` days from ``start``. Returns the last local time."""
    hit = _find(L, title)
    if not hit:
        return None
    key, cat = hit
    prof = profile(key, cat)
    tmdb = T.get(key) or prof.get("tmdb_id")
    eps = []
    for s in seasons:
        eps += [(s, *e) for e in season(tmdb, s)]
    if stop_after:
        eps = eps[:stop_after]
    day, i = start, 0
    t = None
    while i < len(eps):
        t = at(day, hour + R.uniform(-0.4, 0.5))
        for _ in range(per_night if not callable(per_night) else per_night()):
            if i >= len(eps):
                break
            s, e, name, run = eps[i]
            run = run or 45 * 60_000
            t += timedelta(milliseconds=run)
            p.add(user, t, plex_item_id=f"demo:{key}:{s}:{e}", title=name, series_title=title,
                  media_type=media_type or cat, season=s, episode=e, duration_ms=run,
                  view_offset_ms=run, tmdb_id=prof.get("tmdb_id"),
                  genres=",".join(prof.get("genres") or []),
                  player="iPhone" if late else player, platform="iOS" if late else "Samsung TV")
            t += timedelta(minutes=R.randint(1, 6))
            i += 1
        day += timedelta(days=nights_gap if not callable(nights_gap) else nights_gap())
    return t


def music(p, user, artists, days, per_day, hours):
    m = sqlite3.connect(f"file:{MUSIC}?mode=ro", uri=True)
    tracks = {}
    for a in artists:
        rows = m.execute("select t.plex_rating_key, t.title, t.album, t.duration_ms from track_lyrics t "
                         "join plex_artists p on p.artist_key=t.artist_key where p.name=? and t.duration_ms>60000 "
                         "and coalesce(t.gone,0)=0", (a,)).fetchall()
        if rows:
            tracks[a] = rows
        else:
            MISSING.append(f"{a} (music)")
    m.close()
    names = list(tracks)
    if not names:
        return
    weights = [len(names) - i for i in range(len(names))]   # the first artists most
    for day in days:
        for _ in range(per_day()):
            a = R.choices(names, weights)[0]
            rk, title, album, dur = R.choice(tracks[a])
            t = at(day, R.choice(hours) + R.uniform(0, 1.5))
            p.add(user, t, plex_item_id=rk, title=title, series_title=a, album=album, media_type="music",
                  duration_ms=dur, view_offset_ms=dur, player="Plexamp", platform="iOS")


def jane(p, L, T):
    films = [  # (title, month, day, hour, part)
        ("The Shining", 1, 9, 21.0, 1), ("Network", 1, 17, 20.5, 1), ("The Witch", 1, 30, 21.5, 1),
        ("Parasite", 2, 14, 20.0, 1), ("Under the Skin", 2, 27, 22.0, 1),
        ("Dr. Strangelove or: How I Learned to Stop Worrying and Love the Bomb", 3, 8, 20.5, 1),
        ("Get Out", 3, 21, 21.0, 1), ("The Lobster", 4, 4, 21.0, 1), ("Ex Machina", 4, 18, 20.5, 1),
        ("Rosemary's Baby", 5, 2, 21.0, 1), ("Triangle of Sadness", 5, 23, 20.0, 1), ("It Follows", 6, 6, 22.0, 1),
        ("The Big Short", 6, 20, 20.0, 1), ("Annihilation", 7, 4, 21.5, 1), ("Don't Look Up", 7, 18, 20.5, 0.35),
        ("The Menu", 8, 1, 21.0, 1), ("Hereditary", 8, 15, 22.0, 1), ("Knives Out", 8, 29, 20.0, 1),
        ("Glass Onion: A Knives Out Mystery", 9, 5, 20.0, 1), ("The Wailing", 9, 12, 21.0, 1),
        ("Us", 9, 19, 21.5, 1), ("Midsommar", 9, 26, 20.5, 1), ("The Lighthouse", 10, 3, 22.0, 1),
        ("Perfect Blue", 10, 10, 23.0, 1), ("Talk to Me", 10, 17, 22.5, 1), ("Pearl", 10, 22, 22.0, 1),
        ("The Shining", 10, 24, 21.0, 1), ("Saltburn", 4, 25, 21.0, 0.4), ("Nope", 6, 27, 21.0, 1),
    ]
    for title, mo, d, h, part in films:
        watch_movie(p, 1, L, title, at(datetime(2026, mo, d), h), "Living Room TV", part)
    TV = "Living Room TV"
    watch_series(p, 1, L, T, "Chernobyl", [1], datetime(2026, 1, 3), 2, 2, 21.0, TV)
    watch_series(p, 1, L, T, "Severance", [1, 2], datetime(2026, 2, 6), lambda: R.choice([3, 4, 5]), 1, 20.5, TV)
    watch_series(p, 1, L, T, "The Big Bang Theory", [1], datetime(2026, 2, 21), 3, 1, 19.5, TV, stop_after=3)
    watch_series(p, 1, L, T, "Succession", [1, 2], datetime(2026, 3, 10), 2, lambda: R.choice([1, 2, 3]), 21.0, TV)
    watch_series(p, 1, L, T, "The White Lotus", [1], datetime(2026, 5, 9), 2, 2, 21.0, TV)
    watch_series(p, 1, L, T, "Fargo", [1], datetime(2026, 6, 10), 2, 2, 21.5, TV)
    watch_series(p, 1, L, T, "Mindhunter", [1, 2], datetime(2026, 7, 8), lambda: R.choice([2, 3]), 2, 21.0, TV)
    watch_series(p, 1, L, T, "Dark", [1, 2, 3], datetime(2026, 8, 3), lambda: R.choice([3, 4, 6]),
                 lambda: R.choice([1, 2]), 20.5, TV)
    watch_series(p, 1, L, T, "Black Mirror", [3, 4], datetime(2026, 4, 2), 1, 9, 22.0, TV, stop_after=9)
    watch_series(p, 1, L, T, "Midnight Mass", [1], datetime(2026, 10, 12), 2, 1, 22.0, TV)
    watch_series(p, 1, L, T, "Paranoia Agent", [1], datetime(2026, 9, 28), 3, 2, 23.6, TV, late=True)
    watch_series(p, 1, L, T, "Serial Experiments Lain", [1], datetime(2026, 10, 14), 4, 2, 0.4, TV, late=True)
    watch_series(p, 1, L, T, "Monster", [1], datetime(2026, 6, 1), 2, 3, 22.5, TV, stop_after=24)
    days = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(298)]
    music(p, 1, ["Portishead", "Massive Attack", "Radiohead", "Björk", "Beach House", "Kate Bush", "The Cure",
                 "Depeche Mode", "Mazzy Star", "Siouxsie and the Banshees", "Lana Del Rey", "Ólafur Arnalds",
                 "Nine Inch Nails", "Billie Eilish", "Florence + the Machine", "Daughter"],
          days, lambda: R.choice([0, 4, 7, 9, 12]), [7.5, 8.0, 12.5, 17.5, 18.0, 23.0])


def sam(p, L, T):
    TV = "Living Room TV"
    watch_series(p, 2, L, T, "The Big Bang Theory", [1, 2, 3, 4, 5, 6], datetime(2026, 1, 4),
                 lambda: R.choice([2, 3, 3, 4]), lambda: R.choice([1, 1, 2]), 19.0, TV)
    watch_series(p, 2, L, T, "The Big Bang Theory", [1], datetime(2026, 9, 6), lambda: R.choice([4, 5]), 1, 18.5, TV)
    watch_series(p, 2, L, T, "Young Sheldon", [1, 2], datetime(2026, 7, 20), 3, 2, 19.0, TV)
    watch_series(p, 2, L, T, "Ted Lasso", [1, 2, 3], datetime(2026, 4, 10), 3, 2, 20.0, TV)
    watch_series(p, 2, L, T, "The Office (US)", [2, 3], datetime(2026, 2, 1), 2, 3, 18.0, TV)
    watch_series(p, 2, L, T, "How I Met Your Mother", [1], datetime(2026, 10, 1), 3, 1, 19.5, TV)
    watch_movie(p, 2, L, "Knives Out", at(datetime(2026, 8, 29), 20.0), TV)
    watch_movie(p, 2, L, "Glass Onion: A Knives Out Mystery", at(datetime(2026, 9, 5), 20.0), TV)
    days = [datetime(2026, 1, 1) + timedelta(days=i) for i in range(298)]
    music(p, 2, ["Taylor Swift", "Ed Sheeran", "Coldplay", "The Weeknd"], days,
          lambda: R.choice([0, 0, 3, 5]), [8.0, 17.0])


def main():
    if not (ROOT / ".curatarr-demo").exists():
        sys.exit("seed.py runs only inside a demo copy (scripts/demo/build_demo.py makes one)")
    from src.database.connection import init_db
    init_db()                   # the copy's schema up to this code (new columns)
    c = sqlite3.connect(DB)
    print("scrubbing")
    scrub(c)
    c.commit()
    L, T = lib(c)
    p = Plays()
    print("seeding Jane")
    jane(p, L, T)
    print("seeding Sam")
    sam(p, L, T)
    cols = sorted({k for r in p.rows for k in r})
    c.executemany(f"INSERT INTO watch_history ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",  # nosec B608 - column names from the rows built above
                  [tuple(r.get(k) for k in cols) for r in p.rows])
    c.commit()
    for uid, mt, n in c.execute("select user_id, media_type, count(*) from watch_history group by 1, 2"):
        print(f"  user {uid} {mt}: {n}")
    if MISSING:
        print("  not in this library (skipped): " + ", ".join(MISSING))
    c.execute("VACUUM")
    c.close()


if __name__ == "__main__":
    main()
