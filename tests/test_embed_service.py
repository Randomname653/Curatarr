"""Tests for the central embed service + migration plumbing (eval package 2).

    python tests/test_embed_service.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.app_state as app_state
import src.services.embed_service as es

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── profile: default, flip, cache invalidation ───────────────────────────────

_state = {}
app_state.get_state = lambda key, default=None: _state.get(key, default)
app_state.set_state = lambda key, value: _state.__setitem__(key, value)
es._profile_cache["value"] = None

p = es.get_profile()
# Ballast cleanup 2026-08-18: a profile-less boot defaults to the V2 stack
# (a legacy default would CREATE an empty retired-v1 corpus on fresh installs).
check("default profile = v2 stack (prefixes, media_knowledge_v2)",
      p["prefixes"] is True and p["collection"] == "media_knowledge_v2")

from src.services.embedding_migration import V2_PROFILE

es.set_profile(V2_PROFILE)
p2 = es.get_profile()
check("set_profile flips atomically (model+prefixes+collection together)",
      p2["model"] == "nomic-embed-text-v2-moe" and p2["prefixes"] is True
      and p2["collection"] == "media_knowledge_v2" and p2["num_ctx"] == 512)

es._profile_cache["value"] = None
_state.clear()
check("cleared state falls back to the v2 default",
      es.get_profile()["collection"] == "media_knowledge_v2")

# ── prefixes, normalization, batching (faked transport) ──────────────────────

captured = []


async def fake_post(model, inputs, num_ctx):
    captured.append({"model": model, "inputs": list(inputs), "num_ctx": num_ctx})
    return [[3.0, 4.0] for _ in inputs]


es._post_embed = fake_post

legacy = {"model": "m1", "prefixes": False, "num_ctx": 8192}
v2 = {"model": "m2", "prefixes": True, "num_ctx": 512}

d = asyncio.run(es.embed_documents(["hello"], profile=legacy))
check("legacy: no prefix", captured[-1]["inputs"] == ["hello"])
check("vectors unit-normalized on receipt", d[0] == [0.6, 0.8])

asyncio.run(es.embed_documents(["hello"], profile=v2))
check("v2 doc prefix applied", captured[-1]["inputs"] == ["search_document: hello"])

asyncio.run(es.embed_query("find it", profile=v2))
check("v2 query prefix applied", captured[-1]["inputs"] == ["search_query: find it"])
check("num_ctx follows the profile", captured[-1]["num_ctx"] == 512)

captured.clear()
asyncio.run(es.embed_documents([f"t{i}" for i in range(70)], profile=legacy))
check("70 texts chunked into 3 batches (32/32/6)",
      [len(c["inputs"]) for c in captured] == [32, 32, 6])


async def fail_post(model, inputs, num_ctx):
    return []


es._post_embed = fail_post
d = asyncio.run(es.embed_documents(["a", "b"], profile=legacy))
check("transport failure -> per-text None, no raise", d == [None, None])

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
src_dir = root / "src"
offenders = [p.name for p in src_dir.rglob("*.py")
             if p.name != "embed_service.py"
             and '/api/embeddings"' in p.read_text(encoding="utf-8")]
check("deprecated /api/embeddings endpoint gone from all callsites",
      offenders == [])

te = (root / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("taste rebuild prefers the stored index vector (template unification)",
      "_chroma_item_vector(entry, profile)" in te)
check("embed cache is model-scoped (flip can't serve old-space vectors)",
      'f"emb:{_emb_model}:{pid}"' in te)
check("skip_summaries rebuild path exists",
      "skip_summaries: bool = False" in te)

cw = (root / "src/vector_store/chromadb_wrapper.py").read_text(encoding="utf-8")
check("chroma wrapper is profile-aware with singleton refresh",
      "refresh_singleton" in cw and "get_profile" in cw)

em = (root / "src/services/embedding_migration.py").read_text(encoding="utf-8")
check("migration has an eval gate and never flips on failure",
      "_EVAL_GATE = 0.8" in em and '"eval_gate"' in em
      and "set_profile(V2_PROFILE)" in em)
check("DB vector spaces (memories + principles) migrate too",
      "EpisodicMemory" in em and "CuratorPrinciple" in em)

en = (root / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("migration + rollback endpoints registered",
      '"/embedding-migration"' in en and '"/embedding-migration/rollback"' in en)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
