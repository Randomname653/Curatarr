"""The semantic-search vector memo is bounded (2026-10 lifecycle audit).

_vec_memo keyed open-vocabulary text (query constraints, LLM theme phrases
on every hit) to ~25 KB 768-float lists and was only ever cleared on an
embedding-model change: months of searching grew it to hundreds of MB. It is
an LRU now, trimmed after each call so every vector the call resolved stays
resident for _evidence_scores.

    python tests/test_vec_memo_bound.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.cache.metadata_cache as mcache
import src.services.embed_service as es
import src.services.semantic_search as ss

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


@contextmanager
def patched(mod, **attrs):
    old = {k: getattr(mod, k) for k in attrs}
    for k, v in attrs.items():
        setattr(mod, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            setattr(mod, k, v)


embeds = []


async def fake_post(model, inputs, num_ctx):
    embeds.extend(inputs)
    return [[1.0, 0.0] for _ in inputs]


class _NoCache:          # no api_cache layer: every memo miss is an embed
    def __init__(self):
        raise RuntimeError("no cache in this test")


def resolve(texts):
    return asyncio.run(ss._texts_to_vectors(texts))


old_cap = ss._VEC_MEMO_CAP
ss._vec_memo.clear()
ss._vec_memo_model["model"] = None
try:
    with patched(es, get_profile=lambda: {"model": "m", "prefixes": False},
                 _post_embed=fake_post), patched(mcache, MetadataCache=_NoCache), \
            patched(ss, _VEC_MEMO_CAP=10):
        for batch in range(5):
            resolve([f"phrase {batch}-{i}" for i in range(6)])
        check("the memo never grows past its cap", len(ss._vec_memo) <= 10)
        check("the latest call's vectors are all resident",
              all(ss._norm_tag(f"phrase 4-{i}") in ss._vec_memo for i in range(6)))
        check("the oldest are the ones evicted",
              ss._norm_tag("phrase 0-0") not in ss._vec_memo)

        # LRU, not FIFO: a hit moves to the back of the line.
        ss._vec_memo.clear()
        resolve([f"a{i}" for i in range(10)])
        embeds.clear()
        resolve(["a0"])                          # a hit: no embed, refreshed
        resolve([f"b{i}" for i in range(9)])     # pushes 9 out
        check("a memo hit costs no embed", "a0" not in embeds)
        check("a recently used vector survives eviction (LRU)",
              ss._norm_tag("a0") in ss._vec_memo
              and ss._norm_tag("a1") not in ss._vec_memo)

        # A call needing more than the cap keeps all of its own vectors.
        ss._vec_memo.clear()
        big = [f"big {i}" for i in range(25)]
        out = resolve(big)
        check("a call larger than the cap keeps every vector it resolved",
              len(out) == 25 and all(ss._norm_tag(t) in ss._vec_memo for t in big))
        resolve(["small"])
        check("... and the next call trims back to the cap", len(ss._vec_memo) <= 10)
finally:
    ss._vec_memo.clear()
    ss._vec_memo_model["model"] = None

check("default cap fits several full searches (40 hits x _TAG_CAP + constraints)",
      old_cap >= 4 * (40 * ss._TAG_CAP + 10))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
