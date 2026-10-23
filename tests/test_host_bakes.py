"""Bakes per host, and the embedding weights a move must keep (2026-10-22,
NAS move phase 2, PR 5).

A split install runs the same bake names on the NAS and on the gaming PC,
each built from a base that fits its card: build_models.py --host main|pc.
The pitcher lives on the PC only (deletion runs wait for it), only the main
host embeds. --check looks at both over HTTP (the container has no ollama
binary) and compares the main host's embedding weights with the ones the
stored vectors were made with - vectors from other weights do not compare,
even under the same model name.

Fakes only: no Ollama is called.

    python tests/test_host_bakes.py
"""
import contextlib
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services import embed_service as es  # noqa: E402
from src.services import model_check as mc  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


KEYS = ("OLLAMA_PC_ENDPOINT", "PITCHER_MODEL", "BASE_CURATOR_MODEL", "BASE_SUMMARIZER_MODEL",
        "BASE_PITCHER_MODEL", "PC_BASE_CURATOR_MODEL", "PC_BASE_SUMMARIZER_MODEL",
        "PC_BASE_PITCHER_MODEL", "PC_LLM_PROFILE", "PC_CURATOR_NUM_CTX")
saved = {k: getattr(settings, k) for k in KEYS}
saved_prof = (es.get_profile, es.effective_embedding_model, es.record_digest)
EMB = "nomic-embed-text-v2-moe"
es.effective_embedding_model = lambda: EMB

try:
    settings.PITCHER_MODEL = "curatarr-pitcher"
    settings.BASE_CURATOR_MODEL, settings.BASE_SUMMARIZER_MODEL = "gemma4:26b-a4b-it-qat", "granite4.1:8b"
    settings.BASE_PITCHER_MODEL = "qwen3.8:27b"
    settings.PC_BASE_CURATOR_MODEL, settings.PC_BASE_SUMMARIZER_MODEL = "gemma4:31b", "granite4.1:8b"
    settings.PC_BASE_PITCHER_MODEL = "qwen3.8:27b"

    # ── not split: one host, everything on it (as before) ───────────────────
    settings.OLLAMA_PC_ENDPOINT = ""
    m = mc.host_plan("main")
    check("not split: the one host gets curator, summariser, pitcher and the embedder",
          m["url"] == settings.effective_ollama and m["pitcher"] == "qwen3.8:27b"
          and m["embedding"] == EMB
          and m["models"] == ["curatarr-curator", "curatarr-summarizer", "curatarr-pitcher", EMB])
    try:
        mc.host_plan("pc")
        check("--host pc without a PC endpoint is an error", False)
    except ValueError:
        check("--host pc without a PC endpoint is an error", True)

    # ── split ───────────────────────────────────────────────────────────────
    settings.OLLAMA_PC_ENDPOINT = "http://pc:11434"
    settings.PC_LLM_PROFILE, settings.PC_CURATOR_NUM_CTX = "large", 0
    m, p = mc.host_plan("main"), mc.host_plan("pc")
    check("split, main host: its own bases, the embedder, no pitcher (deletions wait for the PC)",
          m["curator"] == "gemma4:26b-a4b-it-qat" and m["pitcher"] is None and m["embedding"] == EMB
          and m["models"] == ["curatarr-curator", "curatarr-summarizer", EMB])
    check("split, PC: the PC's bases, its window, the pitcher, no embedder",
          p["url"] == "http://pc:11434" and p["curator"] == "gemma4:31b" and p["num_ctx"] == 16384
          and p["pitcher"] == "qwen3.8:27b" and p["embedding"] is None
          and p["models"] == ["curatarr-curator", "curatarr-summarizer", "curatarr-pitcher"])
    settings.PITCHER_MODEL = ""
    check("no pitcher configured: none on either host",
          mc.host_plan("pc")["pitcher"] is None and "curatarr-pitcher" not in mc.host_plan("pc")["models"])
    settings.PITCHER_MODEL = "curatarr-pitcher"

    # ── /api/tags over HTTP ─────────────────────────────────────────────────
    def fake_get(models=None, fail=False):
        def get(url, timeout=None):
            if fail:
                raise OSError("refused")
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"models": models or []})
        return get

    tags = mc.host_tags("http://nas:30068", fake_get([
        {"name": "curatarr-curator:latest", "digest": "aaa"},
        {"name": f"{EMB}:latest", "digest": "ff9c2f10ef5e"}]))
    check("tags carry digests, and 'x:latest' also answers as 'x'",
          tags["curatarr-curator"] == "aaa" and tags[EMB] == "ff9c2f10ef5e")
    check("an Ollama that does not answer is None, not 'everything missing'",
          mc.host_tags("http://nas:30068", fake_get(fail=True)) is None
          and mc.missing_on("http://nas:30068", ["x"], fake_get(fail=True)) is None)
    check("missing models are named",
          mc.missing_on("http://nas:30068", ["curatarr-curator", "curatarr-summarizer"],
                        fake_get([{"name": "curatarr-curator:latest"}])) == ["curatarr-summarizer"])

    # ── the embedding weights ───────────────────────────────────────────────
    prof = {"model": EMB}
    es.get_profile = lambda: prof
    recorded = []
    es.record_digest = lambda d: (recorded.append(d), prof.__setitem__("digest", d))
    on_host = fake_get([{"name": f"{EMB}:latest", "digest": "ff9c2f10ef5e"}])

    check("first check on the old machine: unrecorded",
          mc.embedding_identity(on_host)["status"] == "unrecorded")
    ident = mc.record_embedding_digest(on_host)
    check("...and --check records which weights the vectors come from",
          recorded == ["ff9c2f10ef5e"] and ident["status"] == "match")
    check("the same weights on the new host: match", mc.embedding_identity(on_host)["status"] == "match")
    other = fake_get([{"name": f"{EMB}:latest", "digest": "0a109f422b47"}])
    check("other weights under the same name: MISMATCH (vectors would not compare)",
          mc.embedding_identity(other)["status"] == "mismatch")
    check("not pulled yet: missing", mc.embedding_identity(fake_get([]))["status"] == "missing")
    check("host down: unreachable", mc.embedding_identity(fake_get(fail=True))["status"] == "unreachable")
    n = len(recorded)
    mc.record_embedding_digest(other)
    check("a mismatch is never overwritten by recording", len(recorded) == n)
