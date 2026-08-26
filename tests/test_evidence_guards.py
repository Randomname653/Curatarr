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
      and _me2.count("await _wiki_get(client") == 4)

# The walker re-offers a stale stamp; the just-in-time path used to gate on the
# bare flag, so a verdict there kept an answer from rules since found wrong.
check("both paths test the stamp the same way",
      'data.get("significance_v") != _SIG_PROMPT_VERSION' in _me2)

# -- every name of the work gets a turn -------------------------------------
# The cache row for "Frieren: Beyond Journey's End" (the library's title, which
# IS the Wikipedia article's name) carries the romanised "Sousou no Frieren"
# inside. The search ran on the inner name, the exact-match guard rightly
# refused to bridge two different names, and one of the defining anime of the
# decade was stamped "no documented significance". Verified live after the fix:
# it returns the Manga Taisho and Tezuka Prize.

_me2 = (Path(__file__).resolve().parents[1]
        / "src/services/media_enricher.py").read_text(encoding="utf-8")
import inspect as _inspect
from src.services.media_enricher import fetch_significance as _fs
check("fetch_significance accepts the work's other names",
      "also_known_as" in _inspect.signature(_fs).parameters)
check("the library's title is handed over as one",
      "also_known_as=aka" in _me2 and 'aka = (str(cache_id),)' in _me2)
check("...but a numeric cache id is an id, not a name",
      "isdigit()" in _me2.split("aka = (str(cache_id),)")[0][-300:])
check("names of OTHER works are never candidates — similar_titles is a "
      "recommendation list, not an alias list",
      "similar_titles" not in _me2.split("def fetch_significance")[1]
                                  .split("def fetch_wikipedia_summary")[0])
check("the search-hit guard accepts a match on ANY of the work's names",
      'any(_wiki_hit_matches(n, h["title"], media_type)' in _me2)

# -- the article resolved by identity, not by name --------------------------
# Wikidata's sitelink turns the IMDb id into the exact article title, which is
# what finds "The Fall Guy (2024 film)" and "Stick (TV series)" — names no
# lookup can guess. Measured on 120 titles the name path had stamped empty:
# 72 had their English article reachable this way, 6 had only a Japanese one,
# 0 only German, 42 none anywhere — so the fix for "we only use English
# Wikipedia" turned out to be identity, not language.

_me2 = (Path(__file__).resolve().parents[1]
        / "src/services/media_enricher.py").read_text(encoding="utf-8")
_wd = (Path(__file__).resolve().parents[1]
       / "src/services/wikidata.py").read_text(encoding="utf-8")
check("a sitelink resolver exists with the module's tri-state contract",
      "async def resolve_enwiki_article" in _wd
      and "return None" in _wd.split("async def resolve_enwiki_article")[1][:2400])
check("the entity's claim is verified, not trusted from search",
      'if imdb_id not in claimed:' in _wd)
check("an all-zero placeholder id joins nothing — tt0000000 exists ON "
      "Wikidata, attached to a real film",
      'if not digits.strip("0"):' in _wd)
check("significance tries the id join before any name",
      "resolve_enwiki_article" in _me2.split("def fetch_significance")[1]
                                      .split("for name in names")[0])
check("...and a resolved article skips the name guards on purpose — "
      "identity came from the id",
      'This is what finds "The Fall Guy' in _me2)
check("the id rides in from the raw entries the topup already holds",
      'imdb = next((raw.get("imdb_id")' in _me2)

# -- the transient-as-permanent family, hunted across the codebase ----------
# After reception and OMDb, an audit found four more instances of the same
# class. Each stamped or negative-cached a one-time failure as a permanent
# answer: studio/director notes for TEN years, Last.fm for 7 days on a 429,
# the Deezer resolver for 60 days on a thrown request, and the memory
# extractor advanced its cursor past windows the model never processed.

import ast as _ast

