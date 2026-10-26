"""Switching an install between Plex and Jellyfin (docs/JELLYFIN_DESIGN.md §8, phase 5).

On a Plex install whose tables carry Plex keys (and *arr ids, Spotify URIs,
Netflix refs, ext-script keys and a TMDB-only doc id beside them):

- the dry run matches every title (external ids, the file below the library
  root, the double episode Jellyfin keeps as one item), the users by name,
  the libraries by kind and name - and changes NOTHING;
- an unmatched user blocks --write unless skipped;
- --write backs the database up, re-keys every server-key column, leaves
  every other value alone (a TMDB doc id that looks like a Plex key
  included), re-keys open deletion proposals with a deep link, copies the
  vectors and moves the facets, drops the app state naming old server
  objects, files both sides and the users in the id memory, flips the
  setting, writes a completed manifest; an unfinished manifest blocks the
  next run;
- --undo puts every value back exactly (the double episode's two rows too),
  vectors under their old ids with no twins left, the setting back;
- Plex -> Jellyfin -> Plex by two rebinds ends where it began, except what
  Jellyfin itself merged (both halves of a double episode come back as the
  first);
- --sync-state marks finished plays played and Plex ratings >= 8 as likes.

    python tests/test_rebind.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

from datetime import datetime  # noqa: E402

from sqlalchemy import text  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from media_server_fixtures import JellyfinWriteServer, plex_transport  # noqa: E402
from src.services.media_server import rebind  # noqa: E402
from src.services.media_server.jellyfin_adapter import JellyfinAdapter  # noqa: E402
from src.services.media_server.plex_adapter import PlexAdapter  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


JANE, SAM = "b0c1d2e3f405162738495a6b7c8d9e0f", "c1d2e3f405162738495a6b7c8d9e0fa1"
HEAT, HOME = "4b1d2f3e5a6c7b8d9e0f1a2b3c4d5e6f", "5c2e3f4a6b7d8c9e0f1a2b3c4d5e6f70"
DARK, SECRETS, DOUBLE = "9a8b7c6d5e4f30211203f4e5d6c7b8a9", "2a3b4c5d6e7f80910a1b2c3d4e5f6071", "3b4c5d6e7f8091a20b1c2d3e4f506172"
BJORK, HUNTER, JOGA = "6d7e8f90a1b2c3d4e5f60718293a4b5c", "8f90a1b2c3d4e5f60718293a4b5c6d7e", "90a1b2c3d4e5f60718293a4b5c6d7e8f"
LIB_MOV = "f137a2dd21bbc1b99aa5c0f6bf02a805"
T = "2026-10-01 20:00:00"
SEED = """
INSERT INTO users (id, plex_user_id, plex_username, is_admin, is_active) VALUES
  (1, '12345678', 'jane', 1, 1), (2, '87654321', 'Sam', 0, 1);
INSERT INTO library_config (id, plex_section_key, plex_section_title, plex_section_type, media_category) VALUES
  (1, '1', 'Movies', 'movie', 'movie'), (2, '2', 'TV Shows', 'show', 'show'), (3, '3', 'Music', 'artist', 'music');
INSERT INTO watch_history (id, user_id, plex_user_id, plex_item_id, title, media_type, viewed_at, source) VALUES
  (1, 1, '12345678', '1001', 'Heat', 'movie', '{T}', NULL),
  (2, 1, '12345678', '1002', 'Home Movie', 'movie', '{T}', NULL),
  (3, 1, '12345678', '2003', 'Secrets', 'show', '{T}', NULL),
  (4, 1, '12345678', '2004', 'Lies', 'show', '{T}', NULL),
  (5, 1, '12345678', '2005', 'Past and Present', 'show', '{T}', NULL),
  (6, 1, '12345678', '3003', 'Hunter', 'music', '{T}', NULL),
  (7, 1, '12345678', 'spotify:track:4uLU6hMCjMI75M1A2tKUQC', 'Song', 'music', '{T}', 'spotify'),
  (8, 1, '12345678', 'netflix:aaaaaaaaaaaaaaaaaaaaaaaa', 'Elsewhere', 'movie', '{T}', 'netflix'),
  (9, 2, '87654321', '1001', 'Heat', 'movie', '{T}', NULL),
  (10, 1, '1', '3004', 'Jóga', 'music', '{T}', NULL);
INSERT INTO enrichment_status (id, plex_rating_key, title, media_category, enriched) VALUES
  (1, '1001', 'Heat', 'movie', 1), (2, '2001', 'Dark', 'show', 1), (3, 'radarr:12', 'Ronin', 'movie', 1),
  (4, 'ext-script:bjork', 'Björk', 'music', 1);
