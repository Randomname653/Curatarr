"""English UI words left in the frontend modules may only go down (UX design
record 3.8): every module translated so far has its count of English
literals (tests/i18n_literals) in tests/i18n_literal_baseline.json, and a
module may never write more of them than that. A translated module stays at
its number or below; lower it in the same change when it goes down. By the
end of the round every module is listed.

    python tests/test_i18n_ratchet.py            # check
    python tests/test_i18n_ratchet.py --update   # rewrite the baseline (only ever lower)
"""
from _harness import check  # noqa: I001 - first: temp stores

import json
import sys
from pathlib import Path

import i18n_literals

BASELINE = Path(__file__).resolve().parent / "i18n_literal_baseline.json"
base = json.loads(BASELINE.read_text(encoding="utf-8"))
now = i18n_literals.counts(base)

if "--update" in sys.argv:
    lower = {m: min(base[m], now[m]) for m in base}
    BASELINE.write_text(json.dumps(lower, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print("baseline:", lower)

for module in sorted(base):
    check(f"{module}: {now[module]} English literals, at most {base[module]}", now[module] <= base[module],
          f"{now[module]} > {base[module]}: put the new words in frontend/i18n/en.json and de.json")
lower = [m for m in base if now[m] < base[m]]
check("a module that went down has its baseline lowered in the same change (--update)", not lower, lower)
check("the counter still sees English: a sentence counts, a class list and a key do not",
      i18n_literals.english("Nothing in this lane yet")
      and not i18n_literals.english("btn btn-secondary btn-sm") and not i18n_literals.english("recs.lane.library"))
check.done()
