"""Episodic memory hygiene: contradictions supersede instead of deleting, a
weaker remark never erases a standing preference, retrieval counts only what
it serves and boosts by relevance, and keep-considerations match whole words
and never generalise from a lone genre word.

Found in the 2026-10 vector/memory audit:
  * CONTRADICTION hard-deleted the old memory whatever its weight — a 0.4
    "something light tonight" could erase a 1.0 "loves dark thrillers".
  * every top-k memory was counted as accessed even when the relevance
    threshold hid it, and the access boost (+0.2) ignored relevance, so
    often-retrieved memories surfaced on habit alone.
  * keyword tests were substrings ("whatever" vetoed as "hate"), and one
    shared genre word fired the lexical path for a whole genre.

    python tests/test_memory_hygiene.py
"""
import asyncio
import json
import math
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.connection as conn  # noqa: E402
import src.services.episodic_memory as em  # noqa: E402
from src.database.models import Base, EpisodicMemory  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1, 100)
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


conn.get_db_session = _session
conn.write_session = _session
em.get_db_session = _session

OLD = datetime.utcnow() - timedelta(days=3)
_uid = [100]


def add(content, emb, importance=0.5, memory_type="explicit_statement", meta=None,
        access=0, created=OLD, user_id=None):
    with _session() as db:
        m = EpisodicMemory(user_id=user_id or _uid[0], memory_type=memory_type,
                           content=content, metadata_json=json.dumps(meta or {}),
                           embedding_json=json.dumps(emb), importance=importance,
                           access_count=access, created_at=created, last_accessed=created)
        db.add(m)
        db.flush()
        return m.id


def row(mid):
    with _session() as db:
        m = db.query(EpisodicMemory).filter(EpisodicMemory.id == mid).first()
        return None if m is None else (m.importance, json.loads(m.metadata_json or "{}"),
                                       m.access_count)


def next_user():
    """A fresh household member per scenario (a real row: foreign keys are on)."""
    _uid[0] += 1
    with_users(engine, _uid[0])
    return _uid[0]


# ── conflict resolution with a fake summarizer ───────────────────────────────

prompts: list = []
VERDICT = {"status": "CONTRADICTION"}


class _Resp:
    status_code = 200

    def json(self):
        return {"message": {"content": json.dumps(dict(VERDICT, reason="test"))}}


class _Client:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, json=None):
        prompts.append(json["messages"][0]["content"])
        return _Resp()


em.httpx.AsyncClient = _Client

uid = next_user()
standing = add("The user loves dark psychological thrillers.", [1.0, 0.0, 0.0], importance=1.0)
mood = add("The user wants something light tonight.", [0.95, 0.1, 0.0], importance=0.4,
           memory_type="conversation", created=datetime.utcnow())
asyncio.run(em.resolve_memory_conflicts(uid, mood, "The user wants something light tonight.", {}))
imp, meta, _ = row(standing)
check("a weaker remark never erases a standing preference (was: deleted)",
      imp is not None and "superseded_by" not in meta)
check("…it is weighed as nuance instead (importance eased by 0.1)", abs(imp - 0.9) < 1e-9)
check("a title-less conflict is framed as general preferences, not title ''",
      "the user's general preferences" in prompts[-1] and "title ''" not in prompts[-1])
check("the prompt tells the model a passing mood is UNRELATED",
      "situational or momentary" in prompts[-1])

uid = next_user()
old = add("The user loves horror films.", [1.0, 0.0, 0.0], importance=0.5)
new = add("The user now dislikes horror films.", [0.97, 0.05, 0.0], importance=0.9,
          created=datetime.utcnow())
asyncio.run(em.resolve_memory_conflicts(uid, new, "The user now dislikes horror films.", {}))
imp, meta, _ = row(old)
check("a real contradiction keeps the old memory (recoverable)", imp is not None)
check("…marked superseded by the new one",
      meta.get("superseded_by") == new and meta.get("superseded_at"))
check("…and dropped below retrieval weight", imp == em._SUPERSEDED_IMPORTANCE)

# a superseded memory is never a conflict target again
later = add("The user really dislikes horror now.", [0.98, 0.02, 0.0], importance=0.9,
            created=datetime.utcnow())
