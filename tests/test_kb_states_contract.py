"""Backend ↔ frontend contract for the enrichment state vocabulary.

Syntax-valid JS that indexes ``s[k]`` for a state the backend renamed is
runtime-dead (the tile falls into its error banner). Nothing caught that
shape of bug before; this does.

    python tests/test_kb_states_contract.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import enrichment_state as es

OLD_NAMES = ("enriched_live", "not_findable", "retry_queued", "awaiting_polish",
             "llm_polished", "queued_for_retry")


def test_frontend_knows_every_backend_state():
    fe = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    kb = fe[fe.index("const KB_STATE_DEFS"):fe.index("async function loadCacheInventory")]
    for s in es.STATES:
        assert f"'{s}'" in kb or f"{s}:" in kb, f"frontend KB view does not know state {s!r}"
    for old in OLD_NAMES:
        assert old not in fe, f"stale state name {old!r} still in the frontend"


def test_one_classifier_everywhere():
    from src.services import kb_overview
    assert kb_overview._STATES is es.STATES
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    kb = (_ROOT / "src/services/kb_overview.py").read_text(encoding="utf-8")
    en = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    assert "classify_enrichment_row" in lib and "classify_enrichment_row" in kb
    assert "EnrichmentStatus.next_retry_at" in kb, "the KB view must read the row truth"
    for old in OLD_NAMES:
        assert old not in lib and old not in kb, f"stale state name {old!r} in a backend classifier"
    assert "Not found in metadata APIs" not in en
    for key in ("state_definitions", "done_states", '"open"'):
        assert key in kb, f"overview payload lacks {key}"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
