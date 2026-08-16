"""Tests for the semantic-search core (Block 3 + curated rerank v2).

    python tests/test_semantic_search.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.semantic_search as ss
from src.services.semantic_search import format_rag_context

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── format parity with the historical chat injection ─────────────────────────

hits = [
    {"title": "Dark", "genres": "Sci-Fi, Thriller", "themes": "time travel",
     "doc": "A" * 300, "watch_tag": "WATCHED", "size_tag": "[42 GB]"},
    {"title": "Kill la Kill", "genres": "Action", "themes": "",
     "doc": "Wild anime.", "watch_tag": "UNWATCHED", "size_tag": ""},
]
out = format_rag_context(hits).split("\n")
check("line 1 mirrors the historical format",
      out[0] == f"- Dark [WATCHED] [42 GB] (Sci-Fi, Thriller, time travel): {'A' * 200}")
check("doc clamped to 200 chars", ("A" * 200) in out[0] and ("A" * 201) not in out[0])
check("empty themes -> no trailing comma",
      out[1] == "- Kill la Kill [UNWATCHED] (Action): Wild anime.")
check("empty size tag -> no double space", "]  (" not in out[1])
check("empty hit list -> empty string", format_rag_context([]) == "")
check("missing doc tolerated",
      format_rag_context([{"title": "X", "genres": "", "themes": "",
                           "watch_tag": "T", "size_tag": ""}]).endswith(": "))

# ── curated search: stubs ────────────────────────────────────────────────────

# semantic_search binds watched_lookup/watch_tag at import time -> patch there.
ss.watched_lookup = lambda uid, titles, category=None: {}
ss.watch_tag = lambda entry: "NOT watched"

import src.services.size_norms as size_norms
size_norms.short_size_tag = lambda **kw: ""

TITLES = ["Gushing Over Magical Girls", "Nanoha", "Raising Project",
          "Jungle De Ikou", "Irregular Witch", "Madoka"]


class FakeChroma:
    def __init__(self):
        self.last_n = None

    def query(self, query_embeddings=None, n_results=10, where=None):
        self.last_n = n_results
        n = min(n_results, len(TITLES))
        return {
            "ids": [[f"id{i}" for i in range(n)]],
            "documents": [[f"doc {t}" for t in TITLES[:n]]],
            "metadatas": [[{"title": t, "genres": "Mahou Shoujo",
                            "themes": "", "year": 2010 + i}
                           for i, t in enumerate(TITLES[:n])]],
            "distances": [[0.1 * (i + 1) for i in range(n)]],
        }

    def get_by_id(self, doc_id):
        return {"id": doc_id, "embedding": [0.5, 0.5]}


fake_chroma = FakeChroma()
import src.vector_store.chromadb_wrapper as cw
cw.get_chroma_db = lambda: fake_chroma

import src.services.embed_service as es
async def _fake_embed(q):
    return [1.0, 0.0]
es.embed_query = _fake_embed

# Candidate fact lines never hit the real cache in tests.
ss._candidate_lines = lambda hits, domain=None: [
    f"[{i}] {h['title']}" for i, h in enumerate(hits)]


def _queue_summarizer(answers):
    """Stub _summarizer_json with a FIFO of canned answers."""
    q = list(answers)
    async def fake(system, user, schema, num_predict, timeout):
        return q.pop(0) if q else None
    ss._summarizer_json = fake


# ── fallback path: parse fails -> plain vector order, mode 'vector' ──────────

_queue_summarizer([None])
res = asyncio.run(ss.curated_search("magical girls", n_results=4, domain="anime"))
check("parse-fail -> mode vector", res["mode"] == "vector")
check("parse-fail -> vector order kept",
      [h["title"] for h in res["results"]] == TITLES[:4])
check("overfetch requested (3x limit, floor 24)", fake_chroma.last_n == 24)
check("scores kept from distances",
      res["results"][0]["score"] == 0.9 and res["results"][1]["score"] == 0.8)

# ── rerank path: anchor filtered, ranking applied, fit_note carried ──────────

parse_answer = {"anchor_title": "gushing over magical girls",
                "constraints": ["adult cast"],
                "search_text": "dark magical girl adult cast"}
rank_answer = {"ranking": [
    {"i": 0, "fit": 2, "why": "child cast"},        # Nanoha (post-filter idx 0)
    {"i": 1, "fit": 9, "why": "adult ensemble"},    # Raising Project
    {"i": 2, "fit": 1, "why": "pre-teen lead"},     # Jungle De Ikou
    {"i": 3, "fit": 7},                              # Irregular Witch
    {"i": 4, "fit": 5, "why": "teen cast"},          # Madoka
]}
_queue_summarizer([parse_answer, rank_answer])
res = asyncio.run(ss.curated_search(
    "a show like gushing over magical girls but with more adult cast",
    n_results=3, domain="anime"))
check("rerank -> mode reranked", res["mode"] == "reranked")
check("anchor itself filtered out",
      all(h["title"] != "Gushing Over Magical Girls" for h in res["results"]))
check("anchor reported", res["anchor"] == "Gushing Over Magical Girls")
check("ranking order applied (fit desc)",
      [h["title"] for h in res["results"]] ==
      ["Raising Project", "Irregular Witch", "Madoka"])
check("fit_note carried", res["results"][0]["fit_note"] == "adult ensemble")
check("limit applied post-rerank", len(res["results"]) == 3)

# ── rerank fails -> vector order, anchor still filtered ──────────────────────

_queue_summarizer([parse_answer, None])
res = asyncio.run(ss.curated_search(
    "like gushing over magical girls but adult", n_results=4, domain="anime"))
check("rerank-fail -> mode vector", res["mode"] == "vector")
check("rerank-fail -> anchor still filtered",
      res["results"][0]["title"] == "Nanoha")

# ── malformed ranking entries tolerated ──────────────────────────────────────

_queue_summarizer([parse_answer,
                   {"ranking": [{"i": "x"}, {"i": 99, "fit": 9},
                                {"i": 1, "fit": 8, "why": "ok"}]}])
res = asyncio.run(ss.curated_search(
    "like gushing over magical girls", n_results=3, domain="anime"))
check("malformed entries skipped, valid one wins",
      res["mode"] == "reranked" and res["results"][0]["title"] == "Raising Project")

# ── schema guards (the empty-ranking failure) ────────────────────────────────
# Grammar-forced output satisfied the old schema with a literal
# {"ranking": []} on EVERY call — the search silently served vector order.
check("rerank schema forbids the empty array (minItems)",
      ss._RERANK_SCHEMA["properties"]["ranking"].get("minItems", 0) >= 1)
check("rerank schema requires why (no bare-index cop-out)",
      "why" in ss._RERANK_SCHEMA["properties"]["ranking"]["items"]["required"])
check("literal-constraints rule present (adult cast != adult themes)",
      "never substitute" in ss._RERANK_SYS)

# ── parse helper shape guards ────────────────────────────────────────────────

_queue_summarizer([{"anchor_title": "  ", "constraints": ["a", 3, ""],
                    "search_text": ""}])
parsed = asyncio.run(ss._parse_query("query text"))
check("blank anchor -> None; junk constraints filtered; empty text -> query",
      parsed["anchor_title"] is None and parsed["constraints"] == ["a"]
      and parsed["search_text"] == "query text")

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
chat = (root / "src/routers/chat.py").read_text(encoding="utf-8")
check("chat RAG delegates to the shared core",
      "from src.services.semantic_search import semantic_hits, format_rag_context" in chat)
check("chat no longer queries chroma inline",
      "chroma.query" not in chat)

lib = (root / "src/routers/library.py").read_text(encoding="utf-8")
check("semantic-search endpoint registered",
      '@router.get("/semantic-search")' in lib
      and "Depends(get_current_user)" in lib.split('semantic-search')[1][:600])
check("endpoint clamps limit and validates category",
      'category if category in ("movie", "show", "anime", "music")' in lib)
check("endpoint rides curated_search and reports mode",
      "curated_search" in lib and '"mode": res["mode"]' in lib)

html = (root / "frontend/index.html").read_text(encoding="utf-8")
for frag in ["lib-search", "searchLibrary()", "semantic-search?q=",
             "fit_note", "curating"]:
    check(f"frontend has {frag}", frag in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
