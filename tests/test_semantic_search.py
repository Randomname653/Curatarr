"""Tests for the extracted semantic-search core (Block 3).

    python tests/test_semantic_search.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

html = (root / "frontend/index.html").read_text(encoding="utf-8")
for frag in ["lib-search", "searchLibrary()", "semantic-search?q="]:
    check(f"frontend has {frag}", frag in html)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
