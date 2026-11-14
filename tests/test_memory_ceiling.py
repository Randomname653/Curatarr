"""Memory ceilings: the import footprint and the core operations the RAM
audit (2026-11-14) slimmed must not grow back.

Measured then on the owner's data copy: ``import src.main`` 88 MB; one facet
query pinned 1.2 GB of HNSW index for the life of the Chroma client; the
taste calibration corpus peaked at +856 MB over four domains (float64
copies). The checks below hold the line with numbers that do not depend on
this machine's data: an import RSS ceiling plus "these heavy modules stay
out of the web process", tracemalloc peaks relative to the input size, and
the idle release of Chroma's indexes.

    python tests/test_memory_ceiling.py
"""
from _harness import ROOT, app_db, check  # noqa: I001 - first: temp stores

import json
import os
import subprocess
import sys
import tracemalloc

import numpy as np

app_db()   # the embedding profile lives in app_state
MB = 2 ** 20

# ── 1. the web process's import footprint ──────────────────────────────────
# Fresh interpreter, so nothing this suite imported counts. 88 MB measured on
# Windows, CI's Linux lands near it; the ceiling leaves room for growth but
# not for a top-level chromadb (+50 MB) or a model runtime.
IMPORT_CEILING_MB = 140
HEAVY = ("numpy", "chromadb", "onnxruntime", "PIL", "pystray", "torch",
         "sentence_transformers", "scipy", "pandas", "sklearn")
probe = (
    "import json, sys, psutil\n"
    f"sys.path.insert(0, {str(ROOT)!r})\n"
    "import src.main\n"
    f"heavy = [m for m in {HEAVY!r} if m in sys.modules]\n"
    "print(json.dumps({'rss': psutil.Process().memory_info().rss, 'heavy': heavy}))\n"
)
out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                     env=dict(os.environ), cwd=str(ROOT), timeout=180)
line = next((ln for ln in out.stdout.splitlines() if ln.startswith("{")), None)
check("import src.main runs in a fresh interpreter", line is not None, out.stderr[-400:])
if line:
    got = json.loads(line)
    rss_mb = got["rss"] / MB
    check(f"import src.main stays under {IMPORT_CEILING_MB} MB RSS",
          rss_mb < IMPORT_CEILING_MB, f"{rss_mb:.0f} MB")
    check("no heavy library is imported by the web process at startup",
          got["heavy"] == [], got["heavy"])

# ── 2. taste corpus: float32, one copy ────────────────────────────────────
from src.services.taste_engine import _clean_unit_rows  # noqa: E402

rng = np.random.default_rng(7)
corpus = rng.standard_normal((20000, 768)).astype(np.float32)
corpus[3] = np.nan          # a NaN row
corpus[5] = 0.0             # a zero row
corpus.setflags(write=False)   # the memo hands out read-only matrices
size = corpus.nbytes
tracemalloc.start()
unit = _clean_unit_rows(corpus)
_, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
check("clean corpus stays float32", unit is not None and unit.dtype == np.float32,
      None if unit is None else unit.dtype)
check("NaN and zero rows are dropped, the rest unit length",
      unit is not None and len(unit) == 19998
      and bool(np.allclose(np.linalg.norm(unit, axis=1), 1.0, atol=1e-4)))
check("the read-only memo matrix is left untouched",
      bool(np.isnan(corpus[3]).all()) and not corpus.flags.writeable)
# one output copy plus small per-row vectors; the float64 version was ~3x
check("cleaning peaks at ~one copy of the corpus (was three float64 copies)",
      peak < size * 1.3, f"peak {peak / MB:.0f} MB for a {size / MB:.0f} MB corpus")

# ── 3. Chroma lets go of its indexes when idle ─────────────────────────────
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

