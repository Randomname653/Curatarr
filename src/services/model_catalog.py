"""Tested-model catalog + VRAM-aware recommendations for the setup wizard.

NOT a place for guesses. Every entry marked ``verified`` mirrors a row of
``tests/benchmarks/model_baselines.csv`` — the benchmark harness's own
output — and ``tests/test_model_catalog.py`` fails the moment catalog and
CSV drift apart (a model dropped, disqualified, or re-roled by a newer
bench run must be reflected here, deliberately). The wizard therefore only
ever *recommends* models that earned it on the bench; anything the user
types beyond the catalog is shown with an explicit "untested" banner.

VRAM figures are the q4 weight files as installed on the bench host
(``ollama list``, checked 2026-10-15 — the earlier 15 GB for gemma4:26b was
3.6 GB short and let the wizard recommend it to 18 GB cards it spills on);
the recommender adds context/KV headroom on top (the qwen3.6 lesson: 22 GB of
weights on a 24 GB card starved the KV cache into 600-second answers, so
"it fits" must mean weights + context, not weights alone). The figures
stay approximate on purpose — the wizard's post-pull warm-up check
(``setup_wizard.warmup_check``) measures the real GPU/CPU split and is the
enforcement; this catalog only steers the choice.
"""
from __future__ import annotations

import json
from pathlib import Path

# Context/KV headroom the recommender demands ON TOP of the weight footprint.
VRAM_HEADROOM_GB = 3.0

# ── the catalog ────────────────────────────────────────────────────────────
# role: what the wizard is picking for. verified: backed by a CSV row.
# fallback_for: appears only when the preferred entry of the role doesn't fit.
CURATOR_MODELS = [
    {
        "model": "gemma4:31b",
        "vram_gb": 19.9,
        "verified": True,
        "label": "Recommended — production standard",
        "notes": ("The benchmarked default: best chat spine (pushback without "
                  "sycophancy) and format discipline. Needs a 24 GB GPU."),
    },
    {
        "model": "gemma4:26b",
        "vram_gb": 18.6,
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
        "vram_gb": 5.3,
        "verified": True,
        "label": "Recommended",
        "notes": "Production summarizer (enrichment, significance distills).",
    },
    {
        "model": "granite4.1:3b",
        "vram_gb": 2.1,
        "verified": True,
        "label": "Low-VRAM fallback",
        "notes": "Speed / VRAM-constrained fallback (the shipped backup).",
    },
]

