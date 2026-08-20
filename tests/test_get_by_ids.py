"""Bulk embedding fetch (adapted from Jules PR #14 — N+1 fix).

``get_by_ids`` replaces per-item ``get_by_id`` calls inside the
thousands-wide deletion-scoring / arr-ranking loops with one Chroma
``.get`` per 1000-id chunk. The adaptation drops the PR's duplicated
skip-filter logic (desync trap) and adds a per-id fallback on chunk
failure so the corruption-tell counter stays per-item.

    python tests/test_get_by_ids.py
"""
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


from src.vector_store.chromadb_wrapper import ChromaDBWrapper


class FakeCollection:
    """Serves ids a-e; raises on bulk calls when ``fail_bulk`` is set."""

    def __init__(self, fail_bulk=False, corrupt_ids=()):
        self.known = {f"id-{c}": [float(ord(c))] * 3 for c in "abcde"}
        self.fail_bulk = fail_bulk
        self.corrupt_ids = set(corrupt_ids)
        self.calls = []

    def get(self, ids, include):
        self.calls.append((list(ids), list(include)))
        if self.fail_bulk and len(ids) > 1:
            raise RuntimeError("HNSW segment error")
        if any(i in self.corrupt_ids for i in ids):
            raise RuntimeError("corrupt id")
        found = [i for i in ids if i in self.known]
        out = {"ids": found, "embeddings": [self.known[i] for i in found]}
        if "documents" in include:
            out["documents"] = [None] * len(found)
        if "metadatas" in include:
            out["metadatas"] = [None] * len(found)
        return out


def make_wrapper(collection):
    w = ChromaDBWrapper.__new__(ChromaDBWrapper)
    w.collection = collection
    return w


# ── happy path ───────────────────────────────────────────────────────────
col = FakeCollection()
w = make_wrapper(col)
res = w.get_by_ids(["id-a", "id-b", "id-zz"])
check("found ids map to their embedding",
      res["id-a"]["embedding"] == [97.0] * 3 and res["id-b"]["embedding"] == [98.0] * 3)
check("unknown ids are absent, not None-valued", "id-zz" not in res)
check("single bulk call, embeddings-only include",
      len(col.calls) == 1 and col.calls[0][1] == ["embeddings"])

# ── chunking + dedup ─────────────────────────────────────────────────────
col = FakeCollection()
w = make_wrapper(col)
many = [f"x{i}" for i in range(2500)] + ["x0", "x1"]  # 2500 unique + 2 dupes
w.get_by_ids(many)
check("2500 unique ids -> 3 chunks of 1000/1000/500 (dupes collapsed)",
      [len(c[0]) for c in col.calls] == [1000, 1000, 500])

# ── empty input ──────────────────────────────────────────────────────────
check("empty input -> empty map, zero calls",
      make_wrapper(FakeCollection()).get_by_ids([]) == {})

# ── chunk failure -> per-id fallback recovers the healthy ids ────────────
col = FakeCollection(fail_bulk=True, corrupt_ids={"id-c"})
w = make_wrapper(col)
res = w.get_by_ids(["id-a", "id-c", "id-e"])
check("bulk failure: healthy ids recovered via per-id fallback",
      res.get("id-a", {}).get("embedding") == [97.0] * 3
      and res.get("id-e", {}).get("embedding") == [101.0] * 3)
check("corrupt id stays absent instead of poisoning the batch", "id-c" not in res)
check("fallback really went per-id (1 bulk + 3 singles)",
      len(col.calls) == 4 and all(len(c[0]) == 1 for c in col.calls[1:]))

# ── call sites actually use the prefetch (no per-item get_by_id left) ────
eng = (Path(__file__).resolve().parents[1]
       / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("recommendations_engine: both loops read the prefetched map",
      eng.count("_prefetched_embeddings.get(doc_id)") == 2
      and "chroma_db.get_by_id(" not in eng)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