# The regression that motivates this block: a patch used cache_id inside
# topup_franchise without adding the parameter, and every call raised
# NameError — swallowed at DEBUG, invisible to the battery. It then bit a
# THIRD time in build_verified_data, where the chat path surfaced it as a
# 500. Pinned the general way, over the whole tree: any function that reads
# cache_id declares it, receives it, or builds it.
_nameerror_hits = []
for _pyf in (Path(__file__).resolve().parents[1] / "src").rglob("*.py"):
    _t = _ast.parse(_pyf.read_text(encoding="utf-8"))
    for fn in _ast.walk(_t):
        if not isinstance(fn, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        used = {n.id for n in _ast.walk(fn)
                if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Load)}
        if "cache_id" not in used:
            continue
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs
                  + fn.args.posonlyargs}
        stored = {n.id for n in _ast.walk(fn)
                  if isinstance(n, _ast.Name) and isinstance(n.ctx, _ast.Store)}
        if "cache_id" not in params | stored:
            _nameerror_hits.append(f"{_pyf.name}:{fn.lineno} {fn.name}")
check(f"every function declares the cache_id it uses (offenders: "
      f"{_nameerror_hits or 'none'})", not _nameerror_hits)

from src.services.reception import topup_franchise as _tf
import inspect as _ins
check("topup_franchise takes cache_id",
      "cache_id" in _ins.signature(_tf).parameters)

_rc2 = (Path(__file__).resolve().parents[1]
        / "src/services/reception.py").read_text(encoding="utf-8")
check("franchise: AniList silence raises instead of stamping",
      _rc2.split("async def topup_franchise")[1].count("TransientSourceError") >= 2)
check("franchise: stamps carry the logic version",
      '"relations_v": RECEPTION_LOGIC_V' in _rc2)
check("franchise: checked-but-empty docs from the stamped era are re-offered",
      'raw.get("relations_v")' in _rc2)

_sn = (Path(__file__).resolve().parents[1]
       / "src/services/studio_notes.py").read_text(encoding="utf-8")
check("studio/director notes: Wikipedia silence is not \"no article\"",
      _sn.count('return "" if answered else None') == 2)
check("studio/director notes: a busy condenser is not \"NONE\"",
      _sn.count("return None    # transient") >= 2
      and 'return ""      # definitive' in _sn)

from src.services.studio_notes import _norm as _sn_norm
check("romanisation variants are one person",
      _sn_norm("Akiyuki Shinbou") == _sn_norm("Akiyuki Shinbo")
      and _sn_norm("Gisaburou Sugii") == _sn_norm("Gisaburō Sugii"))
check("...different people stay different",
      _sn_norm("Mamoru Hosoda") != _sn_norm("Mamoru Oshii"))

_mm = (Path(__file__).resolve().parents[1]
       / "src/services/music_metadata.py").read_text(encoding="utf-8")
check("Last.fm: a non-200 is never negative-cached",
      "not \"no such artist\"" in _mm)
check("Deezer resolver: only a definitive MB answer is cached",
      "do not freeze a hiccup for 60 days" in _mm
      and "transient — MB throttled or down" in _mm)

