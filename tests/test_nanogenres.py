"""Nanogenres: titles whose themes sit together, named once (2026-10-25).

Measured on the real store: clustering the facet PHRASES gave broad themes
with music and video mixed; clustering TITLES (each the mean of its own
phrases, video and music apart) gave nanogenres (coming-of-age teen
comedies, serial-killer thrillers, german street rap), 340 MB peak.

Fakes only: a scripted facet index, an in-memory database, no model.

    python tests/test_nanogenres.py
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, Nanogenre, TitleNanogenre, User, WatchHistoryEntry as W  # noqa: E402
import src.services.nanogenres as ng  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── the clustering ──────────────────────────────────────────────────────────
rng = np.random.default_rng(3)
blobs = [np.eye(8)[i] for i in (0, 3, 6)]
X = np.stack([b + rng.normal(0, 0.05, 8) for b in blobs for _ in range(20)]).astype(np.float32)
X /= np.linalg.norm(X, axis=1, keepdims=True)
C1, lab1 = ng.kmeans(X, 3)
C2, lab2 = ng.kmeans(X, 3)
check("three clear groups come out as three clusters", len(set(lab1[:20])) == len(set(lab1[20:40])) == 1
      and len(set(lab1)) == 3)
check("the same input gives the same clusters (a fixed seed)", (lab1 == lab2).all() and np.allclose(C1, C2))
check("about one cluster per 80 titles, within bounds",
      ng.k_for(10987) == 137 and ng.k_for(100) == ng._K_MIN and ng.k_for(10 ** 6) == ng._K_MAX)
check("video and music apart; an unknown domain nowhere",
      ng.group_of("anime") == "video" and ng.group_of("music") == "music" and ng.group_of("book") is None)


# ── a scripted facet index ──────────────────────────────────────────────────
def unit(i, noise=0.0):
    v = np.eye(16)[i] + noise
    return list(v / np.linalg.norm(v))


class FakeFacets:
    def __init__(self, points):
        self.points = points        # [(id, doc, metadata, embedding)]
        self.by_ids_calls = 0

    def facets_page(self, offset, limit, embeddings=True):
        page = self.points[offset:offset + limit]
        out = {"ids": [p[0] for p in page], "documents": [p[1] for p in page],
               "metadatas": [p[2] for p in page]}
        if embeddings:
            out["embeddings"] = [p[3] for p in page]
        return out

    def facets_by_ids(self, ids):
        self.by_ids_calls += 1
        want = set(ids)
        page = [p for p in self.points if p[0] in want]
        return {"ids": [p[0] for p in page], "documents": [p[1] for p in page],
                "metadatas": [p[2] for p in page], "embeddings": [p[3] for p in page]}


def title(parent, name, domain, axis, genres, phrases=("theme a", "theme b"), model="emb-a"):
    return [(f"{parent}::f{i}", ph, {"parent": parent, "title": name, "domain": domain,
                                      "genres": genres, "emb_model": model}, unit(axis, 0.01 * i))
            for i, ph in enumerate(phrases)]


points = []
for n in range(10):
    points += title(f"m{n}", f"Teen Comedy {n}", "movie", 0, "Comedy, Romance", ("first love", "high school"))
    points += title(f"t{n}", f"Killer {n}", "show", 5, "Crime, Thriller", ("serial killer", "detective"))
    points += title(f"a{n}", f"Rapper {n}", "music", 9, "Hip Hop, Rap", ("street life", "german rap"))
facets = FakeFacets(points)
ng._PAGE = 7                     # several pages
ng._K_MIN = 2
ng._PER_CLUSTER = 5

X, meta, model = ng.read_titles(facets)
check("one vector per title, from all its phrases across pages",
      len(meta) == 30 and X.shape == (30, 16) and model == "emb-a"
      and sorted(meta[0]["phrases"]) == ["first love", "high school"])
plan = ng.build(facets)
video = plan["groups"]["video"]
check("a build clusters video and music apart",
      set(plan["groups"]) == {"video", "music"} and sum(c["size"] for c in video) == 20
      and sum(c["size"] for c in plan["groups"]["music"]) == 10)
teen = next(c for c in video if any("Teen" in t for t in c["top_titles"]))
check("a cluster keeps its central titles, commonest phrases and genres",
      all("Teen" in t for t in teen["top_titles"]) and "first love" in teen["top_phrases"]
      and teen["top_genres"][:2] == ["comedy", "romance"])

# ── stored, and kept current ────────────────────────────────────────────────
engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
        db.commit()
    finally:
        db.close()


ng.get_db_session = fake_session
import src.vector_store.chromadb_wrapper as cw  # noqa: E402


async def direct(fn, *a, **k):
    return fn(*a, **k)

cw.get_chroma_db = lambda: facets
cw.run_chroma = direct
asyncio.run(ng.run_nanogenres())
db = S()
n_genres, n_titles = db.query(Nanogenre).count(), db.query(TitleNanogenre).count()
db.close()
check("the first run builds and stores every title's nanogenre", n_genres >= 3 and n_titles == 30)

points += title("m99", "Teen Comedy New", "movie", 0, "Comedy", ("prom night",))
facets.points = points
facets.by_ids_calls = 0
asyncio.run(ng.run_nanogenres())
db = S()
new = db.query(TitleNanogenre).filter_by(parent="m99").one()
teen_id = db.query(TitleNanogenre).filter_by(parent="m0").one().nanogenre_id
ids_before = sorted(i for (i,) in db.query(Nanogenre.id))
db.close()
check("a later run only assigns the new title, to the nearest centre, reading just its points",
      new.nanogenre_id == teen_id and facets.by_ids_calls == 1 and n_genres == len(ids_before))

for p in points:
    p[2]["emb_model"] = "emb-b"
asyncio.run(ng.run_nanogenres())
db = S()
ids_after = sorted(i for (i,) in db.query(Nanogenre.id))
model_now = db.query(Nanogenre.embed_model).first()[0]
db.close()
check("a new embedding model builds the clusters again", ids_after != ids_before and model_now == "emb-b")

# ── naming ──────────────────────────────────────────────────────────────────
check("a name is a short lowercase phrase; lists and essays are refused",
      ng.clean_name('"Coming_of_Age Teen Comedy"') == "coming of age teen comedy"
      and ng.clean_name("") is None and ng.clean_name("a " * 30) is None)
asked = []


async def fake_ask(c, model=None):
    asked.append(c)
    return "teen comedy" if "Teen" in " ".join(c["titles"]) else None

ng._ask_name = fake_ask
done = asyncio.run(ng.run_nanogenre_names())
db = S()
named = {n.name for n in db.query(Nanogenre).filter(Nanogenre.name.isnot(None))}
db.close()
check("the summarizer is asked with titles, genres and phrases; an unanswered one waits",
      named == {"teen comedy"} and done is False and all("genres" in c for c in asked))

# ── the Wrap-Up's hours per nanogenre ───────────────────────────────────────
from src.services import wrapup_sections as ws  # noqa: E402
from sqlalchemy import func  # noqa: E402
from src.services.wrapup_facts import Period  # noqa: E402

db = S()
db.add(User(id=1, plex_user_id="1", plex_username="o", is_admin=True))
for tn in db.query(TitleNanogenre):
    nid = tn.nanogenre_id
    db.query(Nanogenre).filter_by(id=nid).update(
        {Nanogenre.name: func.coalesce(Nanogenre.name, f"genre {nid}")}, synchronize_session=False)
for i, (t, mt, mins, when) in enumerate([
        ("Teen Comedy 1", "movie", 120, datetime(2026, 10, 10)),
        ("Teen Comedy 2", "movie", 100, datetime(2026, 10, 12)),
        ("Rapper 3", "music", 60, datetime(2026, 10, 11)),
        ("Not In The Index", "movie", 30, datetime(2026, 10, 11)),
        ("Rapper 3", "music", 4, datetime(2026, 1, 5)),
        ("Teen Comedy 1", "movie", 100, datetime(2026, 10, 20))]):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"k{i}", title=t,
             series_title=t if mt == "music" else None, media_type=mt, viewed_at=when,
             duration_ms=mins * 60_000, completed=True))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="mk", title="Killer 1", series_title="Killer 1",
         media_type="show", viewed_at=datetime(2026, 10, 15), duration_ms=3_600_000, completed=True,
         source="marked"))
db.commit()
period = Period("30d", datetime(2026, 10, 1), datetime(2026, 10, 31), "Last 30 days", False)
out = ws.nanogenres(db, W, 1, period, ["movie", "show", "music"])
db.close()
check("hours per nanogenre, the biggest first, with its titles; a mark of 'played' counts nothing",
      out["top"][0]["name"] == "teen comedy" and out["top"][0]["hours"] == 5.3
      and out["top"][0]["titles"][0] == "Teen Comedy 1" and len(out["top"]) == 2)
check("...the share is of the hours that have a nanogenre; coverage says how many",
      abs(sum(t["share"] for t in out["top"]) - 1) < 0.01 and out["coverage"] == round(380 / 410, 3))
check("...and a nanogenre never played before the period is new to you",
      out["new_to_you"] == ["teen comedy"])

from src.services.data_custodian import _registry  # noqa: E402

jobs = {t.job_id: t for t in _registry()}
check("the build runs without a model, the names on the summarizer after it",
      not jobs["nanogenres"].needs_llm and jobs["nanogenre_names"].needs_llm
      and jobs["nanogenre_names"].llm_role == "summarizer" and "nanogenres" in jobs["nanogenre_names"].after)
facts_src = (ROOT / "src/services/wrapup_facts.py").read_text(encoding="utf-8")
from src.services import wrapup_facts as wf  # noqa: E402
check("the facts carry it, a new generation",
      'out["nanogenres"] = _sections.nanogenres(db, W, user_id, period, types)' in facts_src
      and wf.FACTS_VERSION >= 9)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
