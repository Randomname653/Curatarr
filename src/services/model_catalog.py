"""Tested-model catalog + VRAM-aware recommendations for the setup wizard.

NOT a place for guesses. Every entry marked ``verified`` mirrors a row of
``tests/benchmarks/model_baselines.csv`` — the benchmark harness's own
output — and ``tests/test_model_catalog.py`` fails the moment catalog and
CSV drift apart (a model dropped, disqualified, or re-roled by a newer
bench run must be reflected here, deliberately). The wizard therefore only
ever *recommends* models that earned it on the bench; anything the user
types beyond the catalog is shown with an explicit "untested" banner.

VRAM figures are the observed q4 weight footprint on the bench host; the
recommender adds context/KV headroom on top (the qwen3.6 lesson: 22 GB of
weights on a 24 GB card starved the KV cache into 600-second answers, so
"it fits" must mean weights + context, not weights alone). The figures
stay approximate on purpose — the wizard's post-pull warm-up check
(``setup_wizard.warmup_check``) measures the real GPU/CPU split and is the
enforcement; this catalog only steers the choice.
"""
from __future__ import annotations

# Context/KV headroom the recommender demands ON TOP of the weight footprint.
VRAM_HEADROOM_GB = 3.0

# ── the catalog ────────────────────────────────────────────────────────────
# role: what the wizard is picking for. verified: backed by a CSV row.
# fallback_for: appears only when the preferred entry of the role doesn't fit.
CURATOR_MODELS = [
    {
        "model": "gemma4:31b",
        "vram_gb": 19.0,
        "verified": True,
        "label": "Recommended — production standard",
        "notes": ("The benchmarked default: best chat spine (pushback without "
                  "sycophancy) and format discipline. Needs a 24 GB GPU."),
    },
    {
        "model": "gemma4:26b",
        "vram_gb": 15.0,
        "verified": True,
        "label": "Low-VRAM fallback",
        "notes": ("Fast and format-solid, but noticeably flatter arguments "
                  "than the 31b — verdicts read more generic. The verified "
                  "floor: nothing smaller has passed the bench."),
    },
]

SUMMARIZER_MODELS = [
    {
        "model": "granite4.1:8b",
        "vram_gb": 5.0,
        "verified": True,
        "label": "Recommended",
        "notes": "Production summarizer (enrichment, significance distills).",
    },
    {
        "model": "granite4.1:3b",
        "vram_gb": 2.0,
        "verified": True,
        "label": "Low-VRAM fallback",
        "notes": "Speed / VRAM-constrained fallback (the shipped backup).",
    },
]

EMBEDDING_MODELS = [
    {
        "model": "nomic-embed-text-v2-moe",
        "vram_gb": 0.6,
        "verified": True,
        "label": "Recommended",
        "notes": "Production embedder for the vector store.",
    },
]

# Models a bench run explicitly DISQUALIFIED — the wizard must never offer
# these even as custom entries without a warning naming the reason.
# Two-bake split: a dedicated deletion-judge bake. It never co-resides with
# the curator (llm_priority evicts one for the other), so "fits" is judged
# alone, exactly like the curator - but it is an OPTION, never a floor.
PITCHER_MODELS = [
    {
        "model": "qwen3.8:27b",
        "vram_gb": 17.0,
        "verified": True,
        "label": "Recommended for the two-bake split",
        "notes": ("Pipeline-bench winner for deletion pitches: collision "
                  "flagging, data-bound refusals, 2.4x the curator's speed. "
                  "Fails the chat bench (sycophancy, confabulation) - which is "
                  "why it judges deletions ONLY and never chats."),
    },
]

DISQUALIFIED = {
    "qwen3.6:latest": "KV starvation on 24 GB (2s->600s escalation), timeout pitches",
    "muse-glimmer:30b": "never emits valid schema JSON; persona breaks",
}


def _fits(entry: dict, vram_gb: float) -> bool:
    return vram_gb >= entry["vram_gb"] + VRAM_HEADROOM_GB


def _tight(entry: dict, vram_gb: float) -> bool:
    """Fits, but with less than 2 GB beyond the demanded headroom."""
    return _fits(entry, vram_gb) and (
        vram_gb < entry["vram_gb"] + VRAM_HEADROOM_GB + 2.0)


def recommend_models(vram_gb: float | None,
                     installed: set[str] | None = None) -> dict:
    """VRAM-aware wizard recommendation.

    Returns ``{"curator": [...], "summarizer": [...], "embedding": [...],
    "floor_note": str|None}`` where each list holds catalog entries extended
    with ``fits`` / ``tight`` / ``installed`` flags, preferred-first. With
    ``vram_gb=None`` (no GPU probe) every entry is listed with fits=None —
    the frontend then shows the manual VRAM picker instead of verdicts.
    ``installed`` (normalized Ollama tags) marks zero-download choices; an
    installed model that fits outranks a not-installed one of the same role
    tier, because no download is the smoothest setup there is.
    """
    installed = installed or set()

    def _norm(n: str) -> str:
        return n.split(":", 1)[0] if n.endswith(":latest") else n

    inst = {_norm(m) for m in installed}

    def _mark(entries: list[dict]) -> list[dict]:
        out = []
        for e in entries:
            row = dict(e)
            row["fits"] = None if vram_gb is None else _fits(e, vram_gb)
            row["tight"] = False if vram_gb is None else _tight(e, vram_gb)
            row["installed"] = _norm(e["model"]) in inst
            out.append(row)
        if vram_gb is not None:
            # fitting before non-fitting; inside that, installed first,
            # catalog order (= bench preference) last word.
            out.sort(key=lambda r: (not r["fits"], not r["installed"]))
        return out

    curator = _mark(CURATOR_MODELS)
    floor_note = None
    if vram_gb is not None and not any(r["fits"] for r in curator):
        smallest = min(e["vram_gb"] + VRAM_HEADROOM_GB for e in CURATOR_MODELS)
        floor_note = (
            f"No bench-verified curator fits in {vram_gb:.0f} GB — the smallest "
            f"verified configuration needs ~{smallest:.0f} GB (weights + "
            f"context). You can still enter a smaller model manually, but it "
            f"runs untested: expect softer, flatter verdicts, and check the "
            f"warm-up result for CPU spill.")

    pitcher = _mark(PITCHER_MODELS)
    pitcher_note = None
    if vram_gb is not None and not any(r["fits"] for r in pitcher):
        pitcher_note = (
            f"The two-bake split needs ~{PITCHER_MODELS[0]['vram_gb'] + VRAM_HEADROOM_GB:.0f} GB "
            f"for the dedicated judge on its own; with {vram_gb:.0f} GB the "
            f"curator bake handles deletions too (graceful single-bake mode).")
    return {
        "curator": curator,
        "summarizer": _mark(SUMMARIZER_MODELS),
        "embedding": _mark(EMBEDDING_MODELS),
        "pitcher": pitcher,
        "pitcher_note": pitcher_note,
        "floor_note": floor_note,
    }
