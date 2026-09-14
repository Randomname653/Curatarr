"""Lyrics from Plex, step 2: the profile walker.

One summariser call per artist with enough lyrics on file; the profile is
stored, attached to the artist's raw cache entries, the polished summary is
expired and re-polished. Fake LLM, fake re-polish, a real MetadataCache in a
temp file — nothing here touches the network or the app DB.

    python tests/test_lyrics_profile.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.services import lyrics as ly  # noqa: E402
from src.services.media_enricher import _lyrics_line, _lyrics_prompt_block, SUMMARIZE_MUSIC_PROMPT  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-lyrics-profile-"))


def _db(name):
    return _TMP / f"{name}.db"


def _track(key, artist_key, title, stream, album_key="al1", artist="The Band", album="First"):
    return {"ratingKey": key, "grandparentRatingKey": artist_key, "parentRatingKey": album_key,
            "grandparentTitle": artist, "parentTitle": album, "title": title, "duration": 200000,
            "Media": [{"Part": [{"Stream": [{"streamType": 4, "codec": "txt", "key": stream}]}]}]}


def _seed(name, artists):
    """artists = {artist_key: (name, [(album, n_tracks)])} → a lyrics.db with text on every track."""
    tracks, texts = [], {}
    for akey, (aname, albums) in artists.items():
        for ai, (album, n) in enumerate(albums):
            for i in range(n):
                key = f"{akey}-{ai}-{i}"
                skey = f"/library/streams/{key}"
                tracks.append(_track(key, akey, f"Song {ai}.{i}", skey, album_key=f"{akey}-al{ai}", artist=aname, album=album))
                texts[skey] = f"Line one of {aname} {album} {i}\nRiver runs cold tonight\nChorus {i}\n".encode()

    async def list_tracks(sec):
        return tracks

    async def fetch_text(k):
        return texts.get(k)

    asyncio.run(ly.sync_lyrics([("14", "Music")], list_tracks, fetch_text, db_path=_db(name)))
    return _db(name)


_GOOD = {"lyrical_profile": "Small-town leaving songs sung to a lost friend; plain words, river imagery.",
         "themes": ["leaving home", "grief turned into anger", "rivers as time"],
         "languages": ["English"], "explicit": False, "explicit_note": "",
         "motifs": ["cold rivers", "last trains"], "tone": ["melancholic", "Nostalgic", "not-a-mood"],
         "quotes": ["River runs cold tonight", "This line was never in the lyrics", "“River runs cold tonight”"]}


class _LLM:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    async def __call__(self, prompt):
        self.prompts.append(prompt)
        return self.answers.pop(0) if self.answers else _GOOD


class _Repolish:
    def __init__(self):
        self.calls = []

    async def __call__(self, artist, artist_key, mbid):
        self.calls.append((artist, artist_key, mbid))


def test_eligibility_needs_enough_text_and_reacts_to_growth():
    db = _seed("elig", {"a1": ("Eight", [("A", 8)]), "a2": ("Half", [("A", 6)]), "a3": ("Big", [("A", 30)]),
                        "a4": ("Tiny", [("A", 2)])})
    con = ly._connect(db)
    con.execute("UPDATE track_lyrics SET text=NULL, lines=0 WHERE artist_key='a2' AND title IN ('Song 0.0','Song 0.1','Song 0.2')")
    con.execute("UPDATE track_lyrics SET text=NULL, lines=0 WHERE artist_key='a3' AND title <> 'Song 0.0'")
    con.execute("UPDATE track_lyrics SET text=NULL, lines=0 WHERE artist_key='a4' AND title='Song 0.0'")
    con.commit(); con.close()
    keys = sorted(a["artist_key"] for a in ly.eligible_artists(db))
    assert keys == ["a1", "a2"], keys          # 8 texts; 3 of 6 = half; 1 of 30 and 1 of 2 = no profile from one song
    ly._store_profile({"artist_key": "a1", "artist": "Eight"}, _GOOD, {"with_text": 8, "tracks": 8, "shown": 8}, db, ly.datetime(2026, 9, 14))
    assert [a["artist_key"] for a in ly.eligible_artists(db)] == ["a2"], "profiled: no longer eligible"
    ly._store_profile({"artist_key": "a1", "artist": "Eight"}, _GOOD, {"with_text": 6, "tracks": 8, "shown": 6}, db, ly.datetime(2026, 9, 14))
    assert "a1" in [a["artist_key"] for a in ly.eligible_artists(db)], "8 >= 6 * 1.25: texts grew, profile again"


def test_input_round_robins_albums_and_puts_listened_tracks_first():
    db = _seed("input", {"a1": ("The Band", [("First", 30), ("Second", 3)])})
    block, shown, used = ly._profile_input("a1", db, listens={"a1-0-7": 5, "a1-1-2": 9})
    assert shown == ly._MAX_TRACKS and block.startswith("<<<UNTRUSTED_SOURCE:lyrics>>>")
    assert used[0]["title"] == "Song 0.7" and used[1]["title"] == "Song 1.2", "most-played first per album"
    assert [u["album"] for u in used[:4]] == ["First", "Second", "First", "Second"], "album round-robin"
    assert sum(1 for u in used if u["album"] == "Second") == 3


def test_clean_profile_keeps_only_verbatim_quotes_and_known_moods():
    block = "<<<UNTRUSTED_SOURCE:lyrics>>>\n## Song\nRiver runs cold tonight\n<<<END_UNTRUSTED_SOURCE>>>"
    p = ly._clean_profile(_GOOD, block)
    assert p["quotes"] == ["River runs cold tonight"], p["quotes"]
    assert p["tone"] == ["melancholic", "nostalgic"] and p["explicit"] is False
    assert ly._clean_profile({"lyrical_profile": "x", "themes": ["one"]}, block) is None, "two themes minimum"
    assert ly._clean_profile("garbage", block) is None
    long_quote = {**_GOOD, "quotes": ["River runs cold tonight " * 4]}
    assert ly._clean_profile(long_quote, block)["quotes"] == [], "twelve words at most"


def test_run_profiles_attaches_expires_and_repolishes_then_rests():
    db = _seed("run", {"a1": ("The Band", [("First", 10)]), "a2": ("Other", [("A", 1)])})   # a2: not eligible
    cache = MetadataCache(cache_path=_TMP / "run-cache.db")
    cache.set_cache("raw:music:The Band", {"title": "The Band", "mbid": "mb-1", "bio": "x"}, days=30)
    cache.set_cache("enriched:music:The Band", {"title": "The Band", "source": "musicbrainz+lastfm+llm"}, days=30)
    llm, rep = _LLM([_GOOD]), _Repolish()
    done = asyncio.run(ly.run_lyrics_profiles(db_path=db, call_llm=llm, repolish=rep, cache=cache))
    assert done and len(llm.prompts) == 1 and rep.calls == [("The Band", "a1", "mb-1")]
    assert "ARTIST: The Band" in llm.prompts[0] and "10 of 10 tracks have lyrics" in llm.prompts[0]
    raw = cache.get_cache("raw:music:The Band")["response"]
    assert raw["lyrics_v"] == ly._LYRICS_PROMPT_VERSION and raw["lyrics"]["themes"][0] == "leaving home"
    assert raw["lyrics"]["based_on"] == {"with_text": 10, "tracks": 10, "shown": 10}
    assert raw["mbid"] == "mb-1", "write_fields patches, never replaces"
    assert cache.get_cache("enriched:music:The Band") is None, "the polished summary is expired for the re-polish"
    assert ly.stored_profile("a1", db)["profile"]["quotes"] == ["River runs cold tonight"]
    cov = ly.lyrics_coverage(db)
    assert cov["profiles"] == 1 and cov["explicit_artists"] == 0
    # second run: nothing eligible, nothing called, nothing re-attached
    done = asyncio.run(ly.run_lyrics_profiles(db_path=db, call_llm=llm, repolish=rep, cache=cache))
    assert done and len(llm.prompts) == 1 and len(rep.calls) == 1
    # the raw entry was re-pulled and lost the field: re-attached without an LLM call
    cache.set_cache("raw:music:The Band", {"title": "The Band", "mbid": "mb-1", "bio": "fresh"}, days=30)
    asyncio.run(ly.run_lyrics_profiles(db_path=db, call_llm=llm, repolish=rep, cache=cache))
    assert cache.get_cache("raw:music:The Band")["response"]["lyrics_v"] == ly._LYRICS_PROMPT_VERSION
    assert len(llm.prompts) == 1 and len(rep.calls) == 1
    cache.close()


def test_budget_and_unusable_answers():
    db = _seed("budget", {"a1": ("One", [("A", 8)]), "a2": ("Two", [("A", 8)]), "a3": ("Three", [("A", 8)])})
    cache = MetadataCache(cache_path=_TMP / "budget-cache.db")
    llm, rep = _LLM(["not json at all", _GOOD, _GOOD]), _Repolish()
    done = asyncio.run(ly.run_lyrics_profiles(db_path=db, call_llm=llm, repolish=rep, cache=cache, budget=2))
    assert done is False and len(llm.prompts) == 2 and len(rep.calls) == 1, "one garbage answer, one profile, one left"
    assert len(ly.all_profiles(db)) == 1
    done = asyncio.run(ly.run_lyrics_profiles(db_path=db, call_llm=llm, repolish=rep, cache=cache, budget=2))
    assert done is True and len(ly.all_profiles(db)) == 3, "the garbage one is retried, the third one done"
    cache.close()


def test_the_summariser_prompt_reads_the_profile_or_its_absence():
    assert "LYRICS PROFILE: {lyrics}" in SUMMARIZE_MUSIC_PROMPT
    assert "come ONLY from LYRICS PROFILE" in SUMMARIZE_MUSIC_PROMPT
    assert _lyrics_prompt_block({"bio": "x"}) == "none on file — make no claims about the words"
    block = _lyrics_prompt_block({"lyrics": {**_GOOD, "based_on": {"with_text": 10, "tracks": 12}}})
    assert block.startswith("<<<UNTRUSTED_SOURCE:lyrics_profile>>>") and "(10 of 12 tracks on file)" in block
    assert "themes: leaving home, grief turned into anger, rivers as time" in block and "explicit: no" in block
    label, text = _lyrics_line({**_GOOD, "explicit": True, "based_on": {"with_text": 10, "tracks": 12}})
    assert label == "Lyrics (10 of 12 tracks on file)" and text.endswith("Explicit: yes.")
    assert _lyrics_line(None) == (None, None)


def test_the_custodian_runs_the_profiler_behind_the_gpu_gate():
    src = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    tail = src.split('Task("lyrics_profile"')[1][:200]
    assert "needs_llm=True" in tail and "takes_task=True" in tail
    assert src.index('Task("lyrics_sync"') < src.index('Task("lyrics_profile"'), "collect before profile"


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