finally:
    for k, v in saved.items():
        setattr(settings, k, v)
    es.get_profile, es.effective_embedding_model, es.record_digest = saved_prof

# ── record_digest keeps the profile, flips nothing ──────────────────────────
import src.services.app_state as app_state  # noqa: E402
store = {es.PROFILE_KEY: json.dumps({"model": EMB, "collection": "media_knowledge_v2",
                                     "prefixes": True, "num_ctx": 512, "schema": 2})}
saved_state = (app_state.get_state, app_state.set_state)
app_state.get_state = lambda k: store.get(k)
app_state.set_state = lambda k, v: store.__setitem__(k, v)
es._profile_cache["value"] = None
try:
    es.record_digest("ff9c2f10ef5e")
    p = json.loads(store[es.PROFILE_KEY])
    check("record_digest adds the digest and keeps model, collection and prefixes",
          p["digest"] == "ff9c2f10ef5e" and p["collection"] == "media_knowledge_v2"
          and p["model"] == EMB and p["prefixes"] is True)
finally:
    app_state.get_state, app_state.set_state = saved_state
    es._profile_cache["value"] = None

# ── build_models.py ─────────────────────────────────────────────────────────
sys.argv = ["build_models.py"]
import build_models  # noqa: E402
out = io.StringIO()
with contextlib.redirect_stdout(out):
    rc = build_models.suggest(16)
check("--suggest 16 prints the bench's package for the card, with the Ollama server settings",
      rc == 0 and "gemma4:26b-a4b-it-qat" in out.getvalue()
      and "OLLAMA_KV_CACHE_TYPE" in out.getvalue())
sw = (ROOT / "src/services/setup_wizard.py").read_text(encoding="utf-8")
check("the bakes carry the HOST's curator window, the pitcher too",
      "curator_ctx = int(num_ctx or CURATOR_NUM_CTX)" in sw
      and sw.count("CURATOR_SYSTEM_PROMPT, curator_ctx") == 2)
check("a host that does not embed gets no embedding pull", "if embed_model and with_embedding:" in sw)
bm = (ROOT / "build_models.py").read_text(encoding="utf-8")
check("--check covers both hosts on a split install and records/compares the embedding weights",
      '["main", "pc"] if split() else ["main"]' in bm and "record_embedding_digest()" in bm)
bat = (ROOT / "start.bat").read_text(encoding="utf-8")
check("a weights mismatch is exit 3: start.bat warns instead of rebuilding on every start",
      "rc = 3 if rc == 0 else rc" in bm and 'if errorlevel 3 set "_MODELS_RC=3"' in bat
      and '"%_MODELS_RC%"=="3"' in bat)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