check("superseded memories are not re-litigated",
      old not in [t[0] for t in em._conflict_targets(uid, later, "")])

# ── retrieve_memories: relevance-gated boost, honest access counting ─────────


async def _embed(text, kind="document"):
    return [1.0, 0.0, 0.0]


em._embed = _embed

uid = next_user()
relevant = add("The user loves slow cinema.", [1.0, 0.0, 0.0], importance=0.7)
habit = add("The user mentioned a D&D night once.", [0.0, 1.0, 0.0], importance=1.0, access=50)
gone = add("The user loved slow cinema long ago.", [1.0, 0.0, 0.0], importance=1.0, access=50,
           meta={"superseded_by": relevant})
got = [m["id"] for m in asyncio.run(em.retrieve_memories(uid, "slow cinema"))]
check("a relevant memory is served", relevant in got)
check("an irrelevant memory no longer rides a flat access boost (was +0.2)", habit not in got)
check("a superseded memory is never served", gone not in got)
check("only served memories are counted as accessed",
      row(relevant)[2] == 1 and row(habit)[2] == 50)

# ── keep-considerations: whole words, no lone-genre generalisation ───────────

check("'whatever' is not a 'hate' memory",
      em._CONSIDERATION_NEGATIVE_RE.search("keeps whatever the partner picks") is None)
check("'hated' still is", em._CONSIDERATION_NEGATIVE_RE.search("the user hated it"))
check("'glove' is not 'love'", em._CONSIDERATION_POSITIVE_RE.search("a glove box") is None)
check("stems still match their words",
      em._CONSIDERATION_POSITIVE_RE.search("deeply valued, nostalgic"))

uid = next_user()
# cos to the query [1,0,0]: 0.6 for the three keep memories, 0 for the fillers
# → mean 0.36, threshold 0.65: none is an embedding standout, so only the
# lexical path can fire.
SIM = [0.6, 0.8, 0.0]
partner = add("The user keeps whatever their partner enjoys watching.", SIM)
genre_only = add("The user values dark comedy as comfort viewing.", SIM)
two_genres = add("The user values horror comedy nights.", SIM)
add("Filler note about the weather.", [0.0, 1.0, 0.0])
add("Another filler about the router.", [0.0, 1.0, 0.0])
add("The user keeps every partner pick.", SIM, meta={"superseded_by": 1})
cons = asyncio.run(em.retrieve_considerations(
    uid, "Some Film — Horror, Comedy — a night in with a partner", media_category="movie",
    top_k=5))
ids = {c["id"] for c in cons}
check("a distinctive shared word fires (and 'whatever' no longer vetoes it)", partner in ids)
check("a lone shared GENRE word no longer generalises a memory", genre_only not in ids)
check("two shared genre words still count", two_genres in ids)
check("a superseded memory is never a consideration",
      all("every partner pick" not in c["content"] for c in cons))

# ── keep-considerations: the embedding path has an absolute floor ────────────
# 2026-10-21: a K.I.T.T. nostalgia memory matched Cat's Eyes at cos 0.27. It
# stood out of a tight cluster of unrelated memories, but it was noise.


def at(c):
    return [c, math.sqrt(1 - c * c), 0.0]


PROFILE = "Cafe Story — Comedy — three sisters run a cafe"
NOSTALGIA = "The user enjoyed the nostalgia of a talking car."

uid = next_user()
noise = add(NOSTALGIA, at(0.30))
for _ in range(5):
    add("Filler note about the weather.", at(0.15))
cons = asyncio.run(em.retrieve_considerations(uid, PROFILE, media_category="show", top_k=3))
check("a keep memory that only stands out of a tight cluster is noise (cos 0.30, relative bar 0.26)",
      noise not in {c["id"] for c in cons})

uid = next_user()
real = add(NOSTALGIA, at(0.40))
for _ in range(5):
    add("Filler note about the weather.", at(0.15))
cons = asyncio.run(em.retrieve_considerations(uid, PROFILE, media_category="show", top_k=3))
check("a real embedding match above the floor still counts", real in {c["id"] for c in cons})
check("the floor sits above the measured noise (p99 0.335) and below real matches (0.35+)",
      0.335 < em.CONSIDERATION_EMBED_FLOOR < 0.35)

check.done()