_em = (Path(__file__).resolve().parents[1]
       / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("memory extraction reports failure instead of returning quietly",
      _em.count("return False") >= 3)
check("...and the cursor only advances past a window the model processed",
      "if not ok:" in _em
      and "extraction failed — window will be retried" in _em)

# -- the idle-evict timer dies WITH the app ---------------------------------
# Stopping the app inside the 10-60s idle window tore the loop down under a
# sleeping task, and the eviction it was about to do never ran — the curator
# squatted in VRAM until Ollama's own keep_alive expired, on the machine
# whose GPU the app was likely closed to free.

_lp = (Path(__file__).resolve().parents[1]
       / "src/services/llm_priority.py").read_text(encoding="utf-8")
check("a shutdown hand exists that cancels the timer and evicts now",
      "async def shutdown_evict" in _lp
      and "_curator_evict_task.cancel()" in
          _lp.split("async def shutdown_evict")[1][:900])
check("...and the lifespan teardown actually calls it",
      "await shutdown_evict()" in
      (Path(__file__).resolve().parents[1] / "src/main.py").read_text(encoding="utf-8"))

# -- the monologue cannot recite the owner back at them ---------------------
# OWNER TASTE is stripped from the monologue's facts, but the judge's
# governing finding rides along "for reasoning only" — soaked in taste
# language, because Pillar 0 argues against the taste line. The model obeys
# the letter (no quoting) and breaks the spirit (paraphrase: "you
# consistently demand…"), and the no-size rule leaked as "footprint on your
# disk". Shape check + one named retry, the cast-list lesson again.

from src.services.pillars import _monologue_violations as _mv
for leak in ("the narrative tension you consistently demand",
             "a palate that demands high-stakes friction",
             "incompatible with your appetite for slow cinema",
             "its 86-episode footprint on your disk",
             # the SECOND live batch's two survivors — "seek" was missing
             # from the verb list, and space-clearing talk from the size one
             "the sharp, resonant punch you typically seek",
             "serves no purpose beyond occupying space",
             # the THIRD batch routed around the word list and forced the
             # check onto the SHAPE: possessive+taste-noun, you+claim-verb
             "the psychological interplay you consistently reward",
             "defines your preferred viewing patterns",
             "the atmospheric tension your library demands",
             "defines your viewing standard",
             "a complete waste of space in a curated collection"):
    check(f"leak caught: {leak[:44]!r}", bool(_mv(leak, "CUT")))
for ok_text in ("justify its presence in your library",
                "a passive, low-stakes watch with zero narrative challenge",
                # arguing DOWN cited acclaim is the custodian rule at work,
                # and a premise may demand things — only the OWNER may not
                "The 94% Rotten Tomatoes score is a red herring",
                "the sharp, dark satire the premise demands",
                "required to hold your attention",
                "filler that does not respect your time",
                "a complete waste of screen time"):
    check(f"honest phrasing passes: {ok_text[:40]!r}", not _mv(ok_text, "CUT"))
check("the bitrate note stays legal on a flag verdict",
      not _mv("Bitrate: 173 MB/min, 2.4x the class median", "KEEP_WITH_FLAG")
      and bool(_mv("padded to 12 GB of filler", "CUT")))
check("a violation triggers exactly one named retry",
      "Your previous attempt was rejected" in
      (Path(__file__).resolve().parents[1]
       / "src/services/pillars.py").read_text(encoding="utf-8"))

# -- the owner's word about their own life is evidence, not testimony -------
# The Back to the Future III thread: the owner attested three full watches of
# the trilogy — first-party Pillar-0 evidence the server's history could not
# contain — and the curator dismissed it as "sentiment, not curation" and
# re-offered the delete button after being overruled. The anti-sycophancy
# spine had overcorrected into refusing the very information it exists to
# accept.

_chat = (Path(__file__).resolve().parents[1]
         / "src/routers/chat.py").read_text(encoding="utf-8")
check("the two kinds of owner testimony are distinguished",
      "OWNER TESTIMONY — two kinds" in _chat
      and "FIRST-PARTY EVIDENCE, not testimony" in _chat)
check("stated rewatches are Pillar-0 evidence that wins concession",
      "Stated rewatches or attachment ARE" in _chat)
check("no contempt, no re-offered delete after the owner decided",
      "no repeating the deletion" in _chat
      and "never contemptuous of the owner" in _chat)
check("an overruled bitrate outlier gets the downscale offer, once",
      "downscale flag recovers most of" in _chat)
check("the rules ride on EVERY discussion turn, not just Level 2",
      "block += _OWNER_TESTIMONY_RULES" in _chat)
check("the Level-2 testimony paragraph carries the same distinction",
      "This applies ONLY to claims" in _chat)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
