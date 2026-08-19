"""Guard against acting on implausible mass-staleness (SoulSync port, MIT).

SoulSync's lesson (their #828): ``os.path.exists`` returns False for EVERY
file when storage is momentarily unreachable — one unguarded cleanup then
wiped whole libraries from the DB. Our equivalents: an arr that is down
contributes ZERO items to the ground-truth list, so every one of its chroma
docs suddenly classifies as "gone"; a cache DB hiccup makes every doc look
profile-less. The rule: when an implausibly large share of a set goes stale
at once, the INFRASTRUCTURE is the likelier failure — skip, warn, retry
next pass.

Pure predicates, shared by every stale-classification site.
"""

# Don't second-guess tiny sets — a 3-doc service legitimately losing all
# three shouldn't be blocked. Above the floor, a large missing fraction
# almost always means "source unreachable", not "items actually deleted".
DEFAULT_MIN_TOTAL = 20
DEFAULT_MAX_FRACTION = 0.5


def is_implausible_mass_stale(stale_count: int, total_count: int, *,
                              min_total: int = DEFAULT_MIN_TOTAL,
                              max_fraction: float = DEFAULT_MAX_FRACTION) -> bool:
    """True when ``stale_count`` is too large a share of ``total_count`` to be
    real — the caller should SKIP acting (and warn) rather than mass-process.
    Small sets (< min_total) always pass so genuine cleanup still works."""
    if total_count <= 0 or stale_count <= 0:
        return False
    if total_count < min_total:
        return False
    return stale_count > total_count * max_fraction
