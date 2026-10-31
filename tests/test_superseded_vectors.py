"""Superseded memories and principles (2026-10 lifecycle audit).

Superseded memories are kept for good (the audit trail), and every new
memory's conflict scan loaded and JSON-parsed ALL of them, vectors included.
The scan now leaves them out in SQL — robust to malformed metadata, which a
bare json_extract would choke on — and the weekly decay drops the vectors
(~15 KB each) of memories superseded over 30 days ago and of superseded
principles; the text stays.

    python tests/test_superseded_vectors.py
"""
import json
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker

import src.services.episodic_memory as em
from src.database.models import Base, CuratorPrinciple, EpisodicMemory

from _checks import check  # noqa: E402


from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
        s.commit()
    finally:
        s.close()


em.get_db_session = fake_db_session

NOW = datetime.utcnow()
EMB = json.dumps([1.0, 0.0, 0.0])
long_ago = (NOW - timedelta(days=45)).isoformat()
lately = (NOW - timedelta(days=5)).isoformat()
metas = {
    1: None,                                                    # live, no metadata
    2: json.dumps({"title": "Dune"}),                           # live
    3: json.dumps({"superseded_by": 9, "superseded_at": long_ago}),
    4: json.dumps({"superseded_by": 9, "superseded_at": lately}),
    5: "{not json",                                             # malformed: live
    6: json.dumps({"superseded_by": 0}),                        # falsy: live
    7: json.dumps({"superseded_by": None}),                     # null: live
}
with fake_db_session() as db:
    for mid, meta in metas.items():
        db.add(EpisodicMemory(id=mid, user_id=1, memory_type="explicit_statement",
                              content=f"m{mid}", metadata_json=meta, embedding_json=EMB,
                              created_at=NOW - timedelta(days=60)))
    db.add(EpisodicMemory(id=100, user_id=1, memory_type="explicit_statement",
                          content="new", metadata_json=None, embedding_json=EMB,
                          created_at=NOW))
    db.add(CuratorPrinciple(id=1, user_id=1, text="old rule", status="superseded",
                            embedding_json=EMB))
    db.add(CuratorPrinciple(id=2, user_id=1, text="live rule", status="active",
                            embedding_json=EMB))

# ── SQL twin agrees with the Python check ────────────────────────────────────
with fake_db_session() as db:
    live_sql = {r.id for r in db.query(EpisodicMemory.id).filter(
        EpisodicMemory.id != 100, em.not_superseded_sql(EpisodicMemory.metadata_json))}
live_py = {mid for mid, meta in metas.items() if not em._is_superseded(meta)}
check("not_superseded_sql matches _is_superseded on every shape (incl. malformed)",
      live_sql == live_py == {1, 2, 5, 6, 7})

# ── the conflict scan ────────────────────────────────────────────────────────
targets = {t[0] for t in em._conflict_targets(1, 100, "")}
check("general conflict scan never offers a superseded memory", not targets & {3, 4})
check("... and still sees every live one with a matching vector",
      targets == {1, 2, 5, 6, 7})
check("title-bearing scan works without loading vectors",
      [t[0] for t in em._conflict_targets(1, 100, "Dune")] == [2])

# ── vectors dropped past the horizon ─────────────────────────────────────────
out = em.strip_superseded_vectors(now=NOW)
with fake_db_session() as db:
    emb = {m.id: m.embedding_json for m in db.query(EpisodicMemory)}
    content = {m.id: m.content for m in db.query(EpisodicMemory)}
    pemb = {p.id: p.embedding_json for p in db.query(CuratorPrinciple)}
check("a memory superseded 45 days ago loses its vector", emb[3] is None)
check("... and keeps its text (the audit trail)", content[3] == "m3")
check("a memory superseded 5 days ago keeps its vector", emb[4] == EMB)
check("live memories (malformed metadata included) are untouched",
      all(emb[i] == EMB for i in (1, 2, 5, 6, 7, 100)))
check("a superseded principle loses its vector, an active one keeps it",
      pemb[1] is None and pemb[2] == EMB)
check("counts reported", out == {"memories": 1, "principles": 1})
check("idempotent", em.strip_superseded_vectors(now=NOW) == {"memories": 0, "principles": 0})

sched_src = (Path(__file__).resolve().parents[1] / "src/services/scheduler.py").read_text(encoding="utf-8")
check("the weekly memory decay runs the strip",
      "strip_superseded_vectors" in sched_src[sched_src.index("async def job_memory_decay"):
                                              sched_src.index("async def job_orphan_check")])

check.done()
