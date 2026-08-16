"""Facet index (multi-vector items, stage 1) — writer/probe/backfill tests.

    python tests/test_facet_index.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.facet_index as fi

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── stubs ────────────────────────────────────────────────────────────────────

class FakeChroma:
    def __init__(self):
        self.deleted = []
        self.added = []          # (ids, metadatas, documents)
        self.count = 1000
        self.pages = {}          # offset -> {"ids": [...], "metadatas": [...]}
        self.page_error_at = None
        self.query_result = {"ids": [[]], "documents": [[]],
                             "metadatas": [[]], "distances": [[]]}

    def facets_delete_by_parent(self, parent):
        self.deleted.append(parent)
        return True

    def facets_add(self, documents, embeddings, metadatas, ids):
        self.added.append((ids, metadatas, documents))
        return True

    def facets_query(self, query_embeddings, n_results=20, where=None):
        return self.query_result

    def facets_count(self):
        return sum(len(a[0]) for a in self.added)

    def get_count(self):
        return self.count

    @property
    def collection(self):
        fake = self

        class _C:
            def get(self, include=None, limit=500, offset=0):
                if fake.page_error_at is not None and offset >= fake.page_error_at:
                    raise RuntimeError("boom")
                return fake.pages.get(offset, {"ids": [], "metadatas": []})
        return _C()


fake = FakeChroma()
import src.vector_store.chromadb_wrapper as cw
cw.get_chroma_db = lambda: fake

import src.services.embed_service as es
async def _fake_embed_docs(texts, profile=None):
    return [[1.0, 0.0] for _ in texts]
es.embed_documents = _fake_embed_docs

STATE = {}
import src.services.app_state as ast
ast.get_state = lambda k: STATE.get(k)
ast.set_state = lambda k, v: STATE.__setitem__(k, v)

# ── write_facets ─────────────────────────────────────────────────────────────

n = asyncio.run(fi.write_facets("sonarr:1", "Gleipnir", "anime", "Action",
                                ["death game", "femdom", "  ", "x"]))
check("writes one point per usable phrase (short/blank dropped)", n == 2)
check("delete-by-parent runs BEFORE add (true upsert)",
      fake.deleted == ["sonarr:1"] and len(fake.added) == 1)
ids, metas, docs = fake.added[0]
check("facet id schema parent::fN",
      ids == ["sonarr:1::f0", "sonarr:1::f1"])
check("metadata carries parent/domain/genres for the probe gate",
      metas[0] == {"parent": "sonarr:1", "title": "Gleipnir",
                   "domain": "anime", "genres": "Action", "facet": "theme"})

n = asyncio.run(fi.write_facets("sonarr:1", "Gleipnir", "anime", "", []))
check("empty theme set still clears stale facets, writes none",
      n == 0 and fake.deleted[-1] == "sonarr:1" and len(fake.added) == 1)

check("comma-string themes accepted (metadata form, U+2011 normalized)",
      fi._phrases_from_themes("death‑game, body horror") ==
      ["death-game", "body horror"])

# ── facet_probe aggregation ──────────────────────────────────────────────────

fake.query_result = {
    "ids": [["a::f0", "b::f0", "a::f1"]],
    "documents": [["cute pastel town", "psych warfare", "sugar visuals"]],
    "metadatas": [[{"parent": "a", "title": "A", "genres": "Comedy"},
                   {"parent": "b", "title": "B", "genres": "Thriller"},
                   {"parent": "a", "title": "A", "genres": "Comedy"}]],
    "distances": [[0.2, 0.3, 0.1]],
}
probe = asyncio.run(fi.facet_probe([[1, 0], [0, 1]], "anime"))
check("probe aggregates per parent with max sim",
      probe["a"]["sims"][0] == 0.9 and probe["b"]["sims"][0] == 0.7)
check("probe records best phrase per constraint",
      probe["a"]["phrases"][0] == "sugar visuals")
check("hits counts distinct constraints hit (both probes same result here)",
      probe["a"]["hits"] == 2 and probe["b"]["hits"] == 2)

# ── backfill: cursor resume + done flag + page-error retry ───────────────────

STATE.clear()
fake.deleted, fake.added = [], []
fake.count = 3
fake.pages = {
    0: {"ids": ["x1", "x2"],
        "metadatas": [{"title": "T1", "domain": "anime", "genres": "",
                       "themes": "alpha, beta"},
                      {"title": "T2", "domain": "anime", "genres": "",
                       "themes": ""}]},
    2: {"ids": ["x3"],
        "metadatas": [{"title": "T3", "domain": "movie", "genres": "",
                       "themes": "gamma"}]},
}
done = asyncio.run(fi.run_facet_backfill())
check("backfill completes small corpus in one run and stamps done",
      done is True and STATE.get(fi._BACKFILL_DONE_KEY) == "1")
check("backfill wrote facets for themed titles only",
      sorted(d for d, *_ in [(a[0][0].split("::")[0],) for a in fake.added])
      == ["x1", "x3"])
check("done flag short-circuits the next run",
      asyncio.run(fi.run_facet_backfill()) is True)

STATE.clear()
fake.page_error_at = 0
check("page read failure returns False (retry next tick), cursor kept",
      asyncio.run(fi.run_facet_backfill()) is False
      and STATE.get(fi._BACKFILL_CURSOR_KEY) is None)
fake.page_error_at = None

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
