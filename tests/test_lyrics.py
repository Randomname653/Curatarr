"""Lyrics from Plex, step 1: the collector.

The walker lists tracks with their lyric streams, fetches each text once
per (track, stream key), re-checks every run so SoulSync's trickle lands,
and marks tracks gone only after a COMPLETE listing. Fake Plex throughout —
the shapes are the ones the live probe of 2026-09-14 returned.

    python tests/test_lyrics.py
"""
import asyncio
import pathlib
import sys
import tempfile
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import lyrics as ly  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-lyrics-"))


def _db(name):
    return _TMP / f"{name}.db"


def _track(key, artist_key, title, stream=None, fmt="lrc", album_key="al1", artist="Artist", album="Album"):
    streams = [{"streamType": 2, "codec": "flac"}]
    if stream:
        streams.append({"streamType": 4, "codec": fmt, "key": stream})
    return {"ratingKey": key, "grandparentRatingKey": artist_key, "parentRatingKey": album_key,
            "grandparentTitle": artist, "parentTitle": album, "title": title, "duration": 200000,
            "Media": [{"Part": [{"Stream": streams}]}]}


class _Plex:
    """Scripted Plex: sections -> tracks (None = listing broke), stream key -> bytes."""

    def __init__(self, tracks, texts):
        self.tracks, self.texts, self.fetches = tracks, texts, []

    async def list_tracks(self, sec_key):
        return self.tracks.get(sec_key)

    async def fetch_text(self, stream_key):
        self.fetches.append(stream_key)
        return self.texts.get(stream_key)


_LRC = b"[ar:Someone]\n[ti:Song]\n[00:01.00]First line\n[00:05.20][01:10.00]Chorus line\n\n[00:09.00]<00:09.10>Word <00:09.50>timed\n"


def _run(plex, sections=(("14", "Music"),), **kw):
    return asyncio.run(ly.sync_lyrics(list(sections), plex.list_tracks, plex.fetch_text, **kw))


def test_lrc_and_txt_parse_to_plain_lines():
    assert ly.parse_lyrics(_LRC) == ["First line", "Chorus line", "Word timed"]
    assert ly.parse_lyrics(b"\xef\xbb\xbfLine one\r\n\r\nLine two\r\n", "txt") == ["Line one", "Line two"]
    assert ly.parse_lyrics(b"[00:01.00]\xe9t\xe9") == ["été"], "cp1252 fallback for a non-UTF-8 sidecar"
    assert ly.parse_lyrics(b"[offset:+500]\n[length:03:20]\n") == []


def test_first_run_fetches_streams_and_stamps_the_rest():
    plex = _Plex({"14": [_track("1", "a1", "One", "/library/streams/11"),
                        _track("2", "a1", "Two"),
                        _track("3", "a2", "Three", "/library/streams/33", fmt="txt", artist="Other")]},
                 {"/library/streams/11": _LRC, "/library/streams/33": b"plain\nwords\n"})
    res = _run(plex, db_path=_db("first"))
    assert res["fetched"] == 2 and res["failed"] == 0 and res["done"] and res["complete"], res
    cov = ly.lyrics_coverage(_db("first"))
    assert (cov["tracks"], cov["with_stream"], cov["with_text"], cov["artists"], cov["artists_with_text"]) == (3, 2, 2, 2, 2), cov
    assert cov["last_run"] and cov["last_result"]["fetched"] == 2
    idx = {a["artist_key"]: a for a in ly.artist_index(_db("first"))}
    assert idx["a1"]["tracks"] == 2 and idx["a1"]["with_text"] == 1
    t = ly.artist_tracks("a1", _db("first"))
    assert len(t) == 1 and t[0]["text"] == "First line\nChorus line\nWord timed" and t[0]["lines"] == 3


def test_second_run_is_idempotent_and_the_trickle_lands():
    tracks = {"14": [_track("1", "a1", "One", "/library/streams/11"), _track("2", "a1", "Two")]}
    plex = _Plex(tracks, {"/library/streams/11": _LRC, "/library/streams/22": b"later\n"})
    _run(plex, db_path=_db("trickle"))
    n = len(plex.fetches)
    res = _run(plex, db_path=_db("trickle"))
    assert len(plex.fetches) == n and res["fetched"] == 0, "nothing new: nothing fetched"
    # SoulSync dropped a sidecar for track 2 since the last run
    tracks["14"][1] = _track("2", "a1", "Two", "/library/streams/22")
    res = _run(plex, db_path=_db("trickle"))
    assert res["fetched"] == 1 and plex.fetches[-1] == "/library/streams/22"
    assert ly.lyrics_coverage(_db("trickle"))["with_text"] == 2


def test_a_replaced_or_removed_sidecar_never_serves_stale_text():
    tracks = {"14": [_track("1", "a1", "One", "/library/streams/11")]}
    plex = _Plex(tracks, {"/library/streams/11": _LRC, "/library/streams/12": b"new words\n"})
    _run(plex, db_path=_db("stale"))
    tracks["14"][0] = _track("1", "a1", "One", "/library/streams/12")     # Plex re-indexed: new stream id
    res = _run(plex, db_path=_db("stale"))
    assert res["fetched"] == 1 and ly.artist_tracks("a1", _db("stale"))[0]["text"] == "new words"
    tracks["14"][0] = _track("1", "a1", "One")                             # sidecar deleted
    _run(plex, db_path=_db("stale"))
    assert ly.artist_tracks("a1", _db("stale")) == [] and ly.lyrics_coverage(_db("stale"))["with_text"] == 0


