"""SoulSync follows and likes in the music taste vector and the deletion scan.

2026-10-09, owner: the artists he follows in SoulSync (36) are a high-grade
taste signal, and so are his Spotify likes still waiting on SoulSync's
wishlist; the rest of that wishlist is SoulSync's own retry batches and
discovery mixes. A follow counts like a 5-star rating, a like like 4 stars,
on the Plex rating scale the music vector already used; the higher of
rating and signal wins. A followed artist is never proposed for deletion.

    python tests/test_soulsync_taste_signal.py
"""
import asyncio
import json
import math
import pathlib
import sys
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

import src.services.app_state as app_state  # noqa: E402
import src.services.soulsync_client as ss  # noqa: E402
import src.services.taste_engine as te  # noqa: E402
from src.config import settings  # noqa: E402

NOW = datetime(2026, 10, 9, 12, 0)


def test_no_signal_keeps_the_old_weight_low_ratings_included():
    last = NOW - timedelta(days=60)
    for plays, rating in ((1, None), (40, None), (12, 2.0), (12, 10.0)):
        old = (te.recency_weight(last, NOW) * (1.0 + math.log10(max(plays, 1)))
               * ((rating / 6.0) if rating is not None else 1.0))
        assert abs(te.artist_weight(last, plays, rating, NOW) - old) < 1e-12, (plays, rating)


def test_a_follow_counts_like_five_stars_and_never_stacks():
    last = NOW - timedelta(days=5)
    base = te.artist_weight(last, 10, None, NOW)
    assert abs(te.artist_weight(last, 10, None, NOW, te.FOLLOW_FACTOR) / base - 10 / 6) < 1e-9
    assert abs(te.artist_weight(last, 10, None, NOW, te.LIKED_FACTOR) / base - 8 / 6) < 1e-9
    five = te.artist_weight(last, 10, 10.0, NOW)
    assert te.artist_weight(last, 10, 10.0, NOW, te.FOLLOW_FACTOR) == five, "no stacking"
    low = te.artist_weight(last, 10, 2.0, NOW, te.FOLLOW_FACTOR)
    assert low == te.artist_weight(last, 10, 10.0, NOW), "following outranks an old low rating"


def test_a_followed_artist_without_a_play_is_current():
    w = te.artist_weight(None, 0, None, NOW, te.FOLLOW_FACTOR)
    assert abs(w - te.FOLLOW_FACTOR) < 1e-12, w


def _with_state(fn, state):
    real = (app_state.get_state, app_state.set_state, ss.watchlist_artists, ss.liked_song_artists)
    app_state.get_state = lambda k: state.get(k)
    app_state.set_state = lambda k, v: state.__setitem__(k, v)
    settings_real = (settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY)
    settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = "http://soulsync.test", "sk_test"
    try:
        return fn()
    finally:
        (app_state.get_state, app_state.set_state, ss.watchlist_artists,
         ss.liked_song_artists) = real
        settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = settings_real


def test_signals_fetch_once_a_day_and_a_follow_beats_a_like():
    state, calls = {}, []

    async def follows():
        calls.append("watchlist")
        return ["Daft Punk", "Hozier"]

    async def likes():
        calls.append("wishlist")
        return ["Hozier", "Aurora", "Aurora"]

    def run():
        ss.watchlist_artists, ss.liked_song_artists = follows, likes
        first = asyncio.run(te.soulsync_signals())
        second = asyncio.run(te.soulsync_signals())
        return first, second
    first, second = _with_state(run, state)
    assert first == second == {"daft punk": te.FOLLOW_FACTOR, "hozier": te.FOLLOW_FACTOR,
                               "aurora": te.LIKED_FACTOR}, first
    assert calls == ["watchlist", "wishlist"], "the second call used the day-old copy"


def test_a_failed_read_keeps_the_last_good_copy():
    old = {"at": (datetime.utcnow() - timedelta(days=3)).isoformat(),
           "followed": ["Daft Punk"], "liked": ["Aurora"]}
    state = {"soulsync_taste_signals": json.dumps(old)}

    async def silent():
        return None

    def run():
        ss.watchlist_artists, ss.liked_song_artists = silent, silent
        return asyncio.run(te.soulsync_signals())
    out = _with_state(run, state)
    assert out == {"daft punk": te.FOLLOW_FACTOR, "aurora": te.LIKED_FACTOR}, out


def test_without_soulsync_there_are_no_signals():
    real = (settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY)
    settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = None, None
    try:
        assert asyncio.run(te.soulsync_signals()) == {}
    finally:
        settings.SOULSYNC_URL, settings.SOULSYNC_API_KEY = real


def test_the_wiring():
    taste = (_ROOT / "src" / "services" / "taste_engine.py").read_text(encoding="utf-8")
    music = taste[taste.index('    if category == "music":\n        from src.services.soulsync_client'):]
    assert "signals = await soulsync_signals()" in music
    assert "artist_weight(d[\"last\"], d[\"plays\"], d[\"ur\"], now, sig)" in music
    eng = (_ROOT / "src" / "services" / "recommendations_engine.py").read_text(encoding="utf-8")
    assert "_names = await watchlist_artists()" in eng
    assert "if followed and _ss_norm(title) in followed:\n            continue" in eng


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
