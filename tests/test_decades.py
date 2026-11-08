"""Your decades (wrapup_facts._eras, wrapup_insights._decades, household).

- the facts carry each decade's share, hours and leading titles (by hours);
- the story gets a "decades" card when a decade that is not this one held
  a quarter of the hours or more; the curator writes its line from it;
  this decade leading, or no decade with a real share: no card;
- in the household comparison a member hears which decades they hold the
  biggest share of, without anyone else's numbers.

    python tests/test_decades.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from datetime import datetime

from src.services import wrapup_insights as wi
from src.services.wrapup_facts import Period, _eras

period = Period("year:2026", datetime(2026, 1, 1), datetime(2026, 11, 8), "2026 in review", False)
titles = [{"title": t, "hours": h, "media_type": "movie"} for t, h in
          (("Neon City", 6), ("Hive Ship", 4), ("Heat", 3), ("Far Signal", 2), ("Sand World", 5))]
years = {"Neon City": 1982, "Hive Ship": 1986, "Heat": 1995, "Far Signal": 2016, "Sand World": 2021}
e = _eras(titles, {t: {"year": y} for t, y in years.items()}, period)
check("each decade's share, hours and leading titles by hours",
      e["decades"] == {"1980s": 0.5, "1990s": 0.15, "2010s": 0.1, "2020s": 0.25}
      and e["decade_hours"]["1980s"] == 10.0 and e["decade_top"]["1980s"] == ["Neon City", "Hive Ship"])

cards = wi._decades({"eras": e, "period": {"end": "2026-11-08T00:00:00"}})
check("a past decade with half the hours: the story's decades card, naming its titles",
      len(cards) == 1 and cards[0]["slide"] == "decades" and cards[0]["values"]["decade"] == "1980s"
      and "Neon City" in cards[0]["claim"] and cards[0]["refs"] == ["Neon City", "Hive Ship"])
now_leads = {"eras": {"decades": {"2020s": 0.7, "1990s": 0.3}, "decade_top": {}}, "period": {"end": "2026-05-01"}}
check("this decade leading is no news", wi._decades(now_leads) == [])
flat = {"eras": {"decades": {"1980s": 0.2, "1990s": 0.2, "2000s": 0.2, "2010s": 0.2, "2020s": 0.2}},
        "period": {"end": "2026-05-01"}}
check("no decade with a real share: no card", wi._decades(flat) == [])
check("the card is mined with the others", any(c["slide"] == "decades" for c in wi.mine(
    {"eras": e, "period": {"end": "2026-11-08"}, "tempo": {"hours": 20, "plays": 5}}, floor=0)))

import src.services.household_compare as hc  # noqa: E402
mine = {"eligible": True, "tempo": {"hours": 20}, "eras": {"decades": {"1980s": 0.5, "1990s": 0.15, "2020s": 0.35}},
        "top": []}
other = {"facts_version": __import__("src.services.wrapup_facts", fromlist=["x"]).FACTS_VERSION,
         "eligible": True, "tempo": {"hours": 30}, "eras": {"decades": {"1980s": 0.1, "1990s": 0.6, "2020s": 0.3}}}
hc.consented = lambda uid: True
hc._others = lambda uid: [2]
import src.services.wrapup_store as ws  # noqa: E402
ws.load = lambda uid, scope, key: {"facts": other}
out = hc.compare(1, "all", "year:2026", mine)
check("the household: the decades this member holds the most of, nobody else's numbers",
      out.get("decades_owned") == ["1980s", "2020s"] and "decades" not in str({k: v for k, v in out.items()
                                                                                if k != "decades_owned"}))

check.done()
