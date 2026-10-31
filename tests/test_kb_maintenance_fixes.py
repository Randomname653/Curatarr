"""Knowledge Base maintenance fixes (2026-10-26).

- Lyrics profiles ask Ollama for their JSON shape: the summariser bake
  (whose system prompt lists six modes) opened with "the requested mode
  LYRICS PROFILE is not among the six defined task modes" and wrote prose;
  79 of 166 artists failed on the NAS, 0 of 12 with the schema in a replay.
- The anime card counts what carries an AniList id - the enriched profiles
  and the pins - not the mapping file's AniList column, which nothing fills.

    python tests/test_kb_maintenance_fixes.py
"""
import asyncio
import json
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _checks import check  # noqa: E402


# ── lyrics: the request carries the schema ──────────────────────────────────
import src.services.lyrics as ly  # noqa: E402

sent = []


class _Resp:
    status_code = 200

    def json(self):
        return {"message": {"content": json.dumps({
            "lyrical_profile": "Songs about leaving.", "themes": ["leaving", "home"], "languages": ["English"],
            "explicit": False, "explicit_note": "", "motifs": [], "tone": [], "quotes": []})}}


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        sent.append(json)
        return _Resp()


ly.ollama_client = lambda timeout=None: _Client()
ly.ollama_for = lambda role: "http://ollama"
got = asyncio.run(ly._call_summarizer("prompt"))
fmt = (sent[0] or {}).get("format") or {}
check("the lyrics call asks for its JSON shape (Ollama structured output)",
      fmt.get("type") == "object" and set(fmt.get("required") or []) >= {"lyrical_profile", "themes", "quotes"})
check("...and its answer still parses", got and got["themes"] == ["leaving", "home"])

wiz = (ROOT / "src/services/setup_wizard.py").read_text(encoding="utf-8")
check("the summariser bake no longer insists on six modes (takes effect on the next build)",
      "You have SIX distinct task modes" not in wiz and "never comment on the mode" in wiz)
sch = ly._PROFILE_SCHEMA["properties"]
check("the schema carries the checks the cleaner makes (2+ themes, a real profile)",
      sch["themes"]["minItems"] == 2 and sch["lyrical_profile"]["minLength"] >= 20)

# ── lyrics: no guessing from almost nothing, no copied examples ─────────────
check("a lyric's real words leave out [tags], (asides) and 'Instrumental'",
      ly.real_words("[Verse 1]\nRiver runs cold (yeah)") == 3 and ly.real_words("[Instrumental]") == 0
      and ly.real_words("Instrumental") == 0)
check("the prompt shows no example themes for the model to copy",
      "leaving a small town" not in ly.LYRICS_PROFILE_PROMPT and "grief turned into anger" not in ly.LYRICS_PROFILE_PROMPT)
check("a profile that copied a former example is made again",
      ly._copied_example({"profile": {"themes": ["Leaving a small town", "rivers"]}})
      and not ly._copied_example({"profile": {"themes": ["anger after a loss"]}}))
tmp = Path(tempfile.mkdtemp()) / "lyrics.db"
con = ly._connect(tmp)
for i, text in enumerate(["[Instrumental]", "Hey hey hey", "Instrumental"]):
    con.execute("INSERT INTO track_lyrics (plex_rating_key, artist_key, album_key, artist, album, title, text, lines,"
                " checked_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (f"k{i}", "a1", "al", "Artist", "Album", f"T{i}", text, 1, "2026-10-26T00:00:00"))
con.commit()
con.close()
block, shown, used = ly._profile_input("a1", tmp, listens={})
check("an artist whose texts hold no real lyrics is not shown to the model",
      shown == 0 and block == "" and used == [])
src = (ROOT / "src/services/lyrics.py").read_text(encoding="utf-8")
check("...and is counted as without real lyrics, not as a failed profile",
      "no_lyrics += 1" in src and "without real lyrics" in src)

# ── the anime card ───────────────────────────────────────────────────────────
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.connection as conn  # noqa: E402
from src.database.models import ArrEnrichmentStatus as AES, Base, MediaMatchOverride as MO  # noqa: E402
import src.cache.metadata_cache as mc  # noqa: E402
from src.services import anime_mapping as am  # noqa: E402

from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
        db.commit()          # the contract of get_db_session: commit on a clean exit
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


conn.get_db_session = fake_session
db = S()
for a in (1, 2, 3, 4):
    db.add(AES(service="sonarr", arr_id=a, category="anime", title=f"A{a}"))
db.add(AES(service="sonarr", arr_id=9, category="show", title="S"))
db.add(MO(service="sonarr", arr_id=1, category="anime", anilist_id=10, note="auto: TVDB -> AniDB -> AniList crossref"))
db.add(MO(service="sonarr", arr_id=2, category="anime", anilist_id=20, note="pinned by the owner"))
db.commit()
db.close()


class _Cache:
    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_cache_many(self, keys):
        rows = {"enriched:anime:sonarr:1": {"anilist_id": 10}, "enriched:anime:sonarr:2": {"anilist_id": 20},
                "enriched:anime:sonarr:3": {"anilist_id": None}}
        return {k: {"response": rows[k]} for k in keys if k in rows}


mc.MetadataCache = _Cache
cov = am.anilist_coverage()
check("the card counts the library's anime whose profile names an AniList id, and the pins",
      cov == {"anime": 4, "with_anilist": 2, "pinned": 2, "auto_pinned": 1})
kb = (ROOT / "frontend/js/kb.js").read_text(encoding="utf-8")
check("the card no longer shows the mapping file's empty AniList column",
      "AniList pending" not in kb and "library anime with an AniList id" in kb)

check.done()
