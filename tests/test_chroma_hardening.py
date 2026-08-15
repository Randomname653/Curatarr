"""Tests for the ChromaDB ops hardening (evaluation package 4):
startup quick_check, single-process lock, fallback counters.

    python tests/test_chroma_hardening.py
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.vector_store.chromadb_wrapper as cw

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── init runs quick_check + takes the lock (live store) ──────────────────────

w = cw.ChromaDBWrapper()
check("wrapper init passes quick_check on the live store", w is not None)
check("process lock held", cw._lock_handle is not None)
check("re-init in the SAME process is fine (lock is per-process)",
      cw.ChromaDBWrapper() is not None)

# ── a SECOND process must be refused ─────────────────────────────────────────

code = (
    "import sys; sys.path.insert(0, r'" + str(ROOT) + "');\n"
    "from src.vector_store.chromadb_wrapper import ChromaDBWrapper\n"
    "ChromaDBWrapper()\n"
)
proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                      text=True, timeout=120)
check("second process is refused while we hold the lock",
      proc.returncode != 0 and "another process" in (proc.stderr + proc.stdout))

# ── fallback counters ────────────────────────────────────────────────────────

start = cw._fallback_counts.get("query", 0)
for _ in range(3):
    cw._count_fallback("query")
check("fallback counter counts", cw._fallback_counts["query"] == start + 3)
check("warn threshold configured", cw._FALLBACK_WARN_EVERY == 25)

# ── wiring asserts ───────────────────────────────────────────────────────────

src = (ROOT / "src/vector_store/chromadb_wrapper.py").read_text(encoding="utf-8")
check("query fallback counted", '_count_fallback("query")' in src)
check("get_by_id fallback counted", '_count_fallback("get")' in src)
check("quick_check fails loudly with the restore path",
      "quick_check" in src and "embedding-migration" in src)
check("lock releases with the process (OS lock, no stale lockfiles)",
      "msvcrt" in src and "LK_NBLCK" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