w = cw.get_chroma_db()
w.collection.upsert(ids=["a", "b"], embeddings=[[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                    metadatas=[{"title": "A"}, {"title": "B"}], documents=["a", "b"])
w.collection.query(query_embeddings=[[1.0, 0.0, 0.0]], n_results=1)
check("a recent use is not released", cw.release_if_idle() is False)
check("an idle store releases its indexes", cw.release_if_idle(idle_s=0) is True)
check("nothing used since the release: nothing to do", cw.release_if_idle(idle_s=0) is False)
res = w.collection.query(query_embeddings=[[0.0, 1.0, 0.0]], n_results=1)
check("the store answers after a release (index reloads from disk)",
      res["ids"] == [["b"]], res["ids"])
check("a query after the release marks the store as used again",
      cw.release_if_idle(idle_s=0) is True)

# The phrase index (1.2 GB on the NAS) has its own clock: the enrichment
# page polls an id list every ~45 s, and with one clock the big index was
# never freed while that page stayed open (NAS, 2026-11-14).
w._phrases_collection()                      # a facet operation loads it
cw._idle_state["phr_last"] -= 3600           # ...an hour ago
w.all_ids()                                  # the status poll, just now
check("an idle phrase index is released while the small collection is still polled",
      cw.release_if_idle(idle_s=600) is True)
w.all_ids()
check("a store kept busy by status polls alone is not swapped again and again",
      cw.release_if_idle(idle_s=600) is False)
w._phrases_collection()
check("a recent facet operation keeps the phrase index",
      cw.release_if_idle(idle_s=600) is False)

# the corpus is read in pages (Chroma decodes a page as Python floats first)
class _Paged:
    calls: list = []

    def get(self, where=None, include=None, limit=None, offset=0):
        _Paged.calls.append(limit)
        n = max(0, min(limit, 4500 - offset))
        return {"embeddings": np.full((n, 4), offset, np.float32), "metadatas": [{}] * n}


pw = cw.ChromaDBWrapper.__new__(cw.ChromaDBWrapper)
pw.collection, pw.collection_name = _Paged(), "paged"
mat = pw.embeddings_for_domain("anime")
check("a domain corpus comes in pages no bigger than _DOMAIN_EMB_PAGE",
      max(_Paged.calls) <= cw._DOMAIN_EMB_PAGE and len(_Paged.calls) == 3, _Paged.calls)
check("the pages join into one float32 matrix in order",
      mat.shape == (4500, 4) and mat.dtype == np.float32
      and mat[0, 0] == 0 and mat[-1, 0] == 2 * cw._DOMAIN_EMB_PAGE)

# expired corpus memos go even while the store stays busy
cw._domain_emb_memo[("x", "music", 1)] = (0, -1e9, np.zeros((2, 2), np.float32))
cw._touch()
cw.release_if_idle()
check("an expired corpus memo is dropped without waiting for the next rebuild",
      ("x", "music", 1) not in cw._domain_emb_memo)

# ── 4. reclaim never fails a job ───────────────────────────────────────────
from src.services.memory_reclaim import reclaim_memory  # noqa: E402

check("reclaim_memory returns a bool on every platform",
      isinstance(reclaim_memory(), bool))

# ── 5. slim per-item structures held across a whole walk ───────────────────
from src.services.lyrics import _lyric_stream, _slim_track  # noqa: E402
from src.services.media_server.base import ExternalIds, MediaItem  # noqa: E402

full = {"ratingKey": "7", "title": "Song", "grandparentTitle": "Band", "duration": 1000,
        "Genre": [{"tag": "Rock"}], "Guid": [{"id": "mbid://x"}], "thumb": "/t",
        "Media": [{"bitrate": 900, "Part": [{"size": 42, "file": "/m/a.flac", "Stream": [
            {"streamType": 2, "codec": "flac", "bitDepth": 24},
            {"streamType": 4, "key": "/library/streams/9", "codec": "lrc"}]}]}]}
slim = _slim_track(full)
check("a slimmed track keeps its lyric stream", _lyric_stream(slim) == _lyric_stream(full)
      == ("/library/streams/9", "lrc"))
check("a slimmed track keeps the part size and the listing fields",
      slim["Media"][0]["Part"][0]["size"] == 42 and slim["title"] == "Song"
      and slim["duration"] == 1000)
check("a slimmed track drops what the upsert never reads",
      "Genre" not in slim and "Guid" not in slim and "thumb" not in slim
      and len(slim["Media"][0]["Part"][0]["Stream"]) == 1)

mi = MediaItem(key="1", kind="episode", title="x")
check("MediaItem carries no per-instance __dict__ (one per library item in a walk)",
      not hasattr(mi, "__dict__"))
check("ExternalIds.any still sees a set id", ExternalIds(imdb_id="tt1").any()
      and not ExternalIds().any())

check.done()
