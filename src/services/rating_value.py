"""A rating or a vote count as a number, whatever a source or a model wrote.

Profiles carry ``rating`` from several places, and not all of them meant a
score: Jikan/MAL put the CONTENT rating there ("PG-13 - Teens 13 or older"),
music sources have none ("N/A"), and the summariser echoed its own prompt
line back ("N/A/10 (N/A votes)", "5.3/10"). ``float()`` on any of those
stopped the whole deletion scan (2026-10-19/20: "could not convert string to
float: 'N/A/10 (N/A votes)'").
"""
import math
import re
from typing import Optional

# A score at the very start, optionally written as "x/10": "7.3", "5.3/10",
# "8/10 (1,234 votes)". Anything that does not START with a number (N/A,
# PG-13 ...) is no score.
_SCORE = re.compile(r"^\s*(\d+(?:[.,]\d+)?)\s*(?:/\s*10\b|$|\s)")


def rating_number(value) -> Optional[float]:
    """A 0-10 score as a float, or None when ``value`` is no score."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
    elif isinstance(value, str):
        m = _SCORE.match(value)
        if not m:
            return None
        v = float(m.group(1).replace(",", "."))
    else:
        return None
    return v if math.isfinite(v) and 0 <= v <= 10 else None


def count_number(value) -> int:
    """A vote count as an int; 0 for anything that is not one ("N/A")."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value > 0 else 0
    if isinstance(value, str):
        digits = value.replace(",", "").replace("_", "").strip()
        return int(digits) if digits.isdigit() else 0
    return 0


def prompt_rating(rating, votes) -> str:
    """The summariser's RATING line: "7.3/10 (1,234 votes)" or "unknown".
    It used to print "N/A/10 (N/A votes)" or a content rating as a score."""
    r = rating_number(rating)
    if r is None or r <= 0:
        return "unknown"
    n = count_number(votes)
    return f"{r:.1f}/10 ({n:,} votes)" if n else f"{r:.1f}/10"
