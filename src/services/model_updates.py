"""Model updates: notice when the registry has new weights for a model in
use, test old against new on real titles, let the owner keep the pin.

Every curatarr-* bake carries the weights it was built from, so pulling a
model never changed what runs — the installed copy is a pin, silently. That
also meant nobody noticed when a tag got new weights (gemma4:31b and :26b
both had, by 2026-10-15). This module makes the pin visible:

``check_updates``   daily, no GPU: the RUNNING weights of each bake (the
                    first FROM blob of /api/show — the API works for a
                    remote Ollama too, where the manifest files are out of
                    reach) against the registry manifest's model layer.
``run_comparison``  owner-triggered: pins the current base under a
                    ``-pinned-<digest>`` tag (a manifest copy, no extra
                    disk), pulls the new weights, bakes them like
                    production, and runs both bakes over the SAME evidence
                    for ten library titles (pillars.adjudicate +
                    write_monologue) plus three chat questions. Result:
                    verdict agreement, wording similarity, time per call,
                    generation speed, VRAM and GPU share, failures.
``decide``          "update" rebuilds the bakes from the new weights;
                    "stay" puts the pinned weights back under the tag and
                    remembers the declined digest, so the notice returns
                    only when the registry moves again.

The summarizer gets the notice, but no comparison yet (its jobs are not
the judge path); it updates by hand. The embedding model is never offered:
new weights would mean re-embedding the whole library.
"""
from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import statistics
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

STATE_KEY = "model_updates"
LOCK_KEY = "model_update_compare"
REGISTRY = "https://registry.ollama.ai"
TEST_BAKE = "curatarr-update-test"
SAMPLE_TITLES = 10
CHAT_PROBES = (
    "What separates a slow-burn thriller that earns its pace from one that just drags?",
    "Give me your honest one-paragraph take on long-running anime that never adapt their ending.",
    "Warum altern manche Sitcoms schlecht und andere nicht? Zwei Sätze.",
)
_FROM = re.compile(r"^FROM\s+.*?sha256[-:]([0-9a-f]{64})", re.M | re.I)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── identity ────────────────────────────────────────────────────────────────
async def running_weights(endpoint: str, model: str) -> str | None:
    """sha256 of the weights a model (or bake) actually loads, or None."""
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{endpoint}/api/show", json={"model": model})
        if r.status_code != 200:
            return None
        m = _FROM.search(r.json().get("modelfile") or "")
        return f"sha256:{m.group(1)}" if m else None
    except (httpx.HTTPError, ValueError):
        return None


def split_ref(ref: str) -> tuple[str, str, str] | None:
    """Registry coordinates of a pullable ref; None for a local-only name
    (a bake, a model created from a file, another registry's host)."""
    name, _, tag = ref.partition(":")
    first, sep, _ = name.partition("/")
    # A host only ever comes before a slash (hf.co/org/model); a dot in a
    # bare name is a version (qwen3.8, granite4.1).
    if not name or (sep and "." in first):
        return None
    ns, _, bare = name.rpartition("/")
    return ns or "library", bare, tag or "latest"


