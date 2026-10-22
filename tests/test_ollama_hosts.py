"""Every model call asks ollama_for(role) (2026-10-22, NAS move phase 2).

With two Ollama hosts the routing decides per KIND of work, so each call
site names its role. With one endpoint (every install today) every role is
settings.effective_ollama - no behaviour change. Model MANAGEMENT (setup,
updates, the VRAM choreography, the game-mode unload) still reads the
endpoint itself until it loops over the hosts; that list stays closed here.

    python tests/test_ollama_hosts.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.services.ollama_hosts import ROLES, ollama_for  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


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
    "src/services/llm_priority.py",      # what is loaded, evict to make room
    "src/services/process_monitor.py",   # game mode: what is loaded, unload all
}
direct = sorted(f for f, text in src_files.items()
                if re.search(r"\beffective_ollama\b", text) and f not in MANAGEMENT)
check(f"model calls go through ollama_for (direct readers: {direct or 'none'})", not direct)

used = {m for text in src_files.values() for m in re.findall(r'ollama_for\(\s*"(\w+)"', text)}
used |= {m for text in src_files.values()
         for m in re.findall(r'ollama_for\(\s*"\w+" if [^)]* else "(\w+)"', text)}
check(f"every role a call site names exists (used: {sorted(used)})", used and used <= set(ROLES))
check("the roles the policy needs are all in use", {"chat", "judge", "enrich", "embed", "curator"} <= used)


def roles_in(path):
    return set(re.findall(r'"(\w+)"', " ".join(re.findall(r"ollama_for\(([^)]*)\)", src_files[path]))))


check("the deletion judge and its pitches are 'judge'", roles_in("src/services/pillars.py") == {"judge"})
check("embeddings are 'embed'", roles_in("src/services/embed_service.py") == {"embed"})
check("the chat router is 'chat'", roles_in("src/routers/chat.py") == {"chat"})
check("enrichment is 'enrich'", roles_in("src/services/media_enricher.py") == {"enrich"})
check("the recommendation chain is 'judge' only for a pitch bake",
      'ollama_for("judge" if model_override else "curator")' in src_files["src/services/recommendations_engine.py"])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
