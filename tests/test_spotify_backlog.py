"""Top-tracks grouping for the Spotify backlog page.

    python tests/test_spotify_backlog.py

The page used to run one query per artist to fetch their top three tracks —
a hundred round-trips for data a single GROUP BY already has. Batching that
is only safe if the ordering survives, and the per-artist query ended in
``ORDER BY count DESC LIMIT 3`` with no secondary key: tracks on equal play
counts came back in whatever order the engine produced. Listening histories
are full of tracks played exactly once, so ties sit right at the third slot
as a matter of course. The tie-break is therefore spelled out, and these
checks hold it in place.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.routers.library import _top_tracks_by_artist


class Row:
    """Stands in for a (series_title, title, p) result row."""

    def __init__(self, artist, title, plays):
        self.series_title, self.title, self.p = artist, title, plays


# ── grouping ────────────────────────────────────────────────────────────────

grouped = _top_tracks_by_artist([
    Row("A", "a1", 5), Row("B", "b1", 9), Row("A", "a2", 7),
])
check("rows are grouped by artist", set(grouped) == {"A", "B"})
check("each artist keeps only their own tracks",
      [t["title"] for t in grouped["A"]] == ["a2", "a1"]
      and [t["title"] for t in grouped["B"]] == ["b1"])
check("plays are carried through", grouped["B"][0]["plays"] == 9)

# ── ordering and truncation ─────────────────────────────────────────────────

ranked = _top_tracks_by_artist([
    Row("A", "low", 1), Row("A", "high", 30),
    Row("A", "mid", 8), Row("A", "lowest", 0),
])["A"]
check("most-played first", [t["title"] for t in ranked] == ["high", "mid", "low"])
check("only three survive", len(ranked) == 3)

# ── the tie-break the batched query must not leave to chance ────────────────

tie = _top_tracks_by_artist([
    Row("A", "zulu", 4), Row("A", "alpha", 4),
    Row("A", "mike", 4), Row("A", "bravo", 4),
])["A"]
check("a four-way tie resolves alphabetically, not by row order",
      [t["title"] for t in tie] == ["alpha", "bravo", "mike"])
check("...and the same input always yields the same three",
      _top_tracks_by_artist([
          Row("A", "bravo", 4), Row("A", "mike", 4),
          Row("A", "alpha", 4), Row("A", "zulu", 4),
      ])["A"] == tie)

boundary = _top_tracks_by_artist([
    Row("A", "first", 10), Row("A", "second", 8),
    Row("A", "tie_b", 5), Row("A", "tie_a", 5),
])["A"]
check("a tie for the last slot picks deterministically",
      [t["title"] for t in boundary] == ["first", "second", "tie_a"])

# ── degenerate input ────────────────────────────────────────────────────────

check("no rows -> no artists", _top_tracks_by_artist([]) == {})
check("a missing title does not break the sort",
      len(_top_tracks_by_artist([Row("A", None, 2), Row("A", "x", 1)])["A"]) == 2)
check("a custom page size is honoured",
      len(_top_tracks_by_artist(
          [Row("A", f"t{i}", i) for i in range(10)], per_artist=5)["A"]) == 5)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
