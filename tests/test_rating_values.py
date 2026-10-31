"""A profile's rating is read as a number or not at all.

2026-10-19/20 the scheduled ARR sync failed on every run: "could not convert
string to float: 'N/A/10 (N/A votes)'". The summariser had been asked to
echo the rating and wrote its own prompt line back; Jikan/MAL put the
CONTENT rating into the same field ("PG-13 - Teens 13 or older"), and music
profiles carry "N/A". One such profile stopped the whole deletion scan.

    python tests/test_rating_values.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import src.services.media_enricher as me  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402
from src.services.rating_value import count_number, prompt_rating, rating_number  # noqa: E402

from _checks import check  # noqa: E402


# ── the parser, on the values the live cache holds ───────────────────────────
for text in ("N/A/10 (N/A votes)", "N/A", "PG-13 - Teens 13 or older",
             "R - 17+ (violence & profanity)/10 (N/A votes)", "", None, True, float("nan"), 55):
    check(f"no score in {text!r}", rating_number(text) is None)
for text, want in (("5.3/10", 5.3), ("8.1", 8.1), ("7,2/10", 7.2), ("8/10 (1,234 votes)", 8.0),
                   (7.4, 7.4), (0, 0.0)):
    check(f"{text!r} reads as {want}", rating_number(text) == want)
check("vote counts: numbers, digit strings, anything else 0",
      (count_number(1234), count_number("12,345"), count_number("N/A"), count_number(None)) == (1234, 12345, 0, 0))

# ── the prompt line ──────────────────────────────────────────────────────────
check("the summariser's RATING line says unknown instead of N/A/10 (N/A votes)",
      prompt_rating("N/A", None) == "unknown" and prompt_rating("PG-13 - Teens 13 or older", 5) == "unknown")
check("...and a real score with its votes", prompt_rating(7.36, "12,345") == "7.4/10 (12,345 votes)")
src = Path(me.__file__).read_text(encoding="utf-8")
check("the template no longer builds '<rating>/10 (<votes> votes)' itself",
      "RATING: {rating}/10 ({votes} votes)" not in src and "RATING: {rating}\n" in src)

# ── the deletion scorer survives what is in the cache ────────────────────────
for bad in ("N/A/10 (N/A votes)", "PG-13 - Teens 13 or older", "N/A"):
    ctx, r, g = eng._rating_from_cached({"response": {"rating": bad, "vote_count": "N/A",
                                                      "genres": ["Drama"]}})
    check(f"scorer: {bad!r} is no rating, genres kept", ctx is None and r is None and g == ["Drama"])
ctx, r, _ = eng._rating_from_cached({"response": {"rating": "5.3/10", "vote_count": "1,500"}})
check("scorer: '5.3/10' scores 5.3 with its votes", r == 5.3 and "1,500 votes" in ctx)

# ── the summariser stores the SOURCE rating, as a number ─────────────────────
_real = me._summarize_with_small_llm


async def _echo(raw):
    return {"genres": ["Drama"], "rating": "N/A/10 (N/A votes)"}

me._summarize_with_small_llm = _echo
try:
    a = asyncio.run(me.summarize_with_small_llm({"title": "X", "rating": 6.9}))
    b = asyncio.run(me.summarize_with_small_llm({"title": "Y", "rating": "PG-13 - Teens 13 or older"}))
    c = asyncio.run(me.summarize_with_small_llm({"title": "Z"}))
finally:
    me._summarize_with_small_llm = _real
check("summariser: the model's echo is replaced by the source's number", a["rating"] == 6.9)
check("summariser: a content rating is no score", b["rating"] is None)
check("summariser: no source rating, no rating", c["rating"] is None)

check.done()
