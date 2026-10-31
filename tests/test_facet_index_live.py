"""Facet index against a REAL Chroma store.

tests/test_facet_index.py drives write_facets / facet_probe through a
47-line FakeChroma that re-implements the wrapper's facet API (replace,
query, phrase reuse, count). That checks the call pattern, and the probe's
aggregation on hand-made distances — but not that the real wrapper does what
the fake assumes. Here only the embedder (the Ollama boundary) is faked:
the facet collection, its ids and metadata, the in-place replace, the
phrase reuse, the domain filter and the cosine distances are Chroma's own.

    python tests/test_facet_index_live.py
"""
from _harness import check  # noqa: I001 - first: temp stores, never the live data/

import asyncio

from src.services import embed_service  # noqa: E402
embed_service._profile_cache["value"] = {
    "model": "test-embed", "collection": "facets_live", "prefixes": False}

import src.services.facet_index as fi  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

# The ONLY fake: one axis per phrase, so a query for a phrase finds exactly it.
AXES = ["death game", "body horror", "heist", "found family"]
embedded: list = []


async def _embed(texts, profile=None):
    embedded.extend(texts)
    return [[1.0 if a == t else 0.0 for a in AXES] for t in texts]

embed_service.embed_documents = _embed


def vec(phrase):
    return [1.0 if a == phrase else 0.0 for a in AXES]


db = cw.get_chroma_db()


def ids_of(parent):
    got = db.facets_page(0, 100, embeddings=False)
    return sorted(i for i, m in zip(got.get("ids") or [], got.get("metadatas") or [])
                  if (m or {}).get("parent") == parent)


def write(doc_id, title, domain, themes):
    return asyncio.run(fi.write_facets(doc_id, title, domain, "", themes))


# ── 1. writes land as real points with the probe's metadata ─────────────────
check("starts empty", db.facets_count() == 0)
n1 = write("sonarr:1", "Gleipnir", "anime", ["death game", "body horror"])
n2 = write("radarr:2", "Heat", "movie", ["death game", "heist"])
check("one point per phrase", (n1, n2) == (2, 2) and db.facets_count() == 4, db.facets_count())
check("ids follow parent::fN", ids_of("sonarr:1") == ["sonarr:1::f0", "sonarr:1::f1"])
meta = (db.facets_by_ids(["sonarr:1::f0"]).get("metadatas") or [{}])[0]
check("metadata carries parent, title, domain and the emb_model stamp",
      meta.get("parent") == "sonarr:1" and meta.get("title") == "Gleipnir"
      and meta.get("domain") == "anime" and meta.get("emb_model") == "test-embed", meta)
check("a phrase the index already holds is not embedded again",
      embedded.count("death game") == 1, embedded)

# ── 2. the probe reads real cosine distances, filtered by domain ────────────
probe = asyncio.run(fi.facet_probe([vec("death game"), vec("heist")]))
check("a phrase query finds both titles that carry it, at similarity ~1",
      probe["sonarr:1"]["sims"][0] > 0.99 and probe["radarr:2"]["sims"][0] > 0.99, probe)
check("a title whose different facets hit different constraints counts both",
      probe["radarr:2"]["hits"] == 2 and probe["sonarr:1"]["hits"] == 1
      and probe["radarr:2"]["phrases"] == {0: "death game", 1: "heist"})
anime = asyncio.run(fi.facet_probe([vec("death game")], domain="anime"))
check("the domain filter is Chroma's where clause: a movie never reaches an anime probe",
      set(anime) == {"sonarr:1"}, set(anime))

# ── 3. a rewrite replaces in place; empty themes clear ──────────────────────
write("sonarr:1", "Gleipnir", "anime", ["found family"])
check("a rewrite with fewer phrases leaves no stale point behind",
      ids_of("sonarr:1") == ["sonarr:1::f0"] and db.facets_count() == 3, ids_of("sonarr:1"))
again = asyncio.run(fi.facet_probe([vec("body horror")], domain="anime"))
check("...and the old phrase no longer finds the title",
      "sonarr:1" not in again or again["sonarr:1"]["sims"].get(0, 0) < 0.5, again)
check("an empty theme set clears the title", write("radarr:2", "Heat", "movie", []) == 0
      and ids_of("radarr:2") == [] and db.facets_count() == 1)

check.done()
