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
    from src.services.app_state import get_state
    try:
        return json.loads(get_state(STATE_KEY) or "{}")
    except ValueError:
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
    need = 0
    if coords:
        ns, name, tag = coords
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
                r = await c.get(f"{REGISTRY}/v2/{ns}/{name}/manifests/{tag}", headers={
                    "Accept": "application/vnd.docker.distribution.manifest.v2+json"})
            need = sum(int(l.get("size") or 0) for l in r.json().get("layers") or []) / 1e9
        except (httpx.HTTPError, ValueError):
            pass
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


async def _residency(endpoint: str, model: str) -> dict:
    try:
        r = await _ollama(endpoint, "GET", "/api/ps")
        for m in r.json().get("models") or []:
            if (m.get("name") or m.get("model") or "").split(":")[0] == model.split(":")[0]:
                size, vram = m.get("size") or 0, m.get("size_vram") or 0
                return {"vram_gb": round(vram / 2**30, 1),
                        "gpu_share_pct": round(100 * vram / size) if size else None,
                        "context": m.get("context_length")}
    except (httpx.HTTPError, ValueError):
        pass
    return {}


async def _speed(endpoint: str, model: str) -> dict:
    from src.services.llm_utils import curator_options
    r = await _ollama(endpoint, "POST", "/api/generate", json={
        "model": model, "stream": False, "keep_alive": "5m",
        "prompt": "Describe, in about 120 words, what a film archivist does all day.",
        **{k: v for k, v in curator_options(0.0, 160).items()}})
    d = r.json() if r.status_code == 200 else {}
    ec, ed = d.get("eval_count"), d.get("eval_duration")
    pc, pd = d.get("prompt_eval_count"), d.get("prompt_eval_duration")
    return {"gen_tok_s": round(ec / (ed / 1e9), 1) if ec and ed else None,
            "prefill_tok_s": round(pc / (pd / 1e9), 1) if pc and pd else None,
            "load_s": round((d.get("load_duration") or 0) / 1e9, 1)}


async def sample_evidence(user_id: int, n: int = SAMPLE_TITLES) -> list[dict]:
    """Evidence for n real titles, built ONCE so both models judge the same
    bytes: the latest deletion-proposal titles first, then the rest of the
    library spread across categories."""
    import random
    from src.database.connection import get_db_session
    from src.database.models import DeletionProposal
    from src.routers.recommendations import _fetch_arr_candidates
    from src.services import pillars
    with get_db_session() as db:
        recent = [(p.category, p.title) for p in db.query(DeletionProposal)
                  .order_by(DeletionProposal.created_at.desc()).limit(40)]
    items = await _fetch_arr_candidates()
    by_key = {((i.get("category") or ""), (i.get("title") or "").lower()): i for i in items}
    picked, seen = [], set()
    for cat, title in recent:
        it = by_key.get((cat or "", (title or "").lower()))
        if it and it["title"] not in seen:
            picked.append(it)
            seen.add(it["title"])
    rng = random.Random(len(items))
    rest = [i for i in items if i.get("title") not in seen]
    rng.shuffle(rest)
    cats = ["movie", "show", "anime", "music"]
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
    from src.services.llm_utils import curator_options
    rows = []
    for s in sample:
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
    return {"titles": rows, "chat": chats, "speed": await _speed(endpoint, model),
            "residency": await _residency(endpoint, model)}


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
        await rollback(endpoint, row)
        _clear_marker(role)
        raise
    _clear_marker(role)
    return result


def _clear_marker(role: str) -> None:
    st = load_state()
    st.get(role, {}).pop("test_started", None)
    save_state(st)


async def _compare(role, row, endpoint, user_id, pinned, msg) -> dict:
    from src.database.connection import get_db_session
    from src.services.llm_priority import curator_priority, evict_if_resident
    from src.services.llm_utils import detect_user_language, language_directive
    from src.services.setup_wizard import pull_ollama_model
    base, bake, running = row["base"], row["bake"], row["running"]
    if await running_weights(endpoint, base) == running:
        msg(f"pinning {base} as {pinned}")
        await _ollama(endpoint, "POST", "/api/copy", json={"source": base, "destination": pinned})
    elif await running_weights(endpoint, pinned) != running:
        # The tag already moved (pulled by hand): the bake is the only copy
        # of the running weights left, so the pin is taken from it.
        await _ollama(endpoint, "POST", "/api/copy", json={"source": bake, "destination": pinned})
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
    sample = await sample_evidence(user_id)
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
              "at": _now(), "summary": summarize(old, new), "old": old, "new": new}
    state = load_state()
    state.setdefault(role, row)["comparison"] = result
    save_state(state)
    return result


async def rollback(endpoint: str, row: dict) -> None:
    """Put Ollama back to where it was before a test: the tag on the pinned
    weights, no test bake, no pin. Deleting the test bake is what frees the
    new weights: Ollama removes every blob no manifest references any more
    (checked 2026-10-15 with a throwaway model). A pull cut off mid-way
    leaves ``-partial`` files that no manifest knows; with Ollama on this
    machine they are removed too."""
    base, running = row["base"], row["running"]
    pinned = pin_name(base, running)
    pin_ok = await running_weights(endpoint, pinned) == running
    if pin_ok and await running_weights(endpoint, base) != running:
        await _ollama(endpoint, "POST", "/api/copy", json={"source": pinned, "destination": base})
    await _ollama(endpoint, "DELETE", "/api/delete", json={"model": TEST_BAKE})
    # The pin goes only once the tag holds its weights again — otherwise it
    # is the last copy of the running weights and must stay.
    if pin_ok and await running_weights(endpoint, base) == running:
        await _ollama(endpoint, "DELETE", "/api/delete", json={"model": pinned})
    remove_partials(endpoint, row.get("remote"))


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
                await rollback(settings.effective_ollama, row)
            except Exception as e:
                logger.warning("[model update] rollback of %s failed: %s", role, e)
                continue
            row.pop("test_started", None)
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
        await build_ollama_models(endpoint, settings.BASE_CURATOR_MODEL,
                                  settings.BASE_SUMMARIZER_MODEL,
                                  settings.BASE_PITCHER_MODEL if getattr(settings, "PITCHER_MODEL", "") else None)
        row.update(running=cmp_["new_weights"], status="current", declined_remote=None)
        # The old weights go with the pin once no bake uses them any more.
        for name in (pinned, TEST_BAKE):
            await _ollama(endpoint, "DELETE", "/api/delete", json={"model": name})
    elif action == "stay":
        await rollback(endpoint, row)
        row.update(status="pinned", declined_remote=row.get("remote"))
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
                                          "checked_at", "comparable", "decided", "compare_error")}
                   | ({"comparison": {
                       "summary": cmp_["summary"], "at": cmp_["at"],
                       "pairs": [{"title": a["title"], "old": a["monologue"], "new": b["monologue"],
                                  "old_verdict": a["verdict"], "new_verdict": b["verdict"]}
                                 for a, b in zip(cmp_["old"]["titles"], cmp_["new"]["titles"])],
                       "chat": [{"prompt": a["prompt"], "old": a["text"], "new": b["text"]}
                                for a, b in zip(cmp_["old"]["chat"], cmp_["new"]["chat"])]}}
                      if cmp_ else {}))
    return out
