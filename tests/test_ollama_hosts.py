"""Every model call names its role, and curator calls take one host
(2026-10-22, NAS move phase 2).

With two Ollama hosts the routing decides per KIND of work, so each call
site names its role (ollama_for / host_for). With one endpoint (every
install today) every role is settings.effective_ollama - no behaviour
change. Model MANAGEMENT (setup, updates, the VRAM choreography, the
game-mode unload) still reads the endpoint itself until it loops over the
hosts; that list stays closed here.

A host also carries its card's profile: the curator window and the judge's
constitution (large on the PC, small on the NAS). A curator call takes the
host once and sends that host's options to that host's url, so window,
prompt variant and machine never come from two places.

    python tests/test_ollama_hosts.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services import llm_utils as lu  # noqa: E402
from src.services.ollama_hosts import ROLES, OllamaHost, host_for, ollama_for  # noqa: E402

from _checks import check  # noqa: E402


# ── one endpoint: every role is it ──────────────────────────────────────────
check("every role answers the configured endpoint (no behaviour change)",
      all(ollama_for(r) == settings.effective_ollama for r in ROLES))
saved = settings.OLLAMA_ENDPOINT
try:
    settings.OLLAMA_ENDPOINT = "http://10.0.0.9:11434/"
    check("...read at call time, not cached at import", ollama_for("chat") == "http://10.0.0.9:11434")
finally:
    settings.OLLAMA_ENDPOINT = saved
try:
    ollama_for("summarizer")
    check("an unknown role is an error, not a silent default", False)
except ValueError as e:
    check("an unknown role is an error, not a silent default", "chat" in str(e))

h = host_for("judge")
check("the single host carries this process's profile, window and placement (as before)",
      h.url == settings.effective_ollama and h.profile == lu.llm_profile()
      and h.num_ctx == lu.CURATOR_NUM_CTX and h.gpu == lu.curator_gpu())

# ── a host's profile reaches the request and the prompt ─────────────────────
nas = OllamaHost(url="http://nas:30068", profile="small", num_ctx=8192, gpu={})
pc = OllamaHost(url="http://pc:11434", profile="large", num_ctx=16384, gpu={"num_gpu": 99})
o_nas = lu.curator_options(0.7, 100, host=nas)["options"]
o_pc = lu.curator_payload([], host=pc)["options"]
check("curator options take the host's window", o_nas["num_ctx"] == 8192 and o_pc["num_ctx"] == 16384)
check("...and its placement ({} = Ollama decides, no num_gpu sent)",
      "num_gpu" not in o_nas and o_pc["num_gpu"] == 99)
check("without a host: this process's defaults, exactly as before",
      lu.curator_options(0.7, 100)["options"]["num_ctx"] == lu.CURATOR_NUM_CTX)

from src.services import pillars  # noqa: E402
check("the judge's constitution follows the host's profile",
      pillars.judge_system_prompt("", "small").startswith(pillars.PILLAR_CONSTITUTION_COMPACT[:60])
      and pillars.judge_system_prompt("", "large").startswith(pillars.PILLAR_CONSTITUTION[:60]))

# ── the call sites ──────────────────────────────────────────────────────────
src_files = {p.relative_to(ROOT).as_posix(): p.read_text(encoding="utf-8")
             for p in (ROOT / "src").rglob("*.py")}

# Model management, not model use: gets a loop over the hosts with the
# second one. Nothing else may read the endpoint directly.
MANAGEMENT = {
    "src/config.py",                     # defines it
    "src/services/ollama_hosts.py",      # the one reader for model use
    "src/routers/setup.py",              # wizard: test / recommend / bake the endpoint
    "src/services/setup_wizard.py",      # shows the configured endpoint
    "src/services/model_updates.py",     # registry checks, test bakes, rollback
    "src/services/model_check.py",       # "does the curator fit this card"
    "src/services/process_monitor.py",   # game mode: what is loaded, unload all
}
direct = sorted(f for f, text in src_files.items()
                if re.search(r"\beffective_ollama\b", text) and f not in MANAGEMENT)
check(f"model calls go through ollama_for / host_for (direct readers: {direct or 'none'})", not direct)

ROUTER = r"(?:ollama_for|host_for)"
used = {m for text in src_files.values() for m in re.findall(ROUTER + r'\(\s*"(\w+)"', text)}
used |= {m for text in src_files.values()
         for m in re.findall(ROUTER + r'\(\s*"\w+" if [^)]* else "(\w+)"', text)}
check(f"every role a call site names exists (used: {sorted(used)})", used and used <= set(ROLES))
check("the roles the policy needs are all in use", {"chat", "judge", "enrich", "embed", "curator"} <= used)


def roles_in(path):
    calls = re.findall(ROUTER + r"\(([^)]*)\)", src_files[path])
    return set(re.findall(r'"(\w+)"', " ".join(calls)))


check("the deletion judge and its pitches are 'judge'", roles_in("src/services/pillars.py") == {"judge"})
check("embeddings are 'embed'", roles_in("src/services/embed_service.py") == {"embed"})
check("the chat router is 'chat'", roles_in("src/routers/chat.py") == {"chat"})
check("enrichment is 'enrich'", roles_in("src/services/media_enricher.py") == {"enrich"})
check("the recommendation chain is 'judge' only for a pitch bake",
      'host_for("judge" if model_override else "curator")' in src_files["src/services/recommendations_engine.py"])


def calls(text, name):
    """Every ``name(...)`` call's argument text, parentheses balanced."""
    out = []
    for m in re.finditer(r"\b" + name + r"\(", text):
        depth, i = 1, m.end()
        while depth and i < len(text):
            depth += {"(": 1, ")": -1}.get(text[i], 0)
            i += 1
        out.append(text[m.end():i - 1])
    return out


# Where a curator call is built, it must name the host it goes to - else
# its window and placement are this process's defaults while its url may
# be another machine. Management code keeps the defaults (one host there).
HOSTLESS_OK = MANAGEMENT | {"src/services/llm_utils.py", "src/services/model_update_eval.py",
                            "src/services/setup_minibench.py"}
hostless = sorted(f"{f}: {name}" for f, text in src_files.items() if f not in HOSTLESS_OK
                  for name in ("curator_options", "curator_payload")
                  for args in calls(text, name) if "host=" not in args)
check(f"every curator call outside model management names its host ({hostless or 'all do'})",
      not hostless)
window_readers = sorted(f for f, text in src_files.items() if f not in HOSTLESS_OK
                        and re.search(r"\bCURATOR_NUM_CTX\b", re.sub(r"#.*", "", text)))
check(f"...and budgets with that host's window, not the process default ({window_readers or 'none'})",
      not window_readers)
check("the deletion run resolves its host once, like its bake",
      'judge_host = host_for("judge")' in src_files["src/services/recommendations_engine.py"]
      and src_files["src/services/recommendations_engine.py"].count("host=judge_host") >= 5)

check.done()
