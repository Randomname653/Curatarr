"""Corpus repair: zombie-doc walk in the audit + deterministic rebuild.

The audit button/custodian task only scanned enriched:* cache rows — a
chroma doc whose row expired (deleted media, old epochs) could never
self-heal; the Batman Beyond confabulation survived ~a year that way
(1,251 zombie docs measured 2026-08-18). Now the audit WALKS the docs:
arr-live zombies requeue through the normal pipeline, arr-gone zombies
rebuild deterministically from cached prefetch data (no LLM). Owner
directive: keep the knowledge, fix the assignment — never delete.

    python tests/test_corpus_repair.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── rebuild_doc_from_prefetch unit tests (stubbed IO) ────────────────────────

import src.services.corpus_repair as cr
import src.cache.metadata_cache as mcache
import src.services.embed_service as es
import src.vector_store.chromadb_wrapper as cw


class FakeMC:
    store = {}

    def get_cache(self, key):
        return FakeMC.store.get(key)

    def close(self):
        pass


class FakeCollection:
    def __init__(self):
        self.deleted, self.added = [], []

    def delete(self, ids):
        self.deleted += ids

    def add(self, ids, documents, embeddings, metadatas):
        self.added.append((ids[0], documents[0], metadatas[0]))


class FakeChroma:
    def __init__(self):
        self.collection = FakeCollection()
        self.facet_writes = []


fake_chroma = FakeChroma()
mcache.MetadataCache = FakeMC
cw.get_chroma_db = lambda: fake_chroma


async def _fake_embed(texts, profile=None):
    return [[1.0, 0.0] for _ in texts]
es.embed_documents = _fake_embed

import src.services.facet_index as fi


async def _fake_write_facets(doc_id, title, domain, genres, themes, vec_map=None):
    fake_chroma.facet_writes.append((doc_id, domain, tuple(themes)))
    return len(themes)
fi.write_facets = _fake_write_facets

# 1) The Batman shape: clean tmdb prefetch, poisoned genre scrubbed.
FakeMC.store["raw_prefetch:sonarr:629"] = {"response": {
    "title": "Batman Beyond", "year": 1999, "media_type": "tv",
    "overview": "In a cyberpunk future Gotham, teenager Terry McGinnis becomes the new Batman under an aging Bruce Wayne.",
    "keywords": ["cyberpunk", "teen superhero", "dystopia"],
    "genres": ["Animation", "Action & Adventure", "Hentai"], "source": "tmdb"}}
ok = asyncio.run(cr.rebuild_doc_from_prefetch("sonarr:629", "tv"))
_id, doc, meta = fake_chroma.collection.added[-1]
check("rebuild writes the doc from prefetch data", ok and _id == "sonarr:629"
      and "Terry McGinnis" in doc)
check("adult genre scrubbed, tv normalized to show",
      "Hentai" not in meta["genres"] and meta["domain"] == "show")
check("delete-before-add (true upsert) + facets refreshed",
      fake_chroma.collection.deleted == ["sonarr:629"]
      and fake_chroma.facet_writes[-1][0] == "sonarr:629")

# 2) No prefetch → untouched (knowledge preserved, no fabrication).
before = len(fake_chroma.collection.added)
ok = asyncio.run(cr.rebuild_doc_from_prefetch("sonarr:999", "show"))
check("no prefetch data -> False, doc untouched",
      ok is False and len(fake_chroma.collection.added) == before)

# 3) Thin overview → refuse.
FakeMC.store["raw_prefetch:radarr:1"] = {"response": {
    "title": "X", "overview": "short", "genres": []}}
check("thin overview -> refuse rebuild",
      asyncio.run(cr.rebuild_doc_from_prefetch("radarr:1", "movie")) is False)

# ── wiring: the audit walks docs ─────────────────────────────────────────────

en = (Path(__file__).resolve().parents[1] / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("audit has the zombie walk (docs without live profile rows)",
      "ZOMBIE DOCS" in en and "rebuild_doc_from_prefetch" in en)
check("arr-live zombies requeue via EnrichmentStatus + ArrEnrichmentStatus",
      "plex_rating_key.in_(requeue_ids)" in en)
check("rebuilds are capped per run (audit drains across runs)",
      "_REBUILD_CAP = 150" in en)
check("no ARR ground truth -> walk skipped, never misclassified",
      "no ARR ground truth" in en)
check("dry_run counts zombies without acting",
      '"zombies":    zstats' in en and "if not dry_run:" in en)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
