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

# -- what the judge is given to reason FROM -------------------------------
# A four-round argument to save one title traced back to three separate
# causes, only one of which was the model: the source novelist was never
# fetched, the significance field held a Wikipedia cast list, and the
# engagement line measured three episodes against a two-season total.

from src.services.media_enricher import _looks_like_cast_list, _SOURCE_JOBS

CAST_DUMP = ("Hugh Laurie as Richard Roper, a charismatic but ruthless arms "
             "dealer Olivia Colman as Angela Burr, head of the agency "
             "Tom Hollander as Major Corkoran, Roper's second in command")
check("a Wikipedia cast section is not significance",
      _looks_like_cast_list(CAST_DUMP))
check("prose that names one performance still counts",
      not _looks_like_cast_list(
          "Hugh Laurie as Richard Roper anchors it, a career-best turn."))
check("real significance is untouched",
      not _looks_like_cast_list(
          "Widely regarded as a landmark of the genre; won two Primetime Emmys."))
check("nothing claimed about empty text", not _looks_like_cast_list(""))

check("the source of an adaptation has its own credit jobs",
      "Novel" in _SOURCE_JOBS and "Theatre Play" in _SOURCE_JOBS)
check("a novel outranks a generic story credit",
      _SOURCE_JOBS.index("Novel") < _SOURCE_JOBS.index("Story"))

_src = (Path(__file__).resolve().parents[1] / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("the novelist reaches the verified block, separate from the screenwriter",
      'add("Creator/Writer"' in _src and "Adapted from" in _src)
check("...and is carried through the raw -> verified mapping",
      '"source_author":  raw.get("source_author")' in _src)

_pil = (Path(__file__).resolve().parents[1] / "src/services/pillars.py").read_text(encoding="utf-8")
check("episode counts are reported with the season they sit in",
      "seasons_watched" in _pil and "all of the owner's plays are in season" in _pil)

_rec = (Path(__file__).resolve().parents[1] / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("reception is warmed BEFORE the verdict, not only in the discussion",
      "topup_reception" in _rec and "pre-judge warm-up" in _rec)

# -- an answer is only as good as the rules that produced it ---------------
# A cached significance value was a verbatim cast list. Re-running today's
# prompt on the same article produced the awards three times out of three:
# the distiller was not the problem, the entry was three months old and
# "checked" meant "never again". Distillations now carry the version of the
# prompt behind them, derived from its text so an edit retires old answers
# without anyone remembering to bump a constant.

from src.services.media_enricher import (
    _DISAMBIG, _SIG_LEAD_CHARS, _SIG_PROMPT_VERSION, _SIG_RETRIEVAL_VERSION,
    _SIGNIFICANCE_PROMPT)

# A perfect prompt over the wrong article yields a confident "NONE" — the
# walker read the page on the Birmingham street gang and reported that the
# 2013 series has no documented significance. So the stamp covers the rules
# that CHOSE the article as well, and tightening them retires those answers.
check("the version is derived from the prompt AND the retrieval rules",
      _SIG_PROMPT_VERSION == __import__("hashlib").sha1(
          (_SIGNIFICANCE_PROMPT + _SIG_RETRIEVAL_VERSION).encode("utf-8")
      ).hexdigest()[:8])
check("editing the prompt would change the version",
      __import__("hashlib").sha1(
          (_SIGNIFICANCE_PROMPT + " ").encode("utf-8")).hexdigest()[:8]
      != _SIG_PROMPT_VERSION)
check("the template still renders both slots",
      "{title}" in _SIGNIFICANCE_PROMPT and "{extract}" in _SIGNIFICANCE_PROMPT)
check("...and the rules survived the extraction",
      "NOT significance" in _SIGNIFICANCE_PROMPT
      and "cast or crew names" in _SIGNIFICANCE_PROMPT)

_me = (Path(__file__).resolve().parents[1] / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("a checked entry is re-examined when the rules have moved on",
      'raw.get("significance_v") == _SIG_PROMPT_VERSION' in _me)
check("the walker actually offers version-stale entries again",
      "f\"%{_SIG_PROMPT_VERSION}%\"" in _me)
check("a re-check that finds nothing clears the previous text",
      'drop = ("significance",)' in _me
      and "write_fields(cache, key, raw, fields, drop=drop" in _me)

# -- choosing the right article ---------------------------------------------
# Wikipedia states what a subject IS in its opening sentence. The old check
# scanned 1,500 characters, far enough to reach a passing mention: the
# street-gang article notes the gang inspired a television series, so it
# passed as the television series.

check("plausibility is judged on the lead sentence, not the whole article",
      _SIG_LEAD_CHARS <= 400)
_me2 = (Path(__file__).resolve().parents[1]
        / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("the lead window is what the direct lookup actually uses",
      "plaus.search(cand[:_SIG_LEAD_CHARS])" in _me2)

# The guard knew one phrasing. Wikipedia opens disambiguation pages several
# ways, and Fargo and Alien were both distilled as though they were the work.
for lead in ("Alien most commonly refers to: Extraterrestrial life",
             "Fargo usually refers to: Fargo, North Dakota",
             "Foundation(s) or The Foundation(s) may refer to:",
             "Cosmos can refer to the universe"):
    check("disambiguation caught: " + repr(lead[:32]), bool(_DISAMBIG.search(lead)))
for lead in ("Blade Runner is a 1982 science fiction film by Ridley Scott",
             "The Peaky Blinders were a street gang based in Birmingham"):
    check("a real article is not mistaken for one: " + repr(lead[:30]),
          not _DISAMBIG.search(lead))

# The tri-state exists because a transient failure once became a permanent
# verdict. The search call learned that; the follow-up extracts call had not.
check("a failed extracts fetch is transient, not definitive nothing",
      "if ex is None or ex.status_code != 200:" in _me2)
check("Wikipedia is given a chance to say slow down",
      "async def _wiki_get" in _me2
      and "Retry-After" in _me2
      and _me2.count("await _wiki_get(client") == 3)

# The walker re-offers a stale stamp; the just-in-time path used to gate on the
# bare flag, so a verdict there kept an answer from rules since found wrong.
check("both paths test the stamp the same way",
      'data.get("significance_v") != _SIG_PROMPT_VERSION' in _me2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