EMBEDDING_MODELS = [
    {
        "model": "nomic-embed-text-v2-moe",
        "vram_gb": 1.0,
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
        "vram_gb": 17.7,
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
                     installed: set[str] | None = None,
                     gpu_name: str | None = None) -> dict:
    """VRAM-aware wizard recommendation.

    Returns ``{"curator": [...], "summarizer": [...], "embedding": [...],
    "floor_note": str|None}`` where each list holds catalog entries extended
    with ``fits`` / ``tight`` / ``installed`` flags, preferred-first. With
    ``vram_gb=None`` (no GPU probe) every entry is listed with fits=None —
    the frontend then shows the manual VRAM picker instead of verdicts.
    ``installed`` (normalized Ollama tags) marks zero-download choices; an
    installed model that fits outranks a not-installed one of the same role
    tier, because no download is the smoothest setup there is.
    ``package`` is the hardware matrix's measured recommendation for this
    card (``hardware_package``), None until a bench run exported one.
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
        "package": hardware_package(vram_gb, gpu_name, installed),
    }


# ── the hardware matrix (tests/benchmarks/hwbench) ─────────────────────────
# model_matrix.json is exported by the hardware-matrix bench: per VRAM tier,
# the measured best curator PACKAGE — model, LLM_PROFILE, num_ctx, num_gpu,
# Ollama server settings — and the speed it ran at. Until a bench run has
# exported it the file is absent and the wizard falls back to the fit
# verdicts above. tests/test_model_catalog.py keeps every matrix model inside
# CURATOR_MODELS, so a model reaches the wizard only through a deliberate
# catalog entry.
MATRIX_PATH = Path(__file__).resolve().parent / "model_matrix.json"

# Generation speed is bound by memory bandwidth, so a speed measured on the
# bench card is scaled by the bandwidth ratio for the user's card — and
# labelled an estimate. Datasheet GB/s; unknown cards get no estimate.
GPU_BANDWIDTH_GBPS = {
    "RTX 5090": 1792, "RTX 5080": 960, "RTX 5070 Ti": 896, "RTX 5070": 672,
    "RTX 5060 Ti": 448, "RTX 5060": 448,
    "RTX 4090": 1008, "RTX 4080 SUPER": 736, "RTX 4080": 717,
    "RTX 4070 Ti SUPER": 672, "RTX 4070 Ti": 504, "RTX 4070 SUPER": 504,
    "RTX 4070": 504, "RTX 4060 Ti": 288, "RTX 4060": 272,
    "RTX 3090 Ti": 1008, "RTX 3090": 936, "RTX 3080 Ti": 912, "RTX 3080": 760,
    "RTX 3070 Ti": 608, "RTX 3070": 448, "RTX 3060 Ti": 448, "RTX 3060": 360,
    "RTX 2080 Ti": 616, "RTX 2080": 448, "RTX 2070": 448, "RTX 2060": 336,
    "GTX 1080 Ti": 484,
}


def load_matrix(path: Path = MATRIX_PATH) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def bandwidth(gpu_name: str | None) -> int | None:
    """Datasheet bandwidth for a card name as nvidia-smi spells it; the
    longest matching key wins ("RTX 4070 Ti SUPER" before "RTX 4070")."""
    if not gpu_name:
        return None
    name = gpu_name.upper()
    hits = [k for k in GPU_BANDWIDTH_GBPS if k.upper() in name]
    return GPU_BANDWIDTH_GBPS[max(hits, key=len)] if hits else None


def tier_for(vram_gb: float | None, tiers) -> str | None:
    """The largest bench tier the card reaches. nvidia-smi reports a 12 GB
    card as 11.99 GB, so a quarter GB of slack."""
    if vram_gb is None:
        return None
    fits = [int(t) for t in tiers if int(t) <= vram_gb + 0.25]
    return str(max(fits)) if fits else None


def hardware_package(vram_gb: float | None, gpu_name: str | None = None,
                     installed: set[str] | None = None,
                     matrix: dict | None = None) -> dict | None:
    """The bench-measured package for this card, or None (no matrix, no
    VRAM figure, or no tier this small)."""
    matrix = matrix if matrix is not None else load_matrix()
    if not matrix or vram_gb is None:
        return None
    tiers = matrix.get("tiers") or {}
    tier = tier_for(vram_gb, tiers)
    if tier is None:
        return {"tier_gb": None, "recommended": None,
                "note": (f"The bench found no curator that runs acceptably below "
                         f"{min(int(t) for t in tiers)} GB.")} if tiers else None
    row = tiers[tier]
    rec = row.get("recommended")
    if not rec:
        return {"tier_gb": int(tier), "recommended": None,
                "note": f"Nothing passed the bench at {tier} GB."}
    out = {"tier_gb": int(tier), "recommended": dict(rec),
           "alternatives": row.get("alternatives") or [],
           "bench": {"generated": matrix.get("generated"),
                     "bench_version": matrix.get("bench_version")}}
    measured_on = (rec.get("measured_on") or {}).get("gpu")
    mine, theirs = bandwidth(gpu_name), bandwidth(measured_on)
    gen = (rec.get("expected") or {}).get("gen_tok_s")
    if gen and mine and theirs and (rec.get("expected") or {}).get("gpu_share_pct") == 100:
        out["estimate"] = {"gen_tok_s": round(gen * mine / theirs, 1),
                           "basis": f"measured {gen} tok/s on {measured_on}, "
                                    f"scaled by memory bandwidth ({mine} vs {theirs} GB/s)"}
    if installed is not None:
        norm = {m.split(":", 1)[0] if m.endswith(":latest") else m for m in installed}
        out["recommended"]["installed"] = rec["model"] in norm
    return out
