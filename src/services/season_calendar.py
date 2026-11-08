"""The household's moment of the year, in its own language.

``llm_utils.seasonal_context`` knows "Halloween season", "the holiday
season" and four seasons, in English. The season shelf (collection_designer)
needs the moments a household actually lives by, in the household's
language: in German that is Advent, "zwischen den Jahren", Karneval with its
moving date, Easter and the summer holidays.

The calendar follows the language, not a country: a German-speaking
household gets the German one. Moments overlap nowhere; outside them the
plain season stands in, so the shelf always has a theme.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

LANGUAGE_NAMES = {"de": "German", "en": "English", "fr": "French", "es": "Spanish",
                  "it": "Italian", "nl": "Dutch"}


def easter(year: int) -> date:
    """Easter Sunday (Gregorian; the anonymous algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _windows(year: int, lang: str) -> list:
    """(start, end inclusive, key, label) for one year, in ``lang``."""
    e = easter(year)
    de = lang == "de"
    return [
        (date(year, 1, 1), date(year, 1, 1), "new_year", "Neujahr" if de else "New Year's"),
        # Weiberfastnacht (Thursday before Rosenmontag) to Aschermittwoch;
        # listed before Valentine's Day, which it can overlap, so it wins.
        *([(e - timedelta(days=52), e - timedelta(days=46), "carnival", "Karneval")] if de else []),
        (date(year, 2, 7), date(year, 2, 14), "valentine", "Valentinstag" if de else "Valentine's Day"),
        (e - timedelta(days=7), e + timedelta(days=1), "easter", "Ostern" if de else "Easter"),
        (date(year, 7, 1), date(year, 8, 31), "summer_holidays", "Sommerferien" if de else "summer holidays"),
        (date(year, 10, 15), date(year, 10, 31), "halloween", "Halloween"),
        (date(year, 11, 27), date(year, 12, 26), "christmas",
         "Advent und Weihnachten" if de else "the run-up to Christmas"),
        (date(year, 12, 27), date(year, 12, 31), "between_years",
         "zwischen den Jahren und Silvester" if de else "the days between Christmas and New Year's"),
    ]


_SEASONS = {
    "de": {"winter": "Winter", "spring": "Frühling", "summer": "Sommer", "autumn": "Herbst"},
    "en": {"winter": "winter", "spring": "spring", "summer": "summer", "autumn": "autumn"},
}


def moment(today: date, lang: str = "en") -> dict:
    """``{"key", "label", "season"}`` for ``today``: the named moment it falls
    in, else the plain season (key = the season)."""
    lang = lang if lang in _SEASONS else "en"
    m = today.month
    season = ("winter" if m in (12, 1, 2) else "spring" if m in (3, 4, 5)
              else "summer" if m in (6, 7, 8) else "autumn")
    for start, end, key, label in _windows(today.year, lang):
        if start <= today <= end:
            return {"key": key, "label": label, "season": season}
    return {"key": season, "label": _SEASONS[lang][season], "season": season}


def language_name(lang: Optional[str]) -> str:
    return LANGUAGE_NAMES.get(lang or "en", "English")
