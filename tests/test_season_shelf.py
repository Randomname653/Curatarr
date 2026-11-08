"""The season shelf: the household's moment of the year, in its language.

- the calendar: Easter by the Gregorian rule, German Karneval from
  Weiberfastnacht to Aschermittwoch (moving with Easter), Advent and
  Christmas, "zwischen den Jahren", Halloween, the summer holidays; outside
  them the plain season; an English household has no Karneval and English
  labels; an unknown language falls back to English;
- the designer's prompt asks for one shelf more, theme 1 the season shelf,
  named and described in the household's language, with what the
  household watched in this month of earlier years as the mood;
- those titles come from the monthly taste snapshots, newest year first,
  deduplicated, at most eight.

    python tests/test_season_shelf.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import json
from datetime import date

app_db()
import src.database.connection as conn_mod  # noqa: E402
from src.database.models import TasteSnapshot, User  # noqa: E402
from src.services import collection_designer as cd  # noqa: E402
from src.services.season_calendar import easter, language_name, moment  # noqa: E402

check("Easter by the Gregorian rule", easter(2026) == date(2026, 4, 5) and easter(2027) == date(2027, 3, 28)
      and easter(2025) == date(2025, 4, 20))
check("Karneval moves with Easter: Weiberfastnacht to Aschermittwoch 2027",
      moment(date(2027, 2, 4), "de")["key"] == "carnival" and moment(date(2027, 2, 10), "de")["key"] == "carnival"
      and moment(date(2027, 2, 11), "de")["key"] != "carnival" and moment(date(2027, 2, 3), "de")["key"] == "winter")
check("the German moments, in German",
      [moment(d, "de")["label"] for d in (date(2026, 12, 6), date(2026, 12, 29), date(2026, 10, 20),
                                           date(2026, 7, 15), date(2026, 4, 2))]
      == ["Advent und Weihnachten", "zwischen den Jahren und Silvester", "Halloween", "Sommerferien", "Ostern"])
check("outside a moment: the plain season", moment(date(2026, 11, 8), "de") == {
    "key": "autumn", "label": "Herbst", "season": "autumn"} and moment(date(2026, 5, 20), "en")["label"] == "spring")
check("Karneval wins over Valentine's Day where they overlap; after it, Valentine's Day",
      moment(date(2027, 2, 9), "de")["key"] == "carnival" and moment(date(2027, 2, 12), "de")["label"] == "Valentinstag")
check("an English household: no Karneval, English labels",
      moment(date(2027, 2, 5), "en")["key"] == "winter" and moment(date(2027, 2, 5), "de")["key"] == "carnival" and moment(date(2026, 12, 6), "en")["label"] == "the run-up to Christmas")
check("an unknown language falls back to English",
      moment(date(2026, 12, 6), "xx")["label"] == "the run-up to Christmas" and language_name("xx") == "English")

p = cd._design_prompt("movie", 2, "dark thrillers", ["Heat", "Se7en"], ["Old Shelf"],
                      season={"label": "Advent und Weihnachten", "before": ["Tower Night", "Snow Post"]},
                      language="German")
check("the prompt asks for one shelf more, theme 1 the season shelf, every shelf in the household's language",
      "You design 3 themed" in p and "exactly 3 theme(s)" in p
      and "theme 1 is the household's shelf for Advent und Weihnachten" in p
      and "names and descriptions in German" in p and "English" not in p)
check("...with last years' titles of this month as the mood", "Tower Night; Snow Post" in p)
plain = cd._design_prompt("movie", 2, "", ["Heat"], [])
check("without a season: as before, English without a language",
      "exactly 2 theme(s)" in plain and "SEASON SHELF" not in plain and "names and descriptions in English" in plain)

with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.commit()
    for month, titles in (("2025-11", ["Snow Post", "Tower Night"]), ("2024-11", ["Tower Night", "Workshop"]),
                          ("2025-10", ["Halloween"]), ("2023-11", ["Small Trouble"]), ("2022-11", ["Too Old"])):
        db.add(TasteSnapshot(user_id=1, category="movie", month=month, top_titles=json.dumps(titles)))
    db.add(TasteSnapshot(user_id=1, category="show", month="2025-11", top_titles=json.dumps(["Night Shift"])))
    db.commit()
got = cd.same_month_before(1, "movie", date(2026, 11, 8))
check("this month of the last three years, newest first, no repeats, no other month or category",
      got == ["Snow Post", "Tower Night", "Workshop", "Small Trouble"])

check.done()
