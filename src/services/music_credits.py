"""Collaboration credits in the music index ("A & B", "A feat. B").

Plex files a track tagged "North Signal vs. South Static" under an artist of that
name. No source knows such a credit as an act, so it stayed unenriched, its
plays were left out of the music taste, and it sat on Needs attention. Its
members usually are known: their plays belong to them.

The owner's rule (2026-11-08), measured on his history (635 credit-looking
names, 9,826 of 382,783 plays; 424 of them MusicBrainz knows as acts of
their own, "Sun & Moon", "Ice & Fire"):

- a name that is known itself (a profile, an MBID) is never split;
- it must read as a credit (a separator: &, +, x, vs., feat., ft., pres.,
  and, with, a comma, " / ");
- the FIRST-billed part must be a known artist: "Paper & Jones" would
  otherwise land on an unrelated "Jones", "Jane Doe & the Range" on
  an unrelated "the Range";
- the credit's plays then count for every known part.
"""
from __future__ import annotations

import re

# One spelling of the separators for the whole app (routers/enrichment uses
# it to tell an expected miss from an outage).
CREDIT_SEP = re.compile(r"\s(?:&|\+|x|vs\.?|feat\.?|ft\.?|featuring|pres\.?|presents|and|with)\s|,\s|\s/\s",
                        re.IGNORECASE)


def looks_like_credit(name: str) -> bool:
    """A credit line, or a stray tag with no letters ("00", ".dev")."""
    s = (name or "").strip()
    if not any(ch.isalpha() for ch in s) or s.startswith("."):
        return True
    return bool(CREDIT_SEP.search(s))


def credit_parts(name: str) -> list[str]:
    """The names a credit line lists, in billing order."""
    return [p.strip() for p in CREDIT_SEP.split(name or "") if p and p.strip()]


def credit_members(name: str, known: dict) -> list[str]:
    """The known artists a credit stands for, first-billed first, or [] when
    ``name`` is no foldable credit. ``known``: casefolded name -> the
    artist's own spelling, for artists with a profile."""
    s = (name or "").strip()
    if not s or s.casefold() in known:
        return []
    parts = credit_parts(s)
    if len(parts) < 2:
        return []
    first = known.get(parts[0].casefold())
    if not first:
        return []
    out = [first]
    for p in parts[1:]:
        k = known.get(p.casefold())
        if k and k not in out:
            out.append(k)
    return out


def fold_credits(by_artist: dict, known: dict) -> int:
    """Move each foldable credit's plays onto its members, in place.
    ``by_artist``: name -> {"plays", "last", "ur"} (the taste engine's
    per-artist tally). Returns how many credits were folded."""
    folded = 0
    for name in list(by_artist):
        members = credit_members(name, known)
        if not members:
            continue
        d = by_artist.pop(name)
        folded += 1
        for m in members:
            t = by_artist.setdefault(m, {"plays": 0, "last": None, "ur": None})
            t["plays"] += d.get("plays") or 0
            if d.get("last") and (t["last"] is None or d["last"] > t["last"]):
                t["last"] = d["last"]
            if d.get("ur") is not None and (t["ur"] is None or d["ur"] > t["ur"]):
                t["ur"] = d["ur"]
    return folded
