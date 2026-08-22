"""Guards on what the curator is allowed to claim about a title.

    python tests/test_evidence_guards.py

Each of these exists because a specific false statement reached the owner:

* a duplicate that was not there. Two library items were reported as copies
  of each other because TMDB numbers films and series in SEPARATE sequences
  and the id was grouped without its namespace — film 90 (Beverly Hills Cop)
  and series 90 (Air Crash Investigation) became "two separate copies, ~8.7 GB
  redundant" — and the size quoted belonged to the film. A deletion was
  approved on that argument. Dozens of such phantom pairs existed.
* a documentary judged as if it were a drama ("zero narrative subversion").
* LaTeX markup rendered verbatim in a pillar breakdown.
"""
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


import src.services.size_norms as sn
from src.services.llm_utils import strip_latex_artifacts
from src.services.pillars import _is_factual

# ── TMDB namespaces ─────────────────────────────────────────────────────────

check("a film id and a series id are different namespaces",
      sn._tmdb_namespace("movie") == "movie" and sn._tmdb_namespace("show") == "tv")
check("anime shares the TV namespace (TMDB has no separate one)",
      sn._tmdb_namespace("anime") == sn._tmdb_namespace("show"))


def _with_index(rows):
    """Run the duplicate lookup against a synthetic profile table."""
    from collections import defaultdict
    by = defaultdict(list)
    for tmdb, tvdb, size, mtype in rows:
        ns = sn._tmdb_namespace(mtype)
        if tmdb:
            by[("tmdb", ns, tmdb)].append(size)
        if tvdb:
            by[("tvdb", "tv", tvdb)].append(size)
    idx = {}
    for key, sizes in by.items():
        if len(sizes) > 1:
            sizes.sort(reverse=True)
            idx[key] = (len(sizes), round(sum(sizes[1:]), 1))
    saved = sn._CROSS_DUP["data"]
    sn._CROSS_DUP["data"] = idx
    return saved


# The exact live collision: one film, one series, same number, unrelated works.
_saved = _with_index([
    (90, 2288, 8953.0, "movie"),      # Beverly Hills Cop
    (90, 79771, 9151.0, "show"),      # Air Crash Investigation
    (28, None, 37000.0, "movie"),     # a real duplicate, twice over
    (28, None, 38000.0, "movie"),
])
try:
    check("a film and a series sharing a number are NOT duplicates",
          sn._cross_dup_note(90, 79771, "show") == "")
    check("...in either direction",
          sn._cross_dup_note(90, 2288, "movie") == "")
    genuine = sn._cross_dup_note(28, None, "movie")
    check("two copies of the SAME film are still reported",
          "DUPLICATE" in genuine and "2 separate library copies" in genuine)
    check("...and the redundant figure is the smaller copy, not the total",
          "37.0 GB" in genuine or "36.1 GB" in genuine)
    check("without a media type the film/series ambiguity is refused, not guessed",
          sn._cross_dup_note(90, None, None) == "")
finally:
    sn._CROSS_DUP["data"] = _saved

# ── documentary form guard ──────────────────────────────────────────────────

check("a documentary is recognised as non-fiction", _is_factual("Documentary"))
check("...also inside a genre list", _is_factual("documentary, history"))
check("drama is not", not _is_factual("Action, Drama, Thriller"))
check("reality TV is deliberately NOT covered by this guard",
      not _is_factual("Reality"))
check("missing genres do not claim anything", not _is_factual(""))

# ── LaTeX leaking into prose ────────────────────────────────────────────────

check("the arrow that actually leaked is resolved",
      strip_latex_artifacts(r"boarding $\rightarrow$ emergency")
      == "boarding → emergency")
check("unwrapped commands too",
      strip_latex_artifacts(r"bitrate \times 2") == "bitrate × 2")
check("text wrappers keep their words",
      strip_latex_artifacts(r"\textbf{PILLAR II} holds") == "PILLAR II holds")
check("a price is not a formula",
      strip_latex_artifacts("costs $5 and $10") == "costs $5 and $10")
check("prose without markup is returned untouched",
      strip_latex_artifacts("nothing to do here") == "nothing to do here")

# -- the same rule, at the second place that needs it ------------------------
# list_downscale_candidates bulk-loads MediaTechProfile rows into a dict. The
# loop it replaced looked profiles up by bare tmdb_id with .first(), i.e. the
# collision above; batching that unchanged would have baked the wrong profile
# in permanently. The router must resolve namespaces the same way size_norms
# does, from ONE definition.

from src.routers.recommendations import _namespace_for

check("the router agrees with size_norms on films",
      _namespace_for("movie") == sn._tmdb_namespace("movie") == "movie")
check("...and on series",
      _namespace_for("show") == sn._tmdb_namespace("show") == "tv")
check("...and folds anime into tv",
      _namespace_for("anime") == "tv")
check("an unknown category is NOT guessed into a namespace",
      _namespace_for(None) is None and _namespace_for("") is None)
check("a film and a series with the same id land in different buckets",
      ("movie", 90) != ("tv", 90)
      and _namespace_for("movie") != _namespace_for("show"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