INSERT INTO media_tech_profiles (id, plex_rating_key, media_type, title) VALUES (1, '1001', 'movie', 'Heat'), (2, '2001', 'show', 'Dark');
INSERT INTO library_items (id, plex_rating_key, media_type, title) VALUES (1, '1001', 'movie', 'Heat');
INSERT INTO cached_recommendations (id, user_id, category, lane, title, plex_rating_key) VALUES (1, 1, 'movie', 'library', 'Heat', '1001');
INSERT INTO swipe_ratings (id, user_id, category, doc_id, seen, want) VALUES
  (1, 1, 'movie', '1001', 1, 0), (2, 1, 'movie', '1002', 1, 0), (3, 1, 'movie', 'radarr:12', 1, 0);
INSERT INTO plex_ratings (id, user_id, plex_item_id, media_type, rating) VALUES (1, 1, '3003', 'track', 10.0);
INSERT INTO deletion_proposals (id, user_id, media_id, title, service, status, arr_url) VALUES
  (1, 1, '3001', 'Björk', 'plex', 'pending', 'http://plex/old'), (2, 1, '3001', 'Björk', 'plex', 'deleted', 'http://plex/old'),
  (3, 1, '12', 'Heat', 'radarr', 'pending', 'http://radarr/12');
INSERT INTO app_state (key, value) VALUES ('rec_playlist_push:user_id=1', '{{"movie": {{}}}}'), ('keep_me', 'x');
""".replace("{T}", T)
with conn_mod.engine.begin() as c:
    for stmt in [s for s in SEED.split(";") if s.strip()]:
        c.execute(text(stmt))
    c.execute(text("INSERT INTO nanogenres (id, grp, idx, centroid, version, built_at) VALUES (1, 'show', 0, x'00', 1, '2026-10-01')"))
    c.execute(text("INSERT INTO title_nanogenres (id, parent, title, nanogenre_id, assigned_at) VALUES (1, '2001', 'Dark', 1, '2026-10-01')"))


def columns() -> dict:
    out = {}
    with conn_mod.engine.connect() as c:
        for table, col in rebind.ITEM_COLUMNS + rebind.DOC_COLUMNS + rebind.USER_COLUMNS + rebind.LIBRARY_COLUMNS:
            out[f"{table}.{col}"] = sorted(c.execute(text(f"SELECT id, {col} FROM {table}")).fetchall())
        out["proposals"] = sorted(c.execute(text("SELECT id, media_id, service, arr_url FROM deletion_proposals")).fetchall())
        out["app_state"] = sorted(c.execute(text("SELECT key, value FROM app_state WHERE key NOT LIKE 'jellyfin%'")).fetchall())
    return out


class FakeChroma:
    def __init__(self):
        self.docs = {"1001": ("v1", {"title": "Heat"}, "d1"), "2001": ("v2", {"title": "Dark"}, "d2"),
                     "1002": ("v3", {"title": "a TMDB-only doc"}, "d3")}
        self.facets = {"1001": 3, "2001": 2}

    def copy(self, pairs):
        done = [(o, n) for o, n in pairs if o in self.docs]
        for o, n in done:
            self.docs[n] = self.docs[o]
        return done

    def delete(self, ids):
        for i in ids:
            self.docs.pop(i, None)

    def rekey_facets(self, pairs):
        moved = 0
        for o, n in pairs:
            if o in self.facets and n not in self.facets:
                self.facets[n] = self.facets.pop(o)
                moved += 1
        return moved


flips = []
run = asyncio.run
before = columns()
plex = PlexAdapter("http://plex:32400", "plex-token", transport=plex_transport())
SRV = JellyfinWriteServer()
jf = JellyfinAdapter("http://jf:8096", "jf-key", transport=SRV.transport())

print("\n-- the dry run --")
plan = run(rebind.build_plan("plex", "jellyfin", jf, source_adapter=plex))
check("every title matched, the double episode's two Plex episodes onto Jellyfin's one item",
      plan.items == {"1001": HEAT, "1002": HOME, "2003": SECRETS, "2004": DOUBLE, "2005": DOUBLE,
                     "2001": DARK, "3001": BJORK, "3003": HUNTER, "3004": JOGA}
      and not plan.conflicts and not plan.unmatched)
check("by external id, and the id-less film by its file", plan.methods["file"] == 1 and plan.methods["external_id"] == 8)
check("users by name, libraries by kind and name ('TV Shows' -> 'Shows', the only show library)",
      {u: t for u, (_, t, _) in plan.users.items()} == {1: JANE, 2: SAM}
      and {k: v[1] for k, v in plan.libraries.items()} == {"1": "Movies", "2": "Shows", "3": "Music"})
check("the report counts rows per table; a TMDB-only doc id is no server key",
      plan.tables["watch_history.plex_item_id"] == {"matched": 8, "unmatched": 0}
      and plan.tables["swipe_ratings.doc_id"] == {"matched": 1, "unmatched": 0})
check("the dry run changed nothing", columns() == before)

with conn_mod.engine.begin() as c:
    c.execute(text("INSERT INTO users (id, plex_user_id, plex_username, is_admin, is_active) VALUES (3, '555', 'carol', 0, 1)"))
blocked = run(rebind.build_plan("plex", "jellyfin", jf, source_adapter=plex))
check("a user with no match blocks --write", blocked.blocking and "carol" in blocked.blocking[0])
try:
    run(rebind.apply_plan(blocked, target_adapter=jf, chroma=FakeChroma(), backup_root=_TMP / "b0",
                          settings_writer=flips.append))
    refused = False
except RuntimeError:
    refused = True
check("...and apply refuses it", refused and columns()["users.plex_user_id"][-1] == (3, "555"))
plan = run(rebind.build_plan("plex", "jellyfin", jf, source_adapter=plex, skip_users=["Carol"]))
check("--skip-user lets it through, carol stays on her Plex id", not plan.blocking and plan.users_skipped == ["carol"])

print("\n-- --write --")
before = columns()
chroma = FakeChroma()
mpath = run(rebind.apply_plan(plan, target_adapter=jf, chroma=chroma, backup_root=_TMP / "b1",
                              settings_writer=lambda k: flips.append(k) or "plex-was-blank"))
m = json.loads(mpath.read_text(encoding="utf-8"))
after = columns()
wh = dict(after["watch_history.plex_item_id"])
check("watch history re-keyed; Spotify and Netflix rows untouched",
      wh[1] == HEAT and wh[2] == HOME and wh[4] == DOUBLE and wh[5] == DOUBLE and wh[9] == HEAT
      and wh[7].startswith("spotify:") and wh[8].startswith("netflix:"))
check("enrichment, profiles, ledger, recommendations, ratings re-keyed; arr and ext-script keys untouched",
      dict(after["enrichment_status.plex_rating_key"]) == {1: HEAT, 2: DARK, 3: "radarr:12", 4: "ext-script:bjork"}
      and dict(after["media_tech_profiles.plex_rating_key"]) == {1: HEAT, 2: DARK}
      and dict(after["library_items.plex_rating_key"]) == {1: HEAT}
      and dict(after["cached_recommendations.plex_rating_key"]) == {1: HEAT}
      and dict(after["plex_ratings.plex_item_id"]) == {1: HUNTER})
check("doc ids: an enrichment key moves, a TMDB-only id that looks like a Plex key does not",
      dict(after["swipe_ratings.doc_id"]) == {1: HEAT, 2: "1002", 3: "radarr:12"}
      and dict(after["title_nanogenres.parent"])[1] == DARK)
check("users and their plays' account ids; a Plex local account id stays",
      dict(after["users.plex_user_id"]) == {1: JANE, 2: SAM, 3: "555"}
      and dict(after["watch_history.plex_user_id"])[1] == JANE and dict(after["watch_history.plex_user_id"])[10] == "1")
check("libraries re-keyed with their new names",
      dict(after["library_config.plex_section_key"])[1] == LIB_MOV)
props = {p[0]: p for p in after["proposals"]}
check("the open Plex proposal deletes through Jellyfin now, with a Jellyfin link; history and arr ones untouched",
      props[1][1:3] == (BJORK, "jellyfin") and props[1][3].startswith("http://jf:8096/web/#/details?id=")
      and props[2][1:3] == ("3001", "plex") and props[3][1:3] == ("12", "radarr"))
check("app state naming old server objects is dropped, the rest kept",
      after["app_state"] == [("keep_me", "x")])
check("vectors copied under the new ids and the old ones gone; the TMDB-only doc untouched; facets moved",
      set(chroma.docs) == {HEAT, DARK, "1002"} and chroma.facets == {HEAT: 3, DARK: 2})
check("setting flipped, backup kept, manifest completed",
      flips == ["jellyfin"] and Path(m["backup"]).exists() and m["status"] == "completed"
      and m["settings"] == "plex-was-blank")
with conn_mod.engine.connect() as c:
    mem = dict(c.execute(text("SELECT canonical_key, jellyfin_id FROM server_id_map WHERE entity_type='user'")).fetchall())
check("users filed in the id memory", mem == {"user:1": JANE, "user:2": SAM})
(_TMP / "b1" / "rebind-zz").mkdir()
(_TMP / "b1" / "rebind-zz" / "manifest.json").write_text(json.dumps({"status": "running"}))
try:
    run(rebind.apply_plan(plan, target_adapter=jf, chroma=FakeChroma(), backup_root=_TMP / "b1",
                          settings_writer=flips.append))
    second = True
except RuntimeError as e:
    second = "unfinished" not in str(e)
check("an unfinished manifest blocks the next run", not second)
(_TMP / "b1" / "rebind-zz" / "manifest.json").unlink()

print("\n-- --undo --")
with conn_mod.engine.begin() as c:   # a play recorded on Jellyfin after the switch
    c.execute(text(f"INSERT INTO watch_history (id, user_id, plex_user_id, plex_item_id, title, media_type, viewed_at, source) "
                   f"VALUES (11, 1, '{JANE}', '{HEAT}', 'Heat', 'movie', '2026-10-27 21:00:00', 'jellyfin_session')"))
u = run(rebind.undo(mpath, chroma=chroma, settings_writer=flips.append))
back = columns()
wh_back = dict(back["watch_history.plex_item_id"])
check("every value is back, the double episode's two rows each to its own Plex episode",
      {k: v for k, v in back.items() if k != "watch_history.plex_item_id" and k != "watch_history.plex_user_id"}
      == {k: v for k, v in before.items() if k != "watch_history.plex_item_id" and k != "watch_history.plex_user_id"}
      and wh_back[4] == "2004" and wh_back[5] == "2005" and wh_back[1] == "1001")
check("...and the play recorded after the switch comes along", wh_back[11] == "1001"
      and dict(back["watch_history.plex_user_id"])[11] == "12345678")
check("vectors back under the old ids, no twins left; facets back; setting back",
      set(chroma.docs) == {"1001", "2001", "1002"} and chroma.facets == {"1001": 3, "2001": 2}
      and flips[-1] == "plex" and json.loads(mpath.read_text())["status"] == "undone")
check("undoing twice does nothing", run(rebind.undo(mpath, chroma=chroma, settings_writer=flips.append)) == {"skipped": "already undone"})

print("\n-- there and back again --")
with conn_mod.engine.begin() as c:
    c.execute(text("DELETE FROM watch_history WHERE id = 11"))
start = columns()
chroma = FakeChroma()
plan = run(rebind.build_plan("plex", "jellyfin", jf, source_adapter=plex, skip_users=["carol"]))
run(rebind.apply_plan(plan, target_adapter=jf, chroma=chroma, backup_root=_TMP / "b2", settings_writer=flips.append))
back_plan = run(rebind.build_plan("jellyfin", "plex", plex, source_adapter=jf, skip_users=["carol"]))
check("the way back matches every Jellyfin key (the double episode to its first Plex episode), "
      "users by the plex.tv ids the id memory kept",
      back_plan.items.get(DOUBLE) == "2004" and back_plan.items.get(HEAT) == "1001" and not back_plan.unmatched
      and not back_plan.blocking
      and {u: (t, h) for u, (_, t, h) in back_plan.users.items()} == {1: ("12345678", "id_memory"), 2: ("87654321", "id_memory")})
run(rebind.apply_plan(back_plan, target_adapter=plex, chroma=chroma, backup_root=_TMP / "b3", settings_writer=flips.append))
end = columns()
merged_row = dict(end["watch_history.plex_item_id"])[5]
end_wh = dict(end["watch_history.plex_item_id"])
end_wh[5] = "2005"
check("Plex -> Jellyfin -> Plex ends where it began, except what Jellyfin merged (row 5 on 2004)",
      merged_row == "2004" and sorted(end_wh.items()) == start["watch_history.plex_item_id"]
      and {k: v for k, v in end.items() if k not in ("watch_history.plex_item_id", "proposals", "app_state")}
      == {k: v for k, v in start.items() if k not in ("watch_history.plex_item_id", "proposals", "app_state")})
check("the proposal came back to Plex too", dict((p[0], p[1:3]) for p in end["proposals"])[1] == ("3001", "plex"))
check("vectors under the Plex ids again", set(chroma.docs) == {"1001", "2001", "1002"})

print("\n-- --sync-state --")
st = run(rebind.sync_watched_state(plan, jf))
check("towards Jellyfin: finished plays marked, Plex 10/10 liked",
      "marked_played" in st and st["failed"] == 0)
check("towards Plex it says why it does not", run(rebind.sync_watched_state(back_plan, plex)) == {"skipped": "only towards Jellyfin"})

print("\n-- towards Plex: plex.tv accounts --")
acc = run(plex.account_users())
check("the plex.tv list: the owner and the Home user / friend, by plex.tv id (not /accounts' local ids)",
      [(u.key, u.name, u.is_admin) for u in acc] == [("12345678", "jane", True), ("87654321", "Sam", False)])

print("\n-- the CLI --")
cli = (ROOT / "scripts/rebind_server.py").read_text(encoding="utf-8")
check("dry run by default, --write / --undo / --map-user / --skip-user / --accept-suggestions / --sync-state",
      all(f in cli for f in ("--write", "--undo", "--map-user", "--skip-user", "--accept-suggestions", "--sync-state"))
      and "Dry run - nothing changed" in cli)
check("--write and --undo refuse while Curatarr answers on its port",
      "if (args.write or args.undo) and _server_running():" in cli)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
