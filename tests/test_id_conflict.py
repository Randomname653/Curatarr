"""Corrupt-source-id detector (SoulSync dedupe_source_ids port, MIT).

The poison class: ONE external id (tmdb/anilist) held by differently-
named entities — a wrong resolution leaked across items (the Batman
Beyond profile carried a foreign work's identity for a year). The audit
now collects (category, id_field, id_value) -> rows during its normal
scan and requeues WHOLE clusters whose titles genuinely diverge;
re-resolution runs with today's guards plus any owner pin.

    python tests/test_id_conflict.py
"""
import difflib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def _norm_ct(t):
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", (t or "").lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def _conflict(a, b):
    return difflib.SequenceMatcher(None, _norm_ct(a), _norm_ct(b)).ratio() < 0.8


check("genuinely different works on one id -> conflict",
      _conflict("Batman Beyond (1999)", "Devil of Darkness"))
check("year suffix is not a conflict",
      not _conflict("Panic Room (2002)", "Panic Room"))
check("regional variant is not a conflict",
      not _conflict("The Office", "The Office (US)"))
check("punctuation drift is not a conflict",
      not _conflict("Akira", "AKIRA!"))

en = (Path(__file__).resolve().parents[1] / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("audit collects id->rows during its normal scan (no extra pass)",
      "_id_rows.setdefault((category, _src, str(_v))" in en)
check("clusters requeue through the EXISTING hits machinery",
      'reason = f"id_conflict:{_src}"' in en
      and "hits.append((cache_key, prk, title, _cat, reason))" in en)
check("similar titles are guarded (ratio >= 0.8 = alternate form, not a leak)",
      ".ratio() >= 0.8" in en)
check("conflicts are logged with the colliding names",
      "corrupt source id" in en)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