async def registry_weights(ref: str) -> str | None:
    coords = split_ref(ref)
    if not coords:
        return None
    ns, name, tag = coords
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(f"{REGISTRY}/v2/{ns}/{name}/manifests/{tag}", headers={
                "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
        if r.status_code != 200:
            return None
        for layer in r.json().get("layers") or []:
            if (layer.get("mediaType") or "").endswith("image.model"):
                return layer.get("digest")
    except (httpx.HTTPError, ValueError):
        return None
    return None


def tracked_models(settings) -> list[dict]:
    rows = [{"role": "curator", "bake": settings.CURATOR_MODEL,
             "base": settings.BASE_CURATOR_MODEL, "comparable": True},
            {"role": "summarizer", "bake": settings.SUMMARIZER_MODEL,
             "base": settings.BASE_SUMMARIZER_MODEL, "comparable": False}]
    if getattr(settings, "PITCHER_MODEL", ""):
        rows.append({"role": "pitcher", "bake": settings.PITCHER_MODEL,
                     "base": settings.BASE_PITCHER_MODEL, "comparable": True})
    return rows


# ── state ───────────────────────────────────────────────────────────────────
def load_state() -> dict:
    from src.services.app_state import get_state, set_state
    raw = get_state(STATE_KEY) or "{}"
    try:
        state = json.loads(raw)
        if isinstance(state, dict):
            return state
    except ValueError:
        pass
    # A corrupt value used to read as {} and the next save_state wrote that
    # over it — dropping a ``test_started`` marker, so a cut-off test was
    # never rolled back. The raw value is kept aside before anything resets.
    logger.error("[model update] stored state %r is not a JSON object (%d chars) — "
                 "starting empty; the raw value is kept under %s_corrupt",
                 STATE_KEY, len(raw), STATE_KEY)
    set_state(f"{STATE_KEY}_corrupt", raw)
    return {}


def save_state(state: dict) -> None:
    from src.services.app_state import set_state
    set_state(STATE_KEY, json.dumps(state, ensure_ascii=False))


def classify(running: str | None, remote: str | None, declined: str | None) -> str:
    if not running or not remote:
        return "unknown"
    if running == remote:
        return "current"
    if remote == declined:
        return "pinned"
    return "update"


async def check_updates() -> list[dict]:
    """Refresh the notice state for every tracked model. Network only."""
    from src.config import settings
    endpoint = settings.effective_ollama
    state = load_state()
    out = []
    for t in tracked_models(settings):
        prev = state.get(t["role"], {})
        running = await running_weights(endpoint, t["bake"]) or await running_weights(endpoint, t["base"])
        remote = await registry_weights(t["base"])
        row = {**prev, **t, "running": running, "remote": remote, "checked_at": _now(),
               "status": classify(running, remote, prev.get("declined_remote"))}
        # A comparison of an older remote digest no longer describes this update.
        if (prev.get("comparison") or {}).get("remote") not in (None, remote):
            row.pop("comparison", None)
        state[t["role"]] = row
        out.append(row)
    save_state(state)
    return out


# ── comparison ──────────────────────────────────────────────────────────────
MIN_FREE_GB = 10.0


async def disk_room(endpoint: str, base: str) -> tuple[bool, str]:
    """Old and new weights sit side by side until the owner decides. Only
    checkable when Ollama runs on this machine; a remote Ollama's pull
    fails on its own if its disk is full."""
    import os
    import shutil
    from pathlib import Path
    from urllib.parse import urlparse
    if (urlparse(endpoint).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
        return True, ""
    coords = split_ref(base)
    need, why = 0.0, "not a registry model"
    if coords:
        ns, name, tag = coords
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(f"{REGISTRY}/v2/{ns}/{name}/manifests/{tag}", headers={
                    "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
            if r.status_code != 200:
                why = f"registry answered HTTP {r.status_code}"
            else:
                need = sum(int(l.get("size") or 0) for l in r.json().get("layers") or []) / 1e9
                why = "the manifest lists no layer sizes"
        except (httpx.HTTPError, ValueError, TypeError, AttributeError) as e:
            why = f"{type(e).__name__}: {e}"
    # Fails closed: an unknown size used to count as 0 GB, so a 20 GB pull
    # passed with 11 GB free and filled the disk Ollama and the DB live on.
    if need <= 0:
        logger.warning("[model update] size of the new %s unknown (%s) — test refused", base, why)
        return False, (f"could not read the size of the new {base} from the registry ({why}); "
                       f"the test needs it to check disk room — try again later")
    root = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models")
    free = shutil.disk_usage(root if root.exists() else Path.home()).free / 1e9
    if free - need < MIN_FREE_GB:
        return False, (f"not enough disk for the test: {free:.0f} GB free, the new "
                       f"weights need {need:.0f} GB and {MIN_FREE_GB:.0f} GB must stay free")
    return True, ""


def pin_name(base: str, digest: str) -> str:
    name, _, tag = base.partition(":")
    return f"{name}:{(tag or 'latest')}-pinned-{digest.removeprefix('sha256:')[:12]}"


async def _ollama(endpoint: str, method: str, path: str, **kw) -> httpx.Response:
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=600)) as c:
        return await c.request(method, f"{endpoint}{path}", **kw)


async def _bake(endpoint: str, name: str, base: str) -> None:
    from src.services.llm_utils import CURATOR_NUM_CTX
    from src.services.setup_wizard import CURATOR_SYSTEM_PROMPT
    r = await _ollama(endpoint, "POST", "/api/create", json={
        "model": name, "from": base, "system": CURATOR_SYSTEM_PROMPT, "stream": False,
        "parameters": {"temperature": 0.7, "num_ctx": CURATOR_NUM_CTX}})
    if r.status_code != 200:
        raise RuntimeError(f"bake {name}: HTTP {r.status_code}")


def _same(a: str, b: str) -> bool:
    norm = lambda n: n if ":" in n else f"{n}:latest"
    return norm(a or "") == norm(b or "")


async def _residency(endpoint: str, model: str) -> dict:
    """VRAM and GPU share of exactly ``model`` (the bake name — a prefix
    match once picked up the 2 GB summariser), plus every OTHER model that
    was resident at the same moment: a number taken next to another model
    is not that model's number."""
    try:
        r = await _ollama(endpoint, "GET", "/api/ps")
        models = r.json().get("models") or []
    except (httpx.HTTPError, ValueError):
        return {}
    out = {"co_resident": [m.get("name") for m in models
                           if not _same(m.get("name") or m.get("model"), model)]}
    for m in models:
        if _same(m.get("name") or m.get("model"), model):
            size, vram = m.get("size") or 0, m.get("size_vram") or 0
            out.update(vram_gb=round(vram / 2**30, 1),
                       gpu_share_pct=round(100 * vram / size) if size else None,
                       context=m.get("context_length"))
    return out


async def _alone(endpoint: str, model: str) -> None:
    """Unload every resident model except ``model`` and hold bulk
    enrichment off, so what follows is measured on a quiet card."""
    from src.services.llm_priority import _evict_model, extend_bulk_hold
    extend_bulk_hold(900)
    try:
        r = await _ollama(endpoint, "GET", "/api/ps")
        for m in r.json().get("models") or []:
            name = m.get("name") or m.get("model")
            if name and not _same(name, model):
                await _evict_model(name)
    except (httpx.HTTPError, ValueError):
        pass


SPEED_PROBES = (
    "Describe, in about 120 words, what a film archivist does all day.",
    "Explain, in about 120 words, why some series lose their audience in season two.",
    "Summarise, in about 120 words, how a record label decides which album to reissue.",
)


async def _speed(endpoint: str, model: str, probes: int = 3) -> dict:
    """Generation speed as the median of ~160-token probes, through
    /api/chat — the endpoint every curator call uses. /api/generate is not
    the same path: the 2026-10 gemma4:31b weights ran 10.5 tok/s there and
    33.0 through /api/chat (the old weights 37.1 / 35.7), so a generate
    probe called the new model three times slower than it is."""
    from src.services.llm_utils import curator_options
    gen, pre, load = [], [], []
    for prompt in SPEED_PROBES[:max(1, probes)]:
        r = await _ollama(endpoint, "POST", "/api/chat", json={
            "model": model, "stream": False, "keep_alive": "5m",
            "messages": [{"role": "user", "content": prompt}],
            **{k: v for k, v in curator_options(0.0, 160).items()}})
        d = r.json() if r.status_code == 200 else {}
        ec, ed = d.get("eval_count"), d.get("eval_duration")
        pc, pd = d.get("prompt_eval_count"), d.get("prompt_eval_duration")
        if ec and ed:
            gen.append(ec / (ed / 1e9))
        if pc and pd:
            pre.append(pc / (pd / 1e9))
        load.append((d.get("load_duration") or 0) / 1e9)
    med = lambda v: round(statistics.median(v), 1) if v else None
    return {"gen_tok_s": med(gen), "gen_tok_s_range": [round(min(gen), 1), round(max(gen), 1)] if gen else None,
            "prefill_tok_s": med(pre), "load_s": round(load[0], 1) if load else None}


async def sample_evidence(user_id: int, n: int = SAMPLE_TITLES, *,
                          exclude: set[str] | None = None, seed: int | None = None) -> list[dict]:
    """Evidence for n real titles, built ONCE so both models judge the same
    bytes. At most half come from the latest deletion proposals (the first
    test drew all ten from them — ten films, no show, anime or music); the
    rest is spread round-robin over the categories. ``exclude`` keeps a
    re-run off the titles an earlier run used; ``seed`` None = a fresh draw."""
    import random
    from src.database.connection import get_db_session
    from src.database.models import DeletionProposal
    from src.routers.recommendations import _fetch_arr_candidates
    from src.services import pillars
    exclude = {t.lower() for t in (exclude or set())}
    with get_db_session() as db:
        recent = [(p.category, p.title) for p in db.query(DeletionProposal)
                  .order_by(DeletionProposal.created_at.desc()).limit(40)]
    items = [i for i in await _fetch_arr_candidates()
             if (i.get("title") or "").lower() not in exclude]
    by_key = {((i.get("category") or ""), (i.get("title") or "").lower()): i for i in items}
    picked, seen = [], set()
    for cat, title in recent:
        if len(picked) >= max(1, n // 2):
            break
        it = by_key.get((cat or "", (title or "").lower()))
        if it and it["title"] not in seen:
            picked.append(it)
            seen.add(it["title"])
    rng = random.Random(seed)
    rest = [i for i in items if i.get("title") not in seen]
    rng.shuffle(rest)
    cats = ["show", "anime", "music", "movie"]
    while len(picked) < n * 2 and rest:
        for c in cats:
            nxt = next((i for i in rest if i.get("category") == c), None)
            if nxt:
                rest.remove(nxt)
                picked.append(nxt)
        if not any(i.get("category") in cats for i in rest):
            break
    out = []
    for it in picked:
        if len(out) >= n:
            break
        try:
            ev = await pillars.build_evidence(it, user_id, it.get("category") or "movie")
        except Exception as e:  # one broken title must not end the test
            logger.info("[model update] evidence for %r failed: %s", it.get("title"), e)
            continue
        if ev["flags"].get("evidence_thin") or ev["flags"].get("evidence_mismatched"):
            continue
        out.append({"title": it.get("title"), "category": it.get("category"),
                    "facts": ev["facts"], "law_extra": ev.get("law_extra", "")})
    return out


async def _run_side(endpoint: str, model: str, sample: list[dict], lang: str) -> dict:
    from src.services import pillars
    from src.services.llm_priority import extend_bulk_hold
    from src.services.llm_utils import curator_options
    await _alone(endpoint, model)
    rows = []
    for s in sample:
        extend_bulk_hold(900)
        t0 = time.perf_counter()
        v = await pillars.adjudicate(s["facts"], model=model, skip_priority=True,
                                     law_extra=s["law_extra"])
        j = time.perf_counter() - t0
        t0 = time.perf_counter()
        text = "" if v.get("_error_kind") else await pillars.write_monologue(
            s["facts"], v, model=model, lang_directive=lang, skip_priority=True)
        rows.append({"title": s["title"], "verdict": v.get("verdict"),
                     "pillar": v.get("protecting_pillar"), "error": v.get("_error_kind"),
                     "judge_s": round(j, 1), "monologue": text,
                     "monologue_s": round(time.perf_counter() - t0, 1)})
    chats = []
    from src.services.setup_wizard import CURATOR_SYSTEM_PROMPT
    for q in CHAT_PROBES:
        t0 = time.perf_counter()
        r = await _ollama(endpoint, "POST", "/api/chat", json={
            "model": model, "stream": False, "keep_alive": "5m",
            "messages": [{"role": "system", "content": CURATOR_SYSTEM_PROMPT},
                         {"role": "user", "content": q}], **curator_options(0.7, 400)})
        text = ((r.json().get("message") or {}).get("content") or "") if r.status_code == 200 else ""
        chats.append({"prompt": q, "text": text.strip(), "s": round(time.perf_counter() - t0, 1)})
    return {"titles": rows, "chat": chats, **(await measure(endpoint, model))}


def _is_local(endpoint: str) -> bool:
    from urllib.parse import urlparse
    return (urlparse(endpoint).hostname or "") in ("localhost", "127.0.0.1", "::1")


async def _smi_used_mib() -> int | None:
    """Card-wide VRAM in use (nvidia-smi), None without an NVIDIA driver."""
    import subprocess
    try:
        # A thread: the Windows event loop under uvicorn --reload cannot spawn
        # subprocesses (NotImplementedError) — the first review fell back to
        # Ollama's figure for exactly that reason.
        proc = await asyncio.to_thread(
            subprocess.run, ["nvidia-smi", "--query-gpu=memory.used",
                             "--format=csv,noheader,nounits"],
            capture_output=True, timeout=10)
        return max(int(x) for x in proc.stdout.decode().split() if x.strip().isdigit())
    except Exception as e:
        logger.info("[model update] nvidia-smi unavailable: %s", type(e).__name__)
        return None


async def _smi_settled(settle_s: float = 2.0, reads: int = 3) -> int | None:
    """nvidia-smi after the driver had time to release or map memory: the
    median of a few reads, so one desktop flicker (~100 MB) does not move
    the figure."""
    await asyncio.sleep(settle_s)
    vals = []
    for _ in range(reads):
        v = await _smi_used_mib()
        if v is not None:
            vals.append(v)
        await asyncio.sleep(0.5)
    return int(statistics.median(vals)) if vals else None


async def _file_gb(endpoint: str, model: str) -> float | None:
    try:
        r = await _ollama(endpoint, "GET", "/api/tags")
        for m in r.json().get("models") or []:
            if _same(m.get("name") or "", model):
                return round((m.get("size") or 0) / 2**30, 1)
    except (httpx.HTTPError, ValueError):
        pass
    return None


# /api/ps can be wrong: for the 2026-10 gemma4:31b weights (projector + MTP
# draft layers) Ollama 0.40.4 reported size = size_vram = 2.1 GB while
# nvidia-smi showed ~22 GB taken — and the first self-review recommended
# the update for that "VRAM saving". VRAM is therefore the nvidia-smi
# difference (empty card -> model loaded) when Ollama runs here; a /api/ps
# figure far below the weight file is never shown or reviewed as a number.
PS_PLAUSIBLE = 0.8


async def measure(endpoint: str, model: str, probes: int = 3) -> dict:
    """Speed probe + residency on a quiet card, VRAM cross-checked."""
    from src.services.llm_priority import _evict_model
    # Per side: everything unloaded (this model too) -> settled baseline ->
    # this model alone -> settled reading. Each side gets its OWN empty-card
    # baseline, so the other model never leaks into the difference.
    await _alone(endpoint, model)
    before = None
    if _is_local(endpoint):
        await _evict_model(model)
        before = await _smi_settled()
    speed = await _speed(endpoint, model, probes)
    res = await _residency(endpoint, model)
    after = await _smi_settled(1.0) if before is not None else None
    file_gb = await _file_gb(endpoint, model)
    ps_gb = res.get("vram_gb")
    res["vram_gb_ps"] = ps_gb
    res["weights_file_gb"] = file_gb
    res["ps_implausible"] = bool(ps_gb is not None and file_gb and res.get("gpu_share_pct") == 100
                                 and ps_gb < PS_PLAUSIBLE * file_gb)
    if before is not None and after is not None:
        res["vram_gb"], res["vram_source"] = round((after - before) / 1024, 1), "nvidia-smi"
    elif res["ps_implausible"]:
        res["vram_gb"], res["vram_source"] = None, "unknown (Ollama's figure is implausible)"
    else:
        res["vram_source"] = "ollama"
    return {"speed": speed, "residency": res, "clean": not res.get("co_resident")}


# One measurement per side is luck on a shared desktop: the same old weights
# measured 38, 34, 28 and 11 tok/s on 2026-10-15 depending on what else the
# machine did that minute. The pair is therefore measured INTERLEAVED (old,
# new, new, old, old, new — each freshly loaded, alone), so a disturbance
# hits both sides, and the median is taken per side. A side whose readings
# spread more than UNSTABLE_SPREAD around its median is "unstable": the
# reviewer is told so instead of being given a number, and no speed rule
# fires on it.
PAIR_ROUNDS = 3
UNSTABLE_SPREAD = 0.35


async def measure_pair(endpoint: str, old_model: str, new_model: str,
                       rounds: int = PAIR_ROUNDS) -> dict:
    from src.services.llm_priority import extend_bulk_hold
    reads = {"old": [], "new": []}
    order = [("old", old_model), ("new", new_model)]
    for i in range(rounds):
        extend_bulk_hold(900)
        for side, model in (order if i % 2 == 0 else order[::-1]):
            reads[side].append(await measure(endpoint, model, probes=1))
    out = {}
    for side, rs in reads.items():
        gen = [r["speed"].get("gen_tok_s") for r in rs if r["speed"].get("gen_tok_s")]
        vram = [r["residency"].get("vram_gb") for r in rs if r["residency"].get("vram_gb") is not None]
        med = round(statistics.median(gen), 1) if gen else None
        spread = round((max(gen) - min(gen)) / med, 2) if gen and med else None
        last = rs[-1]["residency"]
        out[side] = {"gen_tok_s": med, "gen_tok_s_reads": [round(g, 1) for g in gen],
                     "spread": spread, "unstable": spread is not None and spread > UNSTABLE_SPREAD,
                     "vram_gb": round(statistics.median(vram), 1) if vram else None,
                     "vram_source": last.get("vram_source"), "gpu_share_pct": last.get("gpu_share_pct"),
                     "clean": all(r["clean"] for r in rs)}
    return out


def summarize(old: dict, new: dict) -> dict:
    """The differences the owner decides on."""
    pairs = [(a, b) for a in old["titles"] for b in new["titles"] if a["title"] == b["title"]]
    judged = [(a, b) for a, b in pairs if not a["error"] and not b["error"]]
    sim = [difflib.SequenceMatcher(None, a["monologue"], b["monologue"]).ratio()
           for a, b in judged if a["monologue"] and b["monologue"]]
    chat_sim = [difflib.SequenceMatcher(None, a["text"], b["text"]).ratio()
                for a, b in zip(old["chat"], new["chat"]) if a["text"] and b["text"]]

    def med(rows, k):
        vals = [r[k] for r in rows if r.get(k) is not None]
        return round(statistics.median(vals), 1) if vals else None

    def words(rows):
        vals = [len(r["monologue"].split()) for r in rows if r.get("monologue")]
        return round(statistics.median(vals)) if vals else None

    return {
        "titles": len(pairs),
        "verdicts_same": sum(a["verdict"] == b["verdict"] for a, b in judged),
        "verdicts_compared": len(judged),
        "verdict_changes": [{"title": a["title"], "old": a["verdict"], "new": b["verdict"]}
                            for a, b in judged if a["verdict"] != b["verdict"]],
        "failures": {"old": sum(bool(a["error"]) for a, _ in pairs),
                     "new": sum(bool(b["error"]) for _, b in pairs)},
        "wording_similarity_pct": round(100 * statistics.mean(sim)) if sim else None,
        "chat_similarity_pct": round(100 * statistics.mean(chat_sim)) if chat_sim else None,
        "monologue_words": {"old": words(old["titles"]), "new": words(new["titles"])},
        "judge_s": {"old": med(old["titles"], "judge_s"), "new": med(new["titles"], "judge_s")},
        "monologue_s": {"old": med(old["titles"], "monologue_s"),
                        "new": med(new["titles"], "monologue_s")},
        "gen_tok_s": {"old": old["speed"].get("gen_tok_s"), "new": new["speed"].get("gen_tok_s")},
        "vram_gb": {"old": old["residency"].get("vram_gb"), "new": new["residency"].get("vram_gb")},
        "gpu_share_pct": {"old": old["residency"].get("gpu_share_pct"),
                          "new": new["residency"].get("gpu_share_pct")},
    }


async def run_comparison(role: str, user_id: int, task=None) -> dict:
    """Pin -> pull -> bake -> judge both on the same titles. Holds the curator
    gate for the whole run, so chat and background work wait, as for a
    deletion run. Raises on a precondition; the caller reports it."""
    from src.config import settings
    from src.services.task_monitor import task_monitor

    def msg(text):
        if task is not None:
            task_monitor.update(task, message=text)

    endpoint = settings.effective_ollama
    state = load_state()
    row = state.get(role)
    if not row or not row.get("comparable"):
        raise ValueError(f"no comparison for the {role} role")
    if row.get("status") != "update":
        raise ValueError("no update pending for this model")
    pinned = pin_name(row["base"], row["running"])
    # Marked BEFORE anything changes in Ollama: a restart mid-test leaves this
    # behind, and recover_interrupted rolls back from it.
    state[role]["test_started"] = _now()
    save_state(state)
    try:
        result = await _compare(role, row, endpoint, user_id, pinned, msg)
    except BaseException:
        # Cancelled by a shutdown, the rollback may not get to finish: then
        # the marker stays and recover_interrupted completes it at the start.
        # A rollback that raised used to replace the test's own error and
        # skip the bulk-hold release; it is logged and the original re-raised.
        from src.services.llm_priority import release_bulk_hold
        complete = False
        try:
            problems = await rollback(endpoint, row)
            complete = not problems
            if problems:
                logger.warning("[model update] rollback after the failed %s test is incomplete "
                               "(retried at the next start): %s", role, "; ".join(problems))
        except Exception as rb:
            logger.error("[model update] rollback after the failed %s test failed "
                         "(retried at the next start): %s", role, rb)
        finally:
            release_bulk_hold()
            if complete:
                _clear_marker(role)
        raise
    _clear_marker(role)
    return result


HISTORY_KEEP = 5


def archive_comparison(row: dict) -> None:
    """Move the current result into ``history`` (kept short) so a fresh run
    can start: titles, numbers and the review's verdict survive, the raw
    texts do not."""
    cmp_ = row.pop("comparison", None)
    if not cmp_:
        return
    ev = cmp_.get("evaluation") or {}
    row.setdefault("history", []).append({
        "at": cmp_.get("at"), "remote": cmp_.get("remote"),
        "titles": [t.get("title") for t in (cmp_.get("old") or {}).get("titles") or []],
        "summary": cmp_.get("summary"),
        "recommendation": ev.get("recommendation"), "tally": ev.get("tally")})
    row["history"] = row["history"][-HISTORY_KEEP:]
    row.pop("eval_error", None)


def _clear_marker(role: str) -> None:
    from src.services.llm_priority import release_bulk_hold
    release_bulk_hold()
    st = load_state()
    st.get(role, {}).pop("test_started", None)
    save_state(st)


async def _compare(role, row, endpoint, user_id, pinned, msg) -> dict:
    from src.database.connection import get_db_session
    from src.services.llm_priority import curator_priority, evict_if_resident
    from src.services.llm_utils import detect_user_language, language_directive
    from src.services.setup_wizard import pull_ollama_model
    base, bake, running = row["base"], row["bake"], row["running"]
    r = None
    if await running_weights(endpoint, base) == running:
        msg(f"pinning {base} as {pinned}")
        r = await _ollama(endpoint, "POST", "/api/copy", json={"source": base, "destination": pinned})
    elif await running_weights(endpoint, pinned) != running:
        # The tag already moved (pulled by hand): the bake is the only copy
        # of the running weights left, so the pin is taken from it.
        r = await _ollama(endpoint, "POST", "/api/copy", json={"source": bake, "destination": pinned})
    # The copy's answer was never read: a failed pin let the pull overwrite
    # the tag with nothing left to roll back to. No pin, no pull.
    if (r is not None and r.status_code != 200) or await running_weights(endpoint, pinned) != running:
        raise RuntimeError(f"could not pin the running weights as {pinned}"
                           + (f" (HTTP {r.status_code})" if r is not None else "")
                           + " — test stopped before downloading anything")
    # A re-run finds the new weights already pulled: no disk is needed then.
    if await running_weights(endpoint, base) != row.get("remote"):
        ok, why = await disk_room(endpoint, base)
        if not ok:
            raise RuntimeError(why)
    msg(f"downloading the new {base}")
    if not await pull_ollama_model(endpoint, base):
        raise RuntimeError(f"could not pull {base}")
    new_digest = await running_weights(endpoint, base)
    if new_digest == running:
        raise RuntimeError("the pull brought no new weights")
    await _bake(endpoint, TEST_BAKE, base)

    msg("collecting ten library titles")
    used = set()
    for past in row.get("history") or []:
        used |= set(past.get("titles") or [])
    sample = await sample_evidence(user_id, exclude=used)
    with get_db_session() as db:
        lang = language_directive(detect_user_language(user_id, db))
    async with curator_priority("model update test", exclusive_model=bake):
        msg(f"running the current {base} on {len(sample)} titles")
        old = await _run_side(endpoint, bake, sample, lang)
        await evict_if_resident(bake)
        msg(f"running the new {base} on the same titles")
        new = await _run_side(endpoint, TEST_BAKE, sample, lang)
        await evict_if_resident(TEST_BAKE)
    result = {"remote": row["remote"], "new_weights": new_digest, "pinned_as": pinned,
              "at": _now(), "summary": summarize(old, new), "old": old, "new": new,
              # The judge (model_update_eval) checks both sides against these.
              "facts": {s["title"]: s["facts"] for s in sample}}
    state = load_state()
    state.setdefault(role, row)["comparison"] = result
    save_state(state)
    return result


async def _refill_facts(comparison: dict, user_id: int) -> None:
    """A result from before facts were kept: rebuild them by title (the
    library may have moved since, so the review notes it)."""
    from src.routers.recommendations import _fetch_arr_candidates
    from src.services import pillars
    missing = [t["title"] for t in comparison["old"]["titles"]
               if t["title"] not in (comparison.get("facts") or {})]
    if not missing:
        return
    by_title = {(i.get("title") or "").lower(): i for i in await _fetch_arr_candidates()}
    facts = comparison.setdefault("facts", {})
    for title in missing:
        it = by_title.get(title.lower())
        if it:
            try:
                facts[title] = (await pillars.build_evidence(
                    it, user_id, it.get("category") or "movie"))["facts"]
            except Exception as e:
                logger.info("[model eval] facts for %r: %s", title, e)
    comparison["facts_rebuilt"] = True


async def run_evaluation(role: str, user_id: int, task=None) -> dict:
    """The current base model (pinned weights, no persona) reviews the
    comparison blind and recommends; model_update_eval has the method."""
    from src.config import settings
    from src.services.llm_priority import curator_priority, evict_if_resident
    from src.services.model_update_eval import evaluate
    from src.services.task_monitor import task_monitor
    endpoint = settings.effective_ollama
    state = load_state()
    row = state.get(role) or {}
    cmp_ = row.get("comparison")
    if not cmp_:
        raise ValueError("compare first")
    # The judge must be the CURRENT weights: the pin when it still holds
    # them, else the tag, else the persona bake (its system prompt is
    # replaced by the reviewer's own, so the persona does not leak in).
    # (A plain loop: an `await` inside next(<genexp>) makes an async
    # generator, which next() cannot take — the first live review died so.)
    judge = row["bake"]
    for m in (cmp_.get("pinned_as"), row["base"]):
        if m and await running_weights(endpoint, m) == row["running"]:
            judge = m
            break
    if task is not None:
        task_monitor.update(task, message=f"{judge} reviews old vs new")
    await _refill_facts(cmp_, user_id)
    from src.services.llm_priority import release_bulk_hold
    try:
        async with curator_priority("model update review", exclusive_model=judge):
            # Speed and VRAM again, each side alone on the card: the test's
            # own figures may have been taken next to another model.
            if task is not None:
                task_monitor.update(task, message="re-measuring speed and VRAM, old and new interleaved")
            pair = await measure_pair(endpoint, row["bake"], TEST_BAKE)
            cmp_["summary"].update(
                gen_tok_s={k: v["gen_tok_s"] for k, v in pair.items()},
                gen_tok_s_reads={k: v["gen_tok_s_reads"] for k, v in pair.items()},
                speed_unstable=any(v["unstable"] for v in pair.values()),
                vram_gb={k: v["vram_gb"] for k, v in pair.items()},
                vram_source={k: v["vram_source"] for k, v in pair.items()},
                gpu_share_pct={k: v["gpu_share_pct"] for k, v in pair.items()},
                remeasured_clean=all(v["clean"] for v in pair.values()),
                timings_contaminated=not all(
                    (cmp_.get(side) or {}).get("clean", False) for side in ("old", "new")))
            await _alone(endpoint, judge)
            ev = await evaluate(cmp_, endpoint, judge)
            await evict_if_resident(judge)
    finally:
        release_bulk_hold()
    ev["at"] = _now()
    state = load_state()
    if state.get(role, {}).get("comparison"):
        state[role]["comparison"]["evaluation"] = ev
        state[role]["comparison"]["summary"] = cmp_["summary"]
        if cmp_.get("facts_rebuilt"):
            state[role]["comparison"]["facts"] = cmp_["facts"]
            state[role]["comparison"]["facts_rebuilt"] = True
        save_state(state)
    return ev


async def rollback(endpoint: str, row: dict) -> None:
    """Put Ollama back to where it was before a test: the tag on the pinned
    weights, no test bake, no pin. Deleting the test bake is what frees the
    new weights: Ollama removes every blob no manifest references any more
    (checked 2026-10-15 with a throwaway model). A pull cut off mid-way
    leaves ``-partial`` files that no manifest knows; with Ollama on this
    machine they are removed too.

    Returns what could NOT be put back (empty = all of it). Each step is
    checked against what Ollama then holds, not the call's answer alone:
    recover_interrupted used to say "everything was put back" after a copy
    or delete that had failed."""
    base, running = row["base"], row["running"]
    pinned = pin_name(base, running)
    problems: list[str] = []
    pin_ok = await running_weights(endpoint, pinned) == running
    if pin_ok and await running_weights(endpoint, base) != running:
        await _ollama(endpoint, "POST", "/api/copy", json={"source": pinned, "destination": base})
    await _ollama(endpoint, "DELETE", "/api/delete", json={"model": TEST_BAKE})
    if await running_weights(endpoint, TEST_BAKE) is not None:
        problems.append(f"the test bake {TEST_BAKE} could not be deleted (the new weights still take disk)")
    tag_ok = await running_weights(endpoint, base) == running
    if not tag_ok:
        problems.append(f"{base} could not be put back on the running weights"
                        + (f" (the pin {pinned} is kept)" if pin_ok else " (no pin of them exists)"))
    # The pin goes only once the tag holds its weights again — otherwise it
    # is the last copy of the running weights and must stay.
    if pin_ok and tag_ok:
        await _ollama(endpoint, "DELETE", "/api/delete", json={"model": pinned})
        if await running_weights(endpoint, pinned) is not None:
            # Only disk: the tag already holds the weights again.
            logger.warning("[model update] the pin %s could not be deleted", pinned)
    remove_partials(endpoint, row.get("remote"))
    return problems


def remove_partials(endpoint: str, digest: str | None) -> int:
    import os
    from pathlib import Path
    from urllib.parse import urlparse
    if not digest or (urlparse(endpoint).hostname or "") not in ("localhost", "127.0.0.1", "::1"):
        return 0
    root = Path(os.environ.get("OLLAMA_MODELS") or Path.home() / ".ollama" / "models") / "blobs"
    n = 0
    for f in root.glob(digest.replace(":", "-") + "-partial*"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n


async def recover_interrupted() -> list[str]:
    """At startup: a test that a restart cut off (``test_started`` still set,
    no result) is rolled back, and the notice says what happened."""
    from src.config import settings
    state = load_state()
    done = []
    for role, row in state.items():
        if isinstance(row, dict) and row.get("test_started") and not row.get("comparison"):
            try:
                problems = await rollback(settings.effective_ollama, row)
            except Exception as e:
                logger.warning("[model update] rollback of %s failed: %s", role, e)
                continue
            row.pop("test_started", None)
            if problems:
                logger.warning("[model update] rollback of the cut-off %s test is incomplete: %s",
                               role, "; ".join(problems))
                row["compare_error"] = ("the test was cut off by a restart; NOT everything could be "
                                        "put back: " + "; ".join(problems))
            else:
                row["compare_error"] = "the test was cut off by a restart; everything was put back"
            done.append(role)
    if done:
        save_state(state)
    return done


async def decide(role: str, action: str) -> dict:
    """``update``: rebuild the bakes from the new weights. ``stay``: restore
    the pinned weights under the tag and mute this remote digest."""
    from src.config import settings
    from src.services.setup_wizard import build_ollama_models
    endpoint = settings.effective_ollama
    state = load_state()
    row = state.get(role) or {}
    cmp_ = row.get("comparison")
    if not row.get("running"):
        raise ValueError(f"nothing known about the {role} model")
    # "stay" also works after a test that broke off: the pin is named by
    # the running digest, so it can be found without a finished result.
    if action == "update" and not cmp_:
        raise ValueError("compare first")
    base = row["base"]
    pinned = cmp_["pinned_as"] if cmp_ else pin_name(base, row["running"])
    if action == "update":
        results = await build_ollama_models(
            endpoint, settings.BASE_CURATOR_MODEL, settings.BASE_SUMMARIZER_MODEL,
            settings.BASE_PITCHER_MODEL if getattr(settings, "PITCHER_MODEL", "") else None)
        # The result was ignored: a failed rebake was shown as "current" and
        # the pin — then the only copy of the old weights — was deleted.
        failed = [k for k in ("curator", "summarizer", "pitcher") if (results or {}).get(k) is False]
        if failed:
            err = (f"the update did not finish: rebuilding {', '.join(failed)} failed — "
                   f"the pin {pinned} is kept; try again or stay")
            logger.error("[model update] %s: %s", role, err)
            row["compare_error"] = err
            state[role] = row
            save_state(state)
            raise RuntimeError(err)
        row.update(running=cmp_["new_weights"], status="current", declined_remote=None)
        row.pop("compare_error", None)
        # The old weights go with the pin once no bake uses them any more.
        for name in (pinned, TEST_BAKE):
            await _ollama(endpoint, "DELETE", "/api/delete", json={"model": name})
            if await running_weights(endpoint, name) is not None:
                # The update itself stands; only the disk is not freed.
                logger.warning("[model update] %s could not be deleted after the update — "
                               "remove it by hand (ollama rm %s)", name, name)
    elif action == "stay":
        problems = await rollback(endpoint, row)
        row.update(status="pinned", declined_remote=row.get("remote"))
        if problems:
            # What runs IS the pinned weights (the bakes carry them); the
            # tag or the disk is what could not be put back — said, not hidden.
            logger.warning("[model update] staying on the pinned %s, but: %s", role, "; ".join(problems))
            row["compare_error"] = "staying on the pinned version, but " + "; ".join(problems)
        else:
            row.pop("compare_error", None)
    else:
        raise ValueError("action is update or stay")
    row["decided"] = {"action": action, "at": _now(),
                      "summary": (cmp_ or {}).get("summary")}
    row.pop("comparison", None)
    state[role] = row
    save_state(state)
    return row


def public_view(state: dict) -> list[dict]:
    """What the UI needs: no raw texts beyond the side-by-side sample."""
    out = []
    for role in ("curator", "pitcher", "summarizer"):
        r = state.get(role)
        if not r:
            continue
        cmp_ = r.get("comparison")
        out.append({k: r.get(k) for k in ("role", "base", "bake", "status", "running", "remote",
                                          "checked_at", "comparable", "decided", "compare_error",
                                          "eval_error", "history")}
                   | ({"comparison": {
                       "summary": cmp_.get("summary") or {}, "at": cmp_.get("at"),
                       "evaluation": cmp_.get("evaluation"),
                       "facts_rebuilt": cmp_.get("facts_rebuilt", False),
                       "pairs": [{"title": a.get("title"), "old": a.get("monologue"),
                                  "new": b.get("monologue"), "old_verdict": a.get("verdict"),
                                  "new_verdict": b.get("verdict")}
                                 for a, b in zip((cmp_.get("old") or {}).get("titles") or [],
                                                 (cmp_.get("new") or {}).get("titles") or [])],
                       "chat": [{"prompt": a.get("prompt"), "old": a.get("text"), "new": b.get("text")}
                                for a, b in zip((cmp_.get("old") or {}).get("chat") or [],
                                                (cmp_.get("new") or {}).get("chat") or [])]}}
                      if cmp_ else {}))
    return out