def test_budget_exhaustion_continues_next_run():
    tracks = {"14": [_track(str(i), "a1", f"T{i}", f"/library/streams/{i}") for i in range(5)]}
    plex = _Plex(tracks, {f"/library/streams/{i}": b"x\n" for i in range(5)})
    res = _run(plex, db_path=_db("budget"), budget=2)
    assert res["fetched"] == 2 and res["pending"] == 3 and res["done"] is False
    res = _run(plex, db_path=_db("budget"), budget=2)
    assert res["fetched"] == 2 and res["pending"] == 1 and res["done"] is False
    res = _run(plex, db_path=_db("budget"), budget=2)
    assert res["fetched"] == 1 and res["pending"] == 0 and res["done"] is True
    assert ly.lyrics_coverage(_db("budget"))["with_text"] == 5


def test_a_refused_stream_backs_off_a_week_unless_its_key_changes():
    """Live 2026-09-14: 112 of 2,000 streams answered 404 — sidecars SoulSync
    had moved since Plex last scanned. Retrying those every 30 minutes is
    waste; a week later, or the moment Plex lists a new key, is right."""
    t0 = datetime(2026, 9, 14, 12, 0, 0)
    tracks = {"14": [_track("1", "a1", "One", "/library/streams/11")]}
    plex = _Plex(tracks, {})
    res = _run(plex, db_path=_db("fail"), now=t0)
    assert res["failed"] == 1 and res["fetched"] == 0 and res["done"]
    assert ly.lyrics_coverage(_db("fail"))["unreachable"] == 1
    plex.texts["/library/streams/11"] = b"now\n"
    res = _run(plex, db_path=_db("fail"), now=t0 + timedelta(days=1))
    assert res["fetched"] == 0 and len(plex.fetches) == 1, "inside the week: not retried"
    res = _run(plex, db_path=_db("fail"), now=t0 + timedelta(days=8))
    assert res["fetched"] == 1 and ly.lyrics_coverage(_db("fail"))["unreachable"] == 0
    # a fresh 404 whose key then changes is retried at once
    tracks["14"][0] = _track("1", "a1", "One", "/library/streams/12")
    res = _run(plex, db_path=_db("fail"), now=t0 + timedelta(days=9))
    assert res["failed"] == 1
    tracks["14"][0] = _track("1", "a1", "One", "/library/streams/13")
    plex.texts["/library/streams/13"] = b"back\n"
    res = _run(plex, db_path=_db("fail"), now=t0 + timedelta(days=9, hours=1))
    assert res["fetched"] == 1 and ly.artist_tracks("a1", _db("fail"))[0]["text"] == "back"


def test_gone_only_after_a_complete_listing():
    tracks = {"14": [_track("1", "a1", "One", "/library/streams/11"), _track("2", "a1", "Two", "/library/streams/22")],
              "15": [_track("9", "a9", "Nine", artist="Nine", album_key="al9")]}
    plex = _Plex(tracks, {"/library/streams/11": b"a\n", "/library/streams/22": b"b\n"})
    secs = (("14", "Music"), ("15", "Audiobooks"))
    _run(plex, sections=secs, db_path=_db("gone"))
    assert ly.lyrics_coverage(_db("gone"))["tracks"] == 3
    del tracks["14"][1]                                   # track 2 left Plex
    tracks["15"] = None                                   # …but section 15's listing broke this run
    res = _run(plex, sections=secs, db_path=_db("gone"))
    assert res["complete"] is False and res["gone_marked"] == 0
    assert ly.lyrics_coverage(_db("gone"))["tracks"] == 3, "a broken walk never looks like a purge"
    tracks["15"] = [_track("9", "a9", "Nine", artist="Nine", album_key="al9")]
    res = _run(plex, sections=secs, db_path=_db("gone"))
    assert res["complete"] is True and res["gone_marked"] == 1
    cov = ly.lyrics_coverage(_db("gone"))
    assert cov["tracks"] == 2 and cov["with_text"] == 1
    assert [a["artist_key"] for a in ly.artist_index(_db("gone"))] == ["a1", "a9"]


def test_lrc_is_preferred_when_a_track_carries_both_formats():
    item = _track("1", "a1", "One", "/library/streams/txt1", fmt="txt")
    item["Media"][0]["Part"][0]["Stream"].append({"streamType": 4, "codec": "lrc", "key": "/library/streams/lrc1"})
    assert ly._lyric_stream(item) == ("/library/streams/lrc1", "lrc")
    assert ly._lyric_stream(_track("2", "a1", "Two")) == (None, None)


class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _SectionsClient:
    def __init__(self, resp):
        self._resp = resp

    async def get(self, *a, **k):
        if isinstance(self._resp, Exception):
            raise self._resp
        return self._resp


def test_music_sections_come_from_plex_when_libraries_has_none_mapped():
    """The owner's instance maps movies, anime and shows only; the sidecars
    live in Plex regardless, so the collector discovers artist sections."""
    ok = _SectionsClient(_Resp(200, {"MediaContainer": {"Directory": [
        {"key": "10", "title": "Movies", "type": "movie"},
        {"key": "14", "title": "Music", "type": "artist"},
        {"key": "16", "title": "Audiobooks", "type": "artist"}]}}))
    assert asyncio.run(ly._discover_music_sections(ok, "http://plex", {})) == [("14", "Music"), ("16", "Audiobooks")]
    assert asyncio.run(ly._discover_music_sections(_SectionsClient(_Resp(401, {})), "http://plex", {})) == []
    assert asyncio.run(ly._discover_music_sections(_SectionsClient(ConnectionError("down")), "http://plex", {})) == []


def test_the_custodian_runs_the_collector_with_its_activity_card():
    src = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    assert 'Task("lyrics_sync"' in src and "takes_task=True" in src.split('Task("lyrics_sync"')[1][:200]
    assert "run_lyrics_sync" in src
    assert 'needs_llm=True' not in src.split('Task("lyrics_sync"')[1][:200], "the collector never needs the GPU"


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
