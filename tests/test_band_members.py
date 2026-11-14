"""A band's members reach the curator (MusicBrainz "member of band").

2026-11-14: the owner named the singer of a band he plays daily, and the
curator "corrected" him from a same-named artist elsewhere in the library,
then admitted it had no line-up at all. A prompt rule against that was
benched and did nothing; the line-up was simply missing. MusicBrainz files
it in the artist lookup we already make (inc=artist-rels): one relation per
role, a role-less one besides, years, ended or not.

- relations merge into one entry per person: roles, first year, last year,
  current or former; current members first;
- the curator's music block gets a "Band members" line, "not in our
  database" when the band lists none, nothing for a person;
- an artist record cached before members were read gets one lookup of its
  own; a person, an unknown artist or an outage say nothing;
- the chat's helper is time-boxed and leaves the data alone on failure.

Every name is invented; the relation shapes are MusicBrainz's own.

    python tests/test_band_members.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import asyncio

import src.services.music_metadata as mm
from src.services.enrichment_state import TRANSIENT


def rel(name, attrs, begin=None, end=None, ended=False, pid=None):
    return {"type": "member of band", "direction": "backward", "attributes": attrs,
            "begin": begin, "end": end, "ended": ended,
            "artist": {"id": pid or name.lower().replace(" ", "-"), "name": name}}


RELS = [
    rel("Ada Brandt", ["original"], "2010", "2012", True),
    rel("Lio Marsh", ["lead vocals", "original"], "2010", "2020-02-12", True),
    rel("Kai Veers", ["keyboard", "original"], "2010"),
    rel("Kai Veers", ["lead vocals", "original"], "2010"),
    rel("Kai Veers", []),
    rel("Mira Holt", ["lead vocals"], "2020-04-24"),
    {"type": "subgroup", "direction": "forward", "artist": {"id": "x", "name": "Side Act"}},
    {"type": "member of band", "direction": "forward", "artist": {"id": "y", "name": "Other Band"}},
]

ms = mm.members_from_relations(RELS)
names = [m["name"] for m in ms]
check("one entry per person, current members first", names == ["Kai Veers", "Mira Holt", "Ada Brandt", "Lio Marsh"], names)
kai = ms[0]
check("...roles merge across a person's relations, 'original' left out",
      kai["roles"] == ["keyboard", "lead vocals"] and kai["current"] and kai["begin"] == "2010", kai)
lio = next(m for m in ms if m["name"] == "Lio Marsh")
check("...a former member keeps the years they were in", lio == {"name": "Lio Marsh", "roles": ["lead vocals"],
      "begin": "2010", "end": "2020", "current": False}, lio)
check("...only 'member of band' backwards (the band's members), not subgroups or the bands a person joined",
      "Side Act" not in names and "Other Band" not in names)

line = mm.format_members(ms)
check("the block line names roles and years, current then former",
      line == "Kai Veers (keyboard, lead vocals, since 2010), Mira Holt (lead vocals, since 2020); "
              "former: Ada Brandt (2010–2012), Lio Marsh (lead vocals, 2010–2020)", line)
check("...an empty line-up formats to nothing", mm.format_members([]) == "")

# ── the curator's music block ────────────────────────────────────────────────
from src.routers.chat import _build_hidden_context, _with_members  # noqa: E402

blk = _build_hidden_context("Glass Signal", {"artist": "Neon Parish", "members": ms}, domain="music")
check("the music block carries the band's members", "- Band members: Kai Veers (keyboard" in blk, blk)
blk = _build_hidden_context("Glass Signal", {"artist": "Neon Parish", "members": []}, domain="music")
check("...'not in our database' when the band lists none: we checked", "- Band members: (not in our database)" in blk, blk)
blk = _build_hidden_context("Glass Signal", {"artist": "Solo Person"}, domain="music")
check("...and no line at all when nobody looked (a person, or no lookup)", "Band members" not in blk, blk)

# ── fetch_band_members ───────────────────────────────────────────────────────
records = {}
mb_calls = []


async def fake_artist(name, mbid=None, rejected=None):
    return records.get(name)


class _Resp:
    status_code = 200

    def json(self):
        return {"relations": RELS}


async def fake_mb(client, url, params):
    mb_calls.append((url, params))
    return _Resp()

mm.fetch_musicbrainz_artist = fake_artist
mm._mb_request = fake_mb

records["Neon Parish"] = {"mbid": "np-1", "type": "Group", "members": ms[:2]}
check("a fresh artist record answers with its own members, no extra lookup",
      asyncio.run(mm.fetch_band_members("Neon Parish")) == ms[:2] and mb_calls == [])
records["Old Cache Band"] = {"mbid": "ocb-1", "type": "Group"}
got = asyncio.run(mm.fetch_band_members("Old Cache Band"))
check("a record cached before members were read gets one artist-rels lookup",
      [m["name"] for m in got] == names and len(mb_calls) == 1
      and mb_calls[0][1].get("inc") == "artist-rels", (got, mb_calls))
asyncio.run(mm.fetch_band_members("Old Cache Band"))
check("...and that lookup is cached", len(mb_calls) == 1, mb_calls)
records["Solo Person"] = {"mbid": "sp-1", "type": "Person", "members": None}
check("a person has no members: nothing to say", asyncio.run(mm.fetch_band_members("Solo Person")) is None)
records["Down Band"] = TRANSIENT
check("an outage says nothing (no empty line-up)", asyncio.run(mm.fetch_band_members("Down Band")) is None)
check("an unknown artist says nothing", asyncio.run(mm.fetch_band_members("Nobody At All")) is None)

# ── the chat helper ──────────────────────────────────────────────────────────
import src.services.music_metadata as _mm2  # noqa: E402


async def slow(*a, **k):
    await asyncio.sleep(10)

_real = _mm2.fetch_band_members
check("the helper adds the line-up for a band",
      asyncio.run(_with_members({"artist": "Neon Parish"}, "Neon Parish")).get("members") == ms[:2])
_mm2.fetch_band_members = slow
import time  # noqa: E402
t0 = time.time()
out = asyncio.run(_with_members({"artist": "Neon Parish"}, "Neon Parish"))
check("...a slow MusicBrainz never holds the reply: time-boxed, data unchanged",
      out == {"artist": "Neon Parish"} and time.time() - t0 < 6, (out, time.time() - t0))
_mm2.fetch_band_members = _real

check.done()
