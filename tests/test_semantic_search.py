"""Tests for the semantic-search core (Block 3 + evidence-scoring v3).

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

# ── stubs ────────────────────────────────────────────────────────────────────

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
            "ids": [[f"sonarr:{i}" for i in range(n)]],
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


def _queue_summarizer(answers):
    """Stub _summarizer_json with a FIFO of canned answers (parse only now)."""
    q = list(answers)
    async def fake(system, user, schema, num_predict, timeout):
        return q.pop(0) if q else None
    ss._summarizer_json = fake


# Evidence stubs: controlled tag sets + controlled vectors.
_real_candidate_tags = ss._candidate_tags   # kept for the cascade test below
TAGS_BY_TITLE = {
    "Gushing Over Magical Girls": ["Bondage", "Sadism"],
    "Nanoha": ["Primarily Child Cast", "Magic"],
    "Raising Project": ["Primarily Adult Cast", "Gore"],
    "Jungle De Ikou": ["Child Protagonist"],
    "Irregular Witch": ["Primarily Adult Cast", "Found Family"],
    "Madoka": ["Time Loop", "Tragedy"],
}
ss._candidate_tags = lambda hits, domain=None: [
    TAGS_BY_TITLE.get(h["title"], []) for h in hits]

# Orthogonal unit vectors: "adult cast" matches only the adult-cast tags,
# "child" tags match the child constraint, everything else is orthogonal.
VECS = {
    "adult cast": [1, 0, 0], "primarily adult cast": [1, 0, 0],
    "child cast": [0, 1, 0], "primarily child cast": [0, 1, 0],
    "child protagonist": [0, 1, 0],
    "bondage": [0, 0, 1], "sadism": [0, 0, 1],
    "gore": [0.0, 0.6, 0.8], "magic": [0, 0, 0.2], "found family": [0, 0, 0.1],
    "time loop": [0, 0, 0.15], "tragedy": [0, 0, 0.25],
}
async def _fake_vectors(texts, query_side=False):
    for t in texts:
        k = ss._norm_tag(t)
        if k in VECS:
            ss._vec_memo[k] = VECS[k]
    return {ss._norm_tag(t): VECS.get(ss._norm_tag(t)) for t in texts}
ss._texts_to_vectors = _fake_vectors

parse_adult = {"anchor_title": "gushing over magical girls",
               "constraints": ["adult cast"],
               "search_text": "dark magical girl adult cast"}

# ── evidence path: order, caps, notes, anchor filter ─────────────────────────

_queue_summarizer([parse_adult])
res = asyncio.run(ss.curated_search(
    "a show like gushing over magical girls but with more adult cast",
    n_results=5, domain="anime"))
check("evidence path -> mode evidence", res["mode"] == "evidence")
check("anchor filtered", all(h["title"] != "Gushing Over Magical Girls"
                             for h in res["results"]))
titles = [h["title"] for h in res["results"]]
check("adult-cast-tagged candidates rank first",
      set(titles[:2]) == {"Raising Project", "Irregular Witch"})
top = res["results"][0]
check("evidenced constraint cited in the note",
      "adult cast" in top["fit_note"] and "Primarily Adult Cast" in top["fit_note"])
check("unevidenced candidates capped at <=5",
      all(h["fit"] <= 5 for h in res["results"] if h["title"] in
          ("Nanoha", "Jungle De Ikou", "Madoka")))
check("unbelegt named in the note",
      any("unbelegt" in h["fit_note"] for h in res["results"]
          if h["title"] == "Madoka"))
check("met counts evidenced constraints per hit",
      res["results"][0]["met"] == 1
      and all(h["met"] == 0 for h in res["results"]
              if h["title"] in ("Madoka", "Jungle De Ikou")))
check("coverage reports best_met + constraint count",
      res["coverage"] == {"best_met": 1, "constraints": 1})

# ── negation: "no gore" violated -> capped at 2 ──────────────────────────────

parse_neg = {"anchor_title": None, "constraints": ["adult cast", "no gore"],
             "search_text": "adult stories"}
_queue_summarizer([parse_neg])
res = asyncio.run(ss.curated_search("adult stories no gore",
                                    n_results=6, domain="anime"))
rp = next(h for h in res["results"] if h["title"] == "Raising Project")
iw = next(h for h in res["results"] if h["title"] == "Irregular Witch")
check("negated constraint violation caps fit at 2",
      rp["fit"] <= 2 and "violates" in rp["fit_note"])
check("negation-clean candidate keeps its score",
      iw["fit"] > rp["fit"] and "free of" in iw["fit_note"])

# ── regex anchor net (LLM missed the lowercase mid-sentence title) ───────────

parse_no_anchor = {"anchor_title": None, "constraints": ["adult cast"],
                   "search_text": "dark magical girl"}
_queue_summarizer([parse_no_anchor])
res = asyncio.run(ss.curated_search(
    "like gushing over magical girls but darker and mature",
    n_results=4, domain="anime"))
check("regex net recovers the anchor when the parse returns null",
      res["anchor"] == "Gushing Over Magical Girls")
check("regex-recovered anchor is filtered from results",
      all(h["title"] != "Gushing Over Magical Girls" for h in res["results"]))

# ── fallbacks ────────────────────────────────────────────────────────────────

_queue_summarizer([None])
res = asyncio.run(ss.curated_search("magical girls", n_results=4, domain="anime"))
check("parse-fail -> mode vector", res["mode"] == "vector")
check("parse-fail -> vector order kept",
      [h["title"] for h in res["results"]] == TITLES[:4])
check("overfetch requested (3x limit, floor 24)", fake_chroma.last_n == 24)
check("scores kept from distances",
      res["results"][0]["score"] == 0.9 and res["results"][1]["score"] == 0.8)

_queue_summarizer([{"anchor_title": None, "constraints": [],
                    "search_text": "magical"}])
res = asyncio.run(ss.curated_search("magical", n_results=3, domain="anime"))
check("no constraints -> vector mode (nothing to evidence)",
      res["mode"] == "vector")

# ── doc ids reach the hits (the 3%-vs-87% cache-key bug) ─────────────────────

_queue_summarizer([None])
res = asyncio.run(ss.curated_search("x y", n_results=3, domain="anime"))
check("doc_id carried on every hit",
      all(h.get("doc_id", "").startswith("sonarr:") for h in res["results"]))

# ── title dedup (index holds id-keyed + title-keyed docs for one title) ──────

class DupChroma(FakeChroma):
    def query(self, query_embeddings=None, n_results=10, where=None):
        r = super().query(query_embeddings, n_results, where)
        for key in ("ids", "documents", "metadatas", "distances"):
            r[key][0] = r[key][0][:2] + r[key][0][:2]  # duplicate first two
        return r

cw.get_chroma_db = lambda: DupChroma()
_queue_summarizer([None])
res = asyncio.run(ss.curated_search("magical", n_results=6, domain="anime"))
titles = [h["title"] for h in res["results"]]
check("duplicate index docs collapse to one hit per title",
      len(titles) == len(set(titles)))
cw.get_chroma_db = lambda: fake_chroma

# ── genre-coherence gate: anchored probe hits need genre overlap ─────────────

class ProbeChroma(FakeChroma):
    """The constraint probe (n_results=12 in these tests) returns a
    genre-foreign stray — must be dropped when an anchor is known."""
    def query(self, query_embeddings=None, n_results=10, where=None):
        if n_results == 12:
            return {
                "ids": [["sonarr:99"]],
                "documents": [["doc Akagi"]],
                "metadatas": [[{"title": "Akagi", "genres": "Gambling, Noir",
                                "themes": "", "year": 2005}]],
                "distances": [[0.05]],
            }
        return super().query(query_embeddings, n_results, where)


cw.get_chroma_db = lambda: ProbeChroma()
_queue_summarizer([parse_adult])
res = asyncio.run(ss.curated_search(
    "like gushing over magical girls but adult", n_results=6, domain="anime"))
check("genre-foreign probe hit dropped when anchor known",
      all(h["title"] != "Akagi" for h in res["results"]))
cw.get_chroma_db = lambda: fake_chroma

# ── raw-tag cascade: enriched(doc_id) resolves the external id for raw ───────

class FakeCache:
    """raw is EXTERNAL-id keyed; enriched hits on the doc id and carries
    the resolved anilist_id — the cascade the live Gleipnir miss exposed."""
    def __init__(self):
        self.data = {
            "enriched:anime:sonarr:1": {"response": {
                "anilist_id": 99, "keywords": ["Lossy Keyword"]}},
            "raw:anime:99": {"response": {
                "tags": ["Femdom", "Seinen", "Gore"]}},
        }

    def get_cache(self, key):
        return self.data.get(key)

    def close(self):
        pass


import src.cache.metadata_cache as mc
_orig_mc = mc.MetadataCache
mc.MetadataCache = FakeCache
tags_out = _real_candidate_tags(
    [{"title": "Gleipnir", "doc_id": "sonarr:1", "themes": "death game"}],
    "anime")
mc.MetadataCache = _orig_mc
check("raw tags resolved via the enriched entry's external id",
      "Femdom" in tags_out[0] and "Seinen" in tags_out[0])
check("raw tags beat the lossy enriched keywords",
      "Lossy Keyword" not in tags_out[0])
check("chroma themes still unioned in", "death game" in tags_out[0])

# ── tag exclusivity: one tag evidences at most one positive constraint ──────

VECS["darker"] = [0, 0, 1]          # matches bondage/sadism vector space
excl_tags = {"Solo": ["Bondage"]}   # ONE tag that would satisfy both
ss._candidate_tags = lambda hits, domain=None: [
    excl_tags.get(h["title"], TAGS_BY_TITLE.get(h["title"], [])) for h in hits]
TITLES.append("Solo")
parse_two = {"anchor_title": None,
             "constraints": ["bondage", "darker"],
             "search_text": "x"}
_queue_summarizer([parse_two])
res = asyncio.run(ss.curated_search("bondage darker", n_results=7,
                                    domain="anime"))
solo = next(h for h in res["results"] if h["title"] == "Solo")
check("one tag cannot evidence two constraints",
      solo["fit_note"].count("Bondage") <= 2
      and "unbelegt" in solo["fit_note"])
TITLES.remove("Solo")
ss._candidate_tags = lambda hits, domain=None: [
    TAGS_BY_TITLE.get(h["title"], []) for h in hits]

# ── unit guards on the pure helpers ──────────────────────────────────────────

check("negation split: 'no gore' -> ('gore', True)",
      ss._split_negation("no gore") == ("gore", True))
check("negation split: plain constraint passes through",
      ss._split_negation("darker") == ("darker", False))
check("U+2011 hyphen normalized in tag keys",
      ss._norm_tag("Gore‑heavy  Action") == "gore-heavy action")

# ── parse helper shape guards ────────────────────────────────────────────────

_queue_summarizer([{"anchor_title": "  ", "constraints": ["a", 3, ""],
                    "search_text": ""}])
parsed = asyncio.run(ss._parse_query("query text"))
check("blank anchor -> None; junk constraints filtered; empty text -> query",
      parsed["anchor_title"] is None and parsed["constraints"] == ["a"]
      and parsed["search_text"] == "query text")

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
src_text = Path(ss.__file__).read_text(encoding="utf-8")
check("no LLM reranker left in the module",
      "_RERANK_SYS" not in src_text and "_RERANK_SCHEMA" not in src_text)
check("summarizer used for the parse only",
      src_text.count("_summarizer_json(") == 2)  # def + parse call
check("evidence thresholds documented as fixture-calibrated",
      "test_search_fixtures" in src_text)
check("raw lookup follows the enriched entry's resolved external ids",
      'enriched.get("anilist_id")' in src_text
      and 'f"raw:{domain}:{rid}"' in src_text)

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
             "fit_note", "curating", "mode === 'evidence'",
             "No library title carries this full profile"]:
    check(f"frontend has {frag}", frag in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
