"""Lyrics from Plex, step 3: what the curator and the owner see.

The album dossier line, the constitution's quote cap, the discussion block
(SSOT with the constitution), the Music pipeline tab's coverage line and the
docs rows — needles, so a rewrite that drops one fails here.

    python tests/test_lyrics_surfaces.py
"""
import asyncio
import pathlib
import sys
import tempfile

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import lyrics as ly  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="curatarr-lyrics-surfaces-"))


def _seed(db):
    def track(key, album, title, text):
        return ({"ratingKey": key, "grandparentRatingKey": "a1", "parentRatingKey": "al-" + album,
                 "grandparentTitle": "The Band", "parentTitle": album, "title": title, "duration": 1,
                 "Media": [{"Part": [{"Stream": [{"streamType": 4, "codec": "txt", "key": "/s/" + key}]}]}]},
                text)
    items = [track("1", "First Light", "One", b"River runs cold tonight\n"),
             track("2", "First Light", "Two", b"Last train home\n"),
             track("3", "Second – Wind", "Three", b"")]
    tracks, texts = [t for t, _ in items], {"/s/" + t["ratingKey"]: x for t, x in items}

    async def list_tracks(sec):
        return tracks

    async def fetch_text(k):
        return texts.get(k)

    asyncio.run(ly.sync_lyrics([("14", "Music")], list_tracks, fetch_text, db_path=db))


def test_album_line_counts_the_albums_tracks_and_quotes_one_line():
    db = _TMP / "album.db"
    _seed(db)
    assert ly.album_lyrics_line("the band", "First Light", db) == "Lyrics on file: 2/2 tracks"
    assert ly.album_lyrics_line("The Band", "Second - Wind", db) == "Lyrics on file: 0/1 tracks", "dash-folded title match, no text yet"
    assert ly.album_lyrics_line("The Band", "Nope", db) is None and ly.album_lyrics_line("Other", "First Light", db) is None
    prof = {"lyrical_profile": "Leaving songs sung to a lost friend.", "themes": ["leaving home", "grief"],
            "languages": ["English"], "explicit": False, "explicit_note": "", "motifs": [], "tone": [],
            "quotes": ["River runs cold tonight"]}
    ly._store_profile({"artist_key": "a1", "artist": "The Band"}, prof, {"with_text": 2, "tracks": 3, "shown": 2}, db, ly.datetime(2026, 9, 14))
    line = ly.album_lyrics_line("The Band", "First Light", db)
    assert line == 'Lyrics on file: 2/2 tracks; artist\'s lyrics: Leaving songs sung to a lost friend. — a line: "River runs cold tonight"', line


def test_the_curator_is_told_how_to_use_lyrics():
    chat = (_ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
    assert "LYRICS: when the verified block carries a Lyrics line" in chat
    assert "quote at most two short lines, never a\nverse or a whole song" in chat
    assert "Without a Lyrics line say nothing about the words" in chat
    from src.services.app_context import DISCUSSION_UI_BLOCK
    assert "Lyrics evidence" in DISCUSSION_UI_BLOCK and "at most two short lines" in DISCUSSION_UI_BLOCK
    dossier = (_ROOT / "src/services/album_dossier.py").read_text(encoding="utf-8")
    assert "album_lyrics_line" in dossier


def test_the_music_tab_and_the_docs_carry_the_coverage():
    status = (_ROOT / "src/routers/music.py").read_text(encoding="utf-8")
    assert "lyrics_coverage" in status and '"lyrics":' in status
    js = (_ROOT / "frontend/js/music.js").read_text(encoding="utf-8")
    assert "music-lyrics-line" in js and "artists profiled" in js
    markup = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    assert 'id="music-lyrics-line"' in markup
    arch = (_ROOT / "ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "| Lyrics from Plex |" in arch and "**Lyrics (2026-09-14, `src/services/lyrics.py`).**" in arch
    usage = (_ROOT / "docs/USAGE.md").read_text(encoding="utf-8")
    assert "| Lyrics on file, artists profiled |" in usage
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "**The curator reads lyrics.**" in changelog


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
