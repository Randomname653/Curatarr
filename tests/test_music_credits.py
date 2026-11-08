"""Collaboration credits fold into their artists (services/music_credits).

The owner's rule, 2026-11-08, cases from his own history:

- a credit of known artists counts for each known one, first-billed first;
- a name known itself is never split ("Simon & Garfunkel");
- the first-billed part must be known: "Paper & Jones" does not land on an
  unrelated "Jones", "Jane Doe & the Range" not on "the Range";
- a band whose name reads like a credit stays itself when its first part
  is unknown ("Between the Trees and Me");
- the taste tally moves a credit's plays, newest play and best rating onto
  the members; the Knowledge Base marks a missed credit "credit", not "not
  found", and leaves it off the open count.

    python tests/test_music_credits.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from datetime import datetime

from src.services import music_credits as mc
from src.services.enrichment_state import STATE_DEFINITIONS, STATES, attention_reasons
from src.services.kb_overview import _mark_credits

KNOWN = {n.casefold(): n for n in ("North Signal", "South Static", "Solo Act", "Plain Singer", "Jones", "the Range",
                                   "Simon & Garfunkel", "First Act", "Second Act", "Lead Rapper")}

check("a credit of two known artists counts for both, first-billed first",
      mc.credit_members("North Signal vs. South Static", KNOWN) == ["North Signal", "South Static"])
check("...case as the artists spell it, any separator",
      mc.credit_members("north signal x SOUTH STATIC", KNOWN) == ["North Signal", "South Static"])
check("only the known parts: Solo Act & Guest Name -> Solo Act",
      mc.credit_members("Solo Act & Guest Name", KNOWN) == ["Solo Act"])
check("three parts, two separators", mc.credit_members("First Act & Second Act feat. Teddy", KNOWN)
      == ["First Act", "Second Act"])
check("a list credit", mc.credit_members("Lead Rapper, Second, Third & Fourth", KNOWN) == ["Lead Rapper"])
check("a name known itself is never split", mc.credit_members("Simon & Garfunkel", KNOWN) == [])
check("the first-billed part must be known: no 'Jones' for Paper & Jones",
      mc.credit_members("Paper & Jones", KNOWN) == [])
check("...no 'the Range' for Jane Doe & the Range", mc.credit_members("Jane Doe & the Range", KNOWN) == [])
check("a band that reads like a credit stays itself", mc.credit_members("Between the Trees and Me", KNOWN) == [])
check("a single name is no credit", mc.credit_members("Plain Singer", KNOWN) == [] and mc.credit_parts("Plain Singer") == ["Plain Singer"])

old, new = datetime(2026, 1, 1), datetime(2026, 11, 1)
tally = {"North Signal vs. South Static": {"plays": 18, "last": new, "ur": 8.0},
         "North Signal": {"plays": 5, "last": old, "ur": None},
         "Paper & Jones": {"plays": 8, "last": new, "ur": None}}
n = mc.fold_credits(tally, KNOWN)
check("the taste tally: the credit's plays, newest play and best rating move onto its artists",
      n == 1 and "North Signal vs. South Static" not in tally
      and tally["North Signal"] == {"plays": 23, "last": new, "ur": 8.0}
      and tally["South Static"] == {"plays": 18, "last": new, "ur": 8.0})
check("...and a credit the rule does not fold stays as it was", tally["Paper & Jones"]["plays"] == 8)


def item(title, state, mbid=None, cat="music"):
    return {"category": cat, "title": title, "state": state, "mbid": mbid, "downloaded": True}


items = [item("North Signal", "enriched"), item("South Static", "enriched_provisional"),
         item("North Signal vs. South Static", "not_found"), item("North Signal & Friends", "retry_due"),
         item("North Signal x South Static", "queued"), item("North Signal & South Static", "not_found", mbid="mb-1"),
         item("Paper & Jones", "not_found"), item("North Signal & Co", "not_found", cat="show")]
_mark_credits(items)
st = {(i["category"], i["title"]): (i["state"], i.get("credit_of")) for i in items}
check("the KB: a missed credit of known artists is 'credit', naming them",
      st[("music", "North Signal vs. South Static")] == ("credit", ["North Signal", "South Static"])
      and st[("music", "North Signal & Friends")] == ("credit", ["North Signal"]))
check("...not one still queued (a source may know it), not one with an MBID, not another category, "
      "not Paper & Jones",
      st[("music", "North Signal x South Static")][0] == "queued" and st[("music", "North Signal & South Static")][0] == "not_found"
      and st[("show", "North Signal & Co")][0] == "not_found" and st[("music", "Paper & Jones")][0] == "not_found")
check("'credit' is a state with its own definition, and on nobody's attention list",
      "credit" in STATES and STATE_DEFINITIONS["credit"]["label"]
      and attention_reasons("credit", None, 5, has_external_id=True) == [])

check.done()
