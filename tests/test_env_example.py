""".env.example is the configuration reference, so it must match src/config.py.

The README sends everyone who configures by hand to .env.example, and its
header promises "every setting, defaults mirror src/config.py". By 2026-10
fourteen settings had been added to config.py without reaching it, and a
documentation pass nearly shipped a blank ``OLLAMA_USE_MMAP=`` line: copied
to .env, that blank boolean fails validation and the server never starts.

Pins:
  - a copy of the template loads through Settings unchanged
  - every setting appears in it (set or commented out), and nothing else does
  - a value it does set is config.py's default, unless it is a placeholder
    address to replace or blank (= unset)

    python tests/test_env_example.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.config import Settings

EXAMPLE = ROOT / ".env.example"
text = EXAMPLE.read_text(encoding="utf-8")
set_keys = dict(re.findall(r"^([A-Z][A-Z0-9_]*)=(.*)$", text, re.M))
mentioned = set(re.findall(r"^#?([A-Z][A-Z0-9_]*)=", text, re.M))

# Not settings an operator configures: the app's own identity, and aliases
# config.py still reads from .env files written by very old versions.
NOT_DOCUMENTED = {
    "APP_NAME", "VERSION",
    "PLEX_BASE_URL", "PLEX_AUTH_TOKEN", "OLLAMA_BASE_URL", "OLLAMA_MODEL",
    "LLM_MODEL", "LOG_LEVEL", "DEBUG_MODE", "MAX_RECOMMENDATIONS",
    "RECOMMENDATION_SCORE_THRESHOLD", "WATCH_HISTORY_RETENTION_DAYS",
    "WATCH_HISTORY_BATCH_SIZE", "MDBLIST_API_KEY",
}
# Example addresses the reader is meant to replace.
PLACEHOLDERS = {"PLEX_URL", "RADARR_URL", "SONARR_URL", "LIDARR_URL"}

fields = set(Settings.model_fields)

try:
    loaded = Settings(_env_file=str(EXAMPLE))
    err = ""
except Exception as e:  # noqa: BLE001 - the failure IS the finding; report it as one
    loaded, err = None, f"{type(e).__name__}: {e}"
check(f"a .env copied from .env.example loads {err}".rstrip(), loaded is not None)

missing = sorted(fields - NOT_DOCUMENTED - mentioned)
check(f"every setting is in .env.example (missing: {missing})", not missing)

unknown = sorted(mentioned - fields)
check(f"every key in .env.example is a setting (unknown: {unknown})", not unknown)

if loaded is not None:
    defaults = Settings(_env_file=None)
    drift = sorted(
        k for k, raw in set_keys.items()
        if k in fields and k not in PLACEHOLDERS and raw.strip()
        and str(getattr(loaded, k)) != str(getattr(defaults, k)))
    check(f"values it sets are config.py's defaults (differ: {drift})", not drift)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
