"""The library item and the profile attached to it must be the same work.

Provoked by a live failure: Sonarr carried ``tmdbId 4054`` for *Museum of
Life*, a BBC nature documentary. The correct entry is ``40545`` — one digit
longer. Every lookup therefore returned *Forbidden Love*, a 1999 Japanese
melodrama about a teacher and her student; that plot was cached under the
documentary's own keys, renewed monthly by the raw refresher, handed to the
deletion judge as VERIFIED DATA, and the judge — which correctly noticed the
plot contradicted the "Documentary" genre — proposed deleting the documentary
*because* its metadata was wrong.

Four things had to fail together, and each gets a test here:
  1. nothing outranked the arr's mistyped id;
  2. the refresher re-fetched from the blob it was refreshing, so the wrong
     match confirmed itself and never expired;
  3. the judge's own path called the enricher without a year, switching off
     the wrong-entity delta-check that has existed since 0e0453f;
  4. no law said that a misfiled record cannot be a deletion argument.

No network, no DB: these read the source and exercise the pure predicates.
"""

import pathlib

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src"


def _read(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


# ── 1. the id authority ───────────────────────────────────────────────────

def test_both_enrichment_paths_prefer_tmdbs_own_tvdb_mapping():
    """The arr's tmdbId is a secondary cross-reference; TMDB's index is
    primary. Both the bulk walker and the on-demand path must consult it, or
    the two disagree about which work an item is — and then write their
    caches under that disagreement."""
    src = _read("services/media_enricher.py")
    assert "async def authoritative_tv_tmdb_id(" in src
    # called from fetch_and_prepare_raw AND enrich_media_item
    assert src.count("await authoritative_tv_tmdb_id(") == 2
    # never for movies: Radarr is TMDB-native, its id IS the primary key
    assert src.count('if media_type != "movie" and tvdb_id') == 2


def test_the_owner_pin_is_never_overruled_by_the_authority_check():
    """A "Fix match" click is the highest authority in the chain. If the tvdb
    lookup ran on a pinned entity it would quietly undo the pin — the exact
    repair the owner reached for when the wrong match appeared."""
    src = _read("services/media_enricher.py")
    assert src.count('not _pin.get("tmdb_id")') == 2


def test_the_on_demand_path_honours_the_pin_at_all():
    """It did not. The pin lived only in fetch_and_prepare_raw, so a pinned
    title stayed pinned in the nightly walk and reverted whenever the judge,
    the chat or a re-eval enriched it live — whichever ran last won."""
    src = _read("services/media_enricher.py")
    assert "def _match_override_ids(" in src
    assert src.count("_pin = _match_override_ids(plex_rating_key)") == 2


def test_a_failed_lookup_never_repoints_an_entry():
    """Transient by contract. A TMDB outage that silently re-pointed items
    would be far worse than the bug being fixed."""
    src = _read("services/media_enricher.py")
    body = src[src.index("async def authoritative_tv_tmdb_id("):]
    body = body[:body.index("\nasync def fetch_tmdb_full")]
    # the except branch returns the caller's id unchanged, and does not cache
    assert "lookup deferred for" in body
    deferred = body.index("lookup deferred for")
    assert "return tmdb_id" in body[deferred:deferred + 200]
    assert body.index("cache.set_cache") > deferred
    # no key, no call
    assert "not settings.TMDB_API_KEY" in body


# ── 2. the refresher must not confirm a blob against itself ───────────────

def test_the_raw_refresher_anchors_on_the_arr_not_on_the_blob():
    """Re-fetching by the ids stored in the row being refreshed, then checking
    the result against that row's own year, always agrees — so a wrong match
    renewed its 30-day TTL forever. Museum of Life was resolved wrongly in May
    and still being refreshed wrongly in August."""
    src = _read("services/raw_refresh.py")
    assert "async def _refresh_one(raw: dict, arr: dict = None)" in src
    assert "_collect_arr_items" in src
    assert "truth.get(_prk)" in src
    # media long gone from the arrs has no record — that fallback must remain
    assert "the stored ids are read" in src or "stored ids only" in src


# ── 3. the delta-check needs its input ────────────────────────────────────

def test_the_judges_path_passes_the_year_to_the_entity_check():
    """enrich_media_item has rejected >5-year entity mismatches since 0e0453f,
    but ensure_verified_data — the function the judge, the pitch loop and the
    chat all reach it through — never forwarded a year, so ``_ty`` was 0 and
    the check passed everything. The guard existed and was starved."""
    enr = _read("services/media_enricher.py")
    head = enr[enr.index("async def ensure_verified_data("):]
    head = head[:head.index(") -> Optional[dict]:")]
    assert "year: Optional[int] = None" in head
    call = enr[enr.index("                enrich_media_item(\n                    title=title"):]
    assert "year=year," in call[:600]
    # and the callers that have a year actually hand it over
    assert 'year=item.get("year"),' in _read("services/pillars.py")
    assert _read("services/recommendations_engine.py").count(
        'year=i.get("year"),\n                        allow_summarizer=False,') == 1


def test_the_deletion_card_path_checks_the_year_too():
    """_fetch_tmdb took ``year`` all along but used it only to disambiguate the
    title SEARCH; the by-ID path took whatever came back. That is how the
    documentary's card shipped wearing the melodrama's plot and poster."""
    src = _read("routers/recommendations.py")
    assert "def _wrong_work(" in src
    assert "if _wrong_work(_entry):" in src
    assert "await authoritative_tv_tmdb_id(" in src


# ── 4. the predicate, and the law behind it ───────────────────────────────

def test_a_misfiled_profile_is_detected_but_romanisations_are_not():
    """Both halves are required. Title divergence alone flags ~50 profiles on
    this library that are simply alternate romanisations or English release
    names; title AND year together flagged 8, and all 8 really were another
    work."""
    import sys
    sys.path.insert(0, str(_SRC.parent))
    from src.services.pillars import _profile_is_another_work

    # the real case
    assert _profile_is_another_work(
        {"title": "Museum of Life", "year": 2010},
        {"title": "Forbidden Love", "year": 1999})
    # the other genuinely wrong ones found in the cache
    assert _profile_is_another_work(
        {"title": "Bungo Stray Dogs", "year": 2016},
        {"title": "Michiko & Hatchin", "year": 2008})
    assert _profile_is_another_work(
        {"title": "Lupin III", "year": 1971},
        {"title": "Lupin the Third: The Woman Called Fujiko Mine", "year": 2012})

    # ...and the ones that must NOT fire: same work, different name
    assert not _profile_is_another_work(
        {"title": "Heavenly Delusion", "year": 2023},
        {"title": "Tengoku Daimakyo", "year": 2023})
    assert not _profile_is_another_work(
        {"title": "Stigma of the Wind", "year": 2007},
        {"title": "Kaze no Stigma", "year": 2007})
    assert not _profile_is_another_work(
        {"title": "Akane-Iro ni Somaru Saka", "year": 2008},
        {"title": "The Hill Dyed Rose Madder", "year": 2008})
    # a release-vs-air-year gap of one is normal, not a mismatch
    assert not _profile_is_another_work(
        {"title": "Some Show", "year": 2019},
        {"title": "Totally Different", "year": 2020})
    # missing data is never a mismatch
    assert not _profile_is_another_work(
        {"title": "Museum of Life", "year": None},
        {"title": "Forbidden Love", "year": 1999})
    assert not _profile_is_another_work({"title": "", "year": 2010},
                                        {"title": "Forbidden Love", "year": 1999})


def test_the_misfiled_record_gate_skips_instead_of_judging():
    """A judge handed another work's plot will notice — and then reason from
    the contradiction. It must never get the chance."""
    pil = _read("services/pillars.py")
    assert '"evidence_mismatched": False' in pil
    assert "_profile_is_another_work(item, vd)" in pil
    eng = _read("services/recommendations_engine.py")
    assert 'if (ev.get("flags") or {}).get("evidence_mismatched"):' in eng
    gate = eng[eng.index('.get("evidence_mismatched"):'):]
    assert "continue" in gate[:700]
    assert "misfiled_skipped" in gate[:700]


def test_the_law_forbids_deleting_a_work_over_our_own_filing_error():
    """The verdict that started this said, of a documentary carrying a
    melodrama's plot: "Removing it resolves the factual error and clears
    space." Both judge and discussion read _PILLARS_BODY, so one edit binds
    both."""
    pil = _read("services/pillars.py")
    body_start = pil.index("_PILLARS_BODY = ")
    body = pil[body_start:pil.index("PILLAR_CONSTITUTION = ")]
    assert "RECORDS vs WORK" in body
    assert "fault in OUR records" in body
    assert "reach NO verdict" in body
    # and it must reach both consumers unchanged
    assert "{_PILLARS_BODY}" in pil[pil.index("PILLAR_CONSTITUTION = "):]
    assert pil.count("{_PILLARS_BODY}") == 2


def test_the_refresher_reads_the_keys_collect_arr_items_actually_writes():
    """_collect_arr_items emits ``media_type`` and ``sonarr_series_type`` —
    reading a ``category`` key would silently type every series as whatever
    the stale blob claimed, which for a misfiled blob is the wrong thing."""
    en = _read("routers/enrichment.py")
    rr = _read("services/raw_refresh.py")
    for key in ('"media_type": cat', '"sonarr_series_type": series_type'):
        assert key in en
    assert 'arr.get("media_type")' in rr
    assert 'arr.get("sonarr_series_type")' in rr
    assert 'arr.get("category")' not in rr


def test_a_drifted_blob_loses_its_own_ids_but_a_matching_one_keeps_them():
    """The arr carries no anilist / anidb / mal id, and for anime those are
    often the only good handle — so a blob that still looks like its item
    keeps them. A blob whose title has drifted must NOT: letting a misfiled
    blob supply its own ids is exactly the closed loop being removed."""
    rr = _read("services/raw_refresh.py")
    assert "misfiled = bool(" in rr
    for field in ("anilist_id", "anidb_id", "mal_id"):
        assert f'"{field}": None if misfiled else raw.get(' in rr
    # ...while the ids the arr does own always come from the arr
    for field in ("tmdb_id", "tvdb_id", "imdb_id"):
        assert f'"{field}": arr.get("{field}")' in rr
