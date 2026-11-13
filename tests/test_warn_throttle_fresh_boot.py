"""Once-a-minute warning throttles warn on a freshly booted host.

time.monotonic() counts seconds since boot on Linux. A throttle whose
"last warned at" started at 0.0 compared ``now - 0.0 > 60`` and swallowed
the first warning on any host up for less than a minute: a new container,
a CI runner. The marker now starts as None. Each throttle here runs with
time.monotonic() pinned to 30.0 (a host up for 30 s): the first failure
warns, a repeat inside the window does not, and one past it warns again.

(media_enricher's OMDb budget warning and library's wish lookup are
covered in test_fail_closed_s2_g2 / _g4.)
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import logging
import time
from types import SimpleNamespace

app_db()


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def throttle_case(label, logger_name, module, marker, trigger, text):
    """Fire ``trigger`` at boot+30 s twice, then at boot+91 s; count the
    WARNINGs carrying ``text``."""
    h = _Capture()
    lg = logging.getLogger(logger_name)
    old_level = lg.level
    lg.addHandler(h)
    lg.setLevel(logging.DEBUG)
    real_monotonic = time.monotonic
    setattr(module, marker, None)
    counts = []
    try:
        for clock in (30.0, 30.0, 91.0):
            time.monotonic = lambda c=clock: c
            trigger()
            counts.append(sum(1 for r in h.records
                              if r.levelno >= logging.WARNING and text in r.getMessage()))
    finally:
        time.monotonic = real_monotonic
        lg.removeHandler(h)
        lg.setLevel(old_level)
    check(f"{label}: warns at boot+30 s ({counts})", counts[0] == 1)
    check(f"{label}: ...once a minute, not on every call", counts[1] == 1)
    check(f"{label}: ...and again once the minute is up", counts[2] == 2)


# ── 1. discogs styles snapshot ──────────────────────────────────────────────
print("discogs_offline.artist_styles")
import src.services.discogs_offline as dg  # noqa: E402
from _harness import TMP  # noqa: E402

_broken_db = TMP / "discogs_styles_broken.db"
_broken_db.write_bytes(b"this is not a sqlite database" * 64)
_real_styles = dg.STYLES_DB_PATH
dg.STYLES_DB_PATH = _broken_db
try:
    throttle_case("styles snapshot", "src.services.discogs_offline", dg,
                  "_styles_warned_at", lambda: dg.artist_styles("Crypton"),
                  "styles snapshot unreadable")
finally:
    dg.STYLES_DB_PATH = _real_styles

# ── 2. game flag ────────────────────────────────────────────────────────────
print("app_state.game_active")
from src.services import app_state  # noqa: E402

_real_get = app_state.get_state


def _locked(key):
    raise RuntimeError("database is locked")


app_state.get_state = _locked
try:
    throttle_case("game flag", "src.services.app_state", app_state,
                  "_game_flag_warned_at", app_state.game_active,
                  "game flag unreadable")
finally:
    app_state.get_state = _real_get

# ── 3. lyrics coverage on the music status endpoint ─────────────────────────
print("music.pipeline_status lyrics")
import src.routers.music as music  # noqa: E402
import src.services.lyrics as ly  # noqa: E402

_real_cov = ly.lyrics_coverage


def _cov_down(*a, **k):
    raise RuntimeError("database disk image is malformed")


ly.lyrics_coverage = _cov_down
try:
    throttle_case("lyrics coverage", "src.routers.music", music,
                  "_lyrics_warned_at",
                  lambda: music.pipeline_status(user=SimpleNamespace(id=1)),
                  "lyrics coverage unavailable")
finally:
    ly.lyrics_coverage = _real_cov

# ── 4. vector counts on the enrichment panel ────────────────────────────────
print("enrichment._vector_counts_by_service")
import src.routers.enrichment as enr  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

_real_chroma = cw.get_chroma_db


def _chroma_down():
    raise RuntimeError("chroma unavailable")


cw.get_chroma_db = _chroma_down
try:
    throttle_case("vector counts", "src.routers.enrichment", enr,
                  "_vec_count_warned_at", enr._vector_counts_by_service,
                  "vector counts unavailable")
finally:
    cw.get_chroma_db = _real_chroma

check.done()
