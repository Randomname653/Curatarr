"""The two sources behind the archive pillar: Wikidata facts and reception.

    python tests/test_archive_facts.py

Offline — no network, no model. The live behaviour of both was verified by
hand against real titles; what is frozen here is the shape.

The reception check exists because one exit from build_reception returned two
values where every other returned four. The caller unpacked four, so every
film and show raised — and the exception was logged at debug level, which
made a total failure look like a slow walker for months. An arity mismatch
inside one function is invisible to the eye and trivial to assert.
"""
import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── every exit from build_reception must agree on how many values it returns ──

tree = ast.parse((ROOT / "src/services/reception.py").read_text(encoding="utf-8"))
fn = next(n for n in ast.walk(tree)
          if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
          and n.name == "build_reception")
arities = set()
for node in ast.walk(fn):
    if isinstance(node, ast.Return) and node.value is not None:
        # nested helpers defined inside would skew this; there are none today
        arities.add(len(node.value.elts) if isinstance(node.value, ast.Tuple) else 1)
check(f"build_reception returns one arity everywhere (found {sorted(arities)})",
      len(arities) == 1)
check("...and it is the four the caller unpacks", arities == {4})

src = (ROOT / "src/services/reception.py").read_text(encoding="utf-8")
check("the caller still unpacks four", "rec, rels, staff, finale = await build_reception" in src)

# ── Wikidata: shaping facts for a judge ─────────────────────────────────────

from src.services.wikidata import (
    _LOGIC_VERSION, _QUERY, _is_major, format_wikidata_line)

check("major awards are recognised",
      _is_major("Academy Award for Best Picture") and _is_major("Palme d'Or")
      and _is_major("Primetime Emmy Award for Outstanding Drama Series"))
check("a local festival prize is not",
      not _is_major("Fantasporto Audience Award")
      and not _is_major("Best Newcomer, Someplace Film Week"))
check("nothing is claimed about an empty award", not _is_major(""))

check("an adaptation reads as one",
      format_wikidata_line({"source_author": "John le Carré",
                            "source_work": "The Night Manager"})
      == "adapted from The Night Manager by John le Carré")
check("a known author without a named work still reads",
      "by Joseph Conrad" in format_wikidata_line({"source_author": "Joseph Conrad"}))
check("awards are named, not counted",
      format_wikidata_line({"awards": ["Palme d'Or", "Academy Award for Best Sound"]})
      == "won Palme d'Or, Academy Award for Best Sound")
check("a title with only minor awards reports the count honestly",
      format_wikidata_line({"award_count": 4}) == "4 recorded awards (none major)")
check("both halves combine",
      format_wikidata_line({"source_author": "Mario Puzo", "source_work": "The Godfather",
                            "awards": ["Academy Award for Best Picture"]})
      == "adapted from The Godfather by Mario Puzo; won Academy Award for Best Picture")
check("nothing known -> no line", format_wikidata_line({}) == "")

check("the query version is derived from the query itself",
      _LOGIC_VERSION == __import__("hashlib").sha1(
          _QUERY.encode("utf-8")).hexdigest()[:8])
check("the query asks for the source and its author",
      "wdt:P144" in _QUERY and "wdt:P50" in _QUERY and "wdt:P166" in _QUERY)

# ── wiring ──────────────────────────────────────────────────────────────────

me = (ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("the facts reach the judge as their own line, beside the distilled prose",
      'add("On record"' in me and '"wikidata":             raw.get("wikidata")' in me)

re_ = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("and are warmed before the verdict with the other two sources",
      "topup_wikidata" in re_ and "topup_reception" in re_
      and "topup_significance" in re_)

check("a standalone runner exists to clear the backlog",
      (ROOT / "scripts/facts_speedrunner.py").exists())

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
