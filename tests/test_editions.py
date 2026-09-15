"""Series editions: which cut is on disk, does an uncensored one exist?

Fake Sonarr, fake AniDB lookup, an in-memory DB. The classification rules
(quality source, names, custom formats), the AniDB tag semantics, the
walker's cursor / budget / re-walk of rows without a source count, the
verified-block line, the upgrade rows and the on-demand release filter.

    python tests/test_editions.py
"""
import asyncio
import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.database.models import Base, SeriesEdition  # noqa: E402
import src.database.connection as _conn  # noqa: E402
from src.services import editions as ed  # noqa: E402

_engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(_engine)
_Session = sessionmaker(bind=_engine)


@contextmanager
def _sess():
    s = _Session()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


_conn.get_db_session = _sess
_STATE = {}


def _gs(k):
    return _STATE.get(k)


def _ss(k, v):
    _STATE[k] = v


def _reset():
    with _sess() as db:
        db.query(SeriesEdition).delete()
    _STATE.clear()


def _file(scene=None, path=None, cfs=(), source=None):
    f = {"sceneName": scene, "relativePath": path, "customFormats": [{"id": i, "name": n} for i, n in enumerate(cfs)]}
    if source:
        f["quality"] = {"quality": {"id": 1, "name": "x", "source": source}}
    return f


class _Sonarr:
    def __init__(self, series, files, releases=None, fail_ids=()):
        self.series, self.files, self.releases, self.fail_ids = series, files, releases or {}, set(fail_ids)
        self.calls = []

    async def get_series(self):
        return self.series

    async def get_episode_files(self, sid):
        self.calls.append(sid)
        if sid in self.fail_ids:
            raise ConnectionError("boom")
        return self.files.get(sid, [])

    async def search_releases(self, sid, season):
        return self.releases.get((sid, season), [])


def _lookup(tags_by_title):
    def lookup(*, title=None, year=None, **kw):
        t = tags_by_title.get(title)
        return {"tags": t} if t is not None else None
    return lookup


_BASE = {"episode_files": 0, "files_disc": 0, "files_uncensored": 0, "files_censored": 0, "custom_formats": "",
         "anidb_uncensored": False, "anidb_censored": False, "anidb_disc_censored": False, "anidb_tags": ""}


def test_sources_names_and_custom_formats_classify_the_files():
    c = ed.classify_files([
        _file("[Group] Show - 01 [BD 1080p Uncensored]", source="bluray"),
        _file(path="Season 01/Show - S01E02 - [WEBDL-1080p][Censored]-Grp.mkv", source="web"),
        _file("Show.S01E03.1080p.WEB", cfs=("Uncensored", "x265"), source="web"),
        _file("Show.S01E04.UNCUT.1080p", source="television"),
        _file("Show.S01E05.1080p.BluRay"),                       # nothing parsed: the name says disc
        _file("Show.S01E06.1080p", source="dvd"),
        "not a dict",
    ])
    assert (c["episode_files"], c["files_disc"], c["files_uncensored"], c["files_censored"]) == (6, 3, 3, 1), c
    assert c["custom_formats"] == "Uncensored, x265" and c["sample_release"].startswith("[Group] Show - 01")
    assert ed.classify_files([]) == {"episode_files": 0, "files_disc": 0, "files_uncensored": 0, "files_censored": 0,
                                     "custom_formats": "", "sample_release": ""}
    assert ed.classify_files([_file("Show.Uncensored.S01E01")])["files_censored"] == 0, "uncensored is not censored"
    assert ed.classify_files([_file("Show.S01E01.BD.1080p", source="web")])["files_disc"] == 0, "the parsed source wins over the name"
    assert ed.classify_files([_file("Show.S01E01.1080p", source="blurayRaw")])["files_disc"] == 1


def test_anidb_flags_follow_anidbs_own_definitions():
    lk = _lookup({"DxD": ["ecchi", "tv censoring", "censored uncensored version"],
                  "Prison": ["tv censoring"], "Kite": ["uncensored version available"],
                  "Frieren": ["censored uncensored version"], "Another": ["violence censoring"], "Plain": ["comedy"]})
    f = ed.anidb_flags("DxD", 2012, lookup=lk)
    assert (f["uncensored"], f["censored"], f["disc_censored"]) == (True, True, True), f
    assert f["tags"] == "censored uncensored version, tv censoring"
    f = ed.anidb_flags("Prison", 2015, lookup=lk)
    assert (f["uncensored"], f["censored"], f["disc_censored"]) == (True, True, False), "tv censoring: the disc is the uncensored cut"
    f = ed.anidb_flags("Kite", 1998, lookup=lk)
    assert (f["uncensored"], f["censored"], f["disc_censored"]) == (True, False, False)
    f = ed.anidb_flags("Frieren", 2023, lookup=lk)
    assert (f["uncensored"], f["censored"], f["disc_censored"]) == (False, True, True), "the disc keeps censoring is not: an uncensored version exists"
    f = ed.anidb_flags("Another", 2012, lookup=lk)
    assert (f["uncensored"], f["censored"], f["disc_censored"]) == (False, True, False) and f["tags"] == "violence censoring"
    assert ed.anidb_flags("Plain", None, lookup=lk) == ed._NO_FLAGS
    assert ed.anidb_flags("Unknown", None, lookup=lk) == ed._NO_FLAGS
    assert ed.anidb_flags("Boom", None, lookup=lambda **k: (_ for _ in ()).throw(RuntimeError("x"))) == ed._NO_FLAGS


def test_the_walker_checks_series_once_a_week_with_cursor_and_budget():
    _reset()
    series = [{"id": 3, "title": "KissXSis", "seriesType": "anime", "year": 2010, "tvdbId": 1, "titleSlug": "kissxsis"},
              {"id": 1, "title": "Plain Show", "seriesType": "standard", "year": 2019, "tvdbId": 2, "titleSlug": "plain-show"},
              {"id": 2, "title": "Broken", "seriesType": "standard", "year": 2020, "tvdbId": 3, "titleSlug": "broken"}]
    files = {3: [_file("KissXSis - 01 [BD 1080p]", source="bluray"), _file("KissXSis - 02 [TV]", source="television")],
             1: [_file("Plain.S01E01.1080p", source="web")]}
    sonarr = _Sonarr(series, files, fail_ids={2})
    lk = _lookup({"KissXSis": ["tv censoring", "censored uncensored version"]})
    now = datetime(2026, 9, 15, 12, 0, 0)
    res = asyncio.run(ed.sync_editions(sonarr, budget=2, now=now, lookup=lk, get_state=_gs, set_state=_ss))
    assert res == {"series": 3, "todo": 3, "checked": 1, "failed": 1, "left": 1, "done": False}, res
    assert sonarr.calls == [1, 2] and _STATE["editions_cursor"] == "2", "id order, the cursor after the failed one too"
    res = asyncio.run(ed.sync_editions(sonarr, budget=2, now=now, lookup=lk, get_state=_gs, set_state=_ss))
    assert res["checked"] == 1 and res["done"] is True and _STATE["editions_cursor"] == "0"
    kiss = ed.edition_for(3)
    assert kiss["category"] == "anime" and kiss["files_disc"] == 1 and kiss["anidb_uncensored"] is True
    assert kiss["anidb_disc_censored"] is True and kiss["anidb_tags"] == "censored uncensored version, tv censoring"
    assert kiss["title_slug"] == "kissxsis"
    plain = ed.edition_by_title("plain show", "show")
    assert plain["episode_files"] == 1 and plain["files_disc"] == 0 and plain["anidb_uncensored"] is False and plain["anidb_tags"] == ""
    assert ed.edition_for(2) is None, "the failed one is not written"
    # a fresh row is skipped next run; the failed one is retried
    sonarr.calls.clear()
    res = asyncio.run(ed.sync_editions(sonarr, budget=10, now=now + timedelta(hours=1), lookup=lk, get_state=_gs, set_state=_ss))
    assert sonarr.calls == [2] and res["failed"] == 1
    # a row from before the source count existed is stale, however fresh
    with _sess() as db:
        db.query(SeriesEdition).filter(SeriesEdition.arr_id == 1).update({"files_disc": None})
    sonarr.calls.clear()
    res = asyncio.run(ed.sync_editions(sonarr, budget=10, now=now + timedelta(hours=2), lookup=lk, get_state=_gs, set_state=_ss))
    assert sonarr.calls == [1, 2] and ed.edition_for(1)["files_disc"] == 0
    sonarr.fail_ids.clear()
    res = asyncio.run(ed.sync_editions(sonarr, budget=10, now=now + timedelta(days=8), lookup=lk, get_state=_gs, set_state=_ss))
    assert res["checked"] == 3, "a week later everything is checked again"


def test_the_curator_line_names_the_sources_and_anidbs_verdict():
    assert ed.edition_line(None) is None
    assert ed.edition_line(dict(_BASE)) is None
    line = ed.edition_line({**_BASE, "episode_files": 24, "files_disc": 24, "anidb_uncensored": True,
                            "anidb_censored": True, "anidb_tags": "tv censoring"})
    assert line == ("24 episode files, all from Blu-ray/DVD, none named uncensored; "
                    "AniDB: the TV airing was censored, the Blu-ray/DVD release is the uncensored cut"), line
    line = ed.edition_line({**_BASE, "episode_files": 12, "files_censored": 12, "anidb_uncensored": True, "anidb_censored": True,
                            "anidb_disc_censored": True, "anidb_tags": "censored uncensored version, tv censoring"})
    assert line == ("12 episode files, all from broadcast or web, 12 named censored; AniDB: the TV airing was censored, "
                    "the Blu-ray/DVD release is the uncensored cut though it keeps some censoring"), line
    line = ed.edition_line({**_BASE, "episode_files": 24, "files_disc": 12, "files_uncensored": 12,
                            "custom_formats": "Anime Dual Audio, Uncensored, x265 (HD)",
                            "anidb_uncensored": True, "anidb_censored": True, "anidb_tags": "tv censoring"})
    assert line.startswith("24 episode files, 12 from Blu-ray/DVD and 12 from broadcast or web, 12 named uncensored; "
                           "custom formats: Uncensored; AniDB:"), line
    line = ed.edition_line({**_BASE, "episode_files": 13, "files_disc": 13, "anidb_censored": True,
                            "anidb_disc_censored": True, "anidb_tags": "censored uncensored version"})
    assert line == ("13 episode files, all from Blu-ray/DVD, none named uncensored; "
                    "AniDB: even the Blu-ray/DVD release keeps some censoring; no uncensored version known"), line
    line = ed.edition_line({**_BASE, "episode_files": 14, "files_disc": 14, "anidb_censored": True, "anidb_tags": "violence censoring"})
    assert line == "14 episode files, all from Blu-ray/DVD, none named uncensored; AniDB: the airing was censored (violence censoring)", line
    line = ed.edition_line({**_BASE, "episode_files": 1, "files_disc": 1, "anidb_uncensored": True, "anidb_tags": "uncensored version available"})
    assert line == "1 episode file from Blu-ray/DVD, none named uncensored; AniDB: an uncensored version exists", line
    assert ed.edition_line({**_BASE, "episode_files": 5, "files_disc": None}) == "5 episode files on disk, none named uncensored"


def test_the_upgrade_rows_want_broadcast_files_and_an_uncensored_disc():
    _reset()
    stamp = datetime.utcnow()
    with _sess() as db:
        db.add(SeriesEdition(service="sonarr", arr_id=3, title="KissXSis", category="anime", title_slug="kissxsis",
                             episode_files=24, files_disc=0, files_censored=24, anidb_uncensored=True, anidb_censored=True,
                             anidb_disc_censored=True, anidb_tags="censored uncensored version, tv censoring",
                             sample_release="KissXSis - 01 [TV]", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=4, title="Partial", category="anime", title_slug="partial",
                             episode_files=24, files_disc=12, anidb_uncensored=True, anidb_censored=True,
                             anidb_tags="tv censoring", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=5, title="All Disc", category="anime", title_slug="all-disc",
                             episode_files=12, files_disc=12, anidb_uncensored=True, anidb_censored=True,
                             anidb_tags="tv censoring", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=6, title="Owned", category="anime", title_slug="owned",
                             episode_files=12, files_disc=0, files_uncensored=12, anidb_uncensored=True,
                             anidb_tags="tv censoring", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=7, title="Disc Censored", category="anime", title_slug="dc",
                             episode_files=12, files_disc=0, anidb_censored=True, anidb_disc_censored=True,
                             anidb_tags="censored uncensored version", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=8, title="Legacy", category="anime", title_slug="legacy",
                             episode_files=12, files_disc=None, anidb_uncensored=True, anidb_tags="tv censoring", checked_at=stamp))
        db.add(SeriesEdition(service="sonarr", arr_id=9, title="Plain", category="show", title_slug="plain",
                             episode_files=10, files_disc=0, checked_at=stamp))
    from src.config import settings
    old = settings.SONARR_URL
    settings.SONARR_URL = "http://sonarr:8989"
    try:
        rows = ed.upgrade_rows()
    finally:
        settings.SONARR_URL = old
    assert [r["title"] for r in rows] == ["KissXSis", "Partial"], \
        "all-disc, owned, disc-only-censored, not-yet-re-walked and plain rows are no candidates"
    r = rows[0]
    assert r["kind"] == "edition" and r["arr_id"] == 3 and r["weakness"] == "TV cut — uncensored disc release exists"
    assert "AniDB tags it 'tv censoring'" in r["love_reason"] and "24 from broadcast or web, 0 from Blu-ray/DVD" in r["love_reason"]
    assert "(24 named censored)" in r["love_reason"] and "keeps some censoring too" in r["love_reason"]
    assert "sample: KissXSis - 01 [TV]" in r["love_reason"] and r["arr_url"] == "http://sonarr:8989/series/kissxsis"
    assert "_share" not in r
    assert rows[1]["weakness"] == "TV cut in 12 of 24 files — uncensored disc release exists"


def test_the_release_search_keeps_uncensored_and_disc_offers():
    rels = {(3, 1): [{"title": "[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "indexer": "Nyaa", "size": 12e9, "seeders": 40},
                     {"title": "[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "indexer": "Nyaa", "size": 12e9, "seeders": 40},
                     {"title": "KissXSis S01 1080p WEB", "indexer": "X", "size": 5e9, "customFormats": [{"name": "Uncensored"}]},
                     {"title": "KissXSis S01 1080p BluRay x264", "indexer": "X", "size": 9e9, "quality": {"quality": {"source": "bluray"}}},
                     {"title": "KissXSis S01 1080p BDRip", "indexer": "Y", "size": 8e9},
                     {"title": "KissXSis S01 720p TV", "indexer": "X", "size": 3e9},
                     {"title": "KissXSis S01 1080p BluRay Censored", "indexer": "Z", "size": 3e9, "quality": {"quality": {"source": "bluray"}}}],
            (3, 2): "not a list"}
    out = asyncio.run(ed.check_releases(_Sonarr([], {}, rels), 3))
    assert [(o["title"], o["indexer"], o["season"], o["why"]) for o in out] == [
        ("[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "Nyaa", 1, "named uncensored"),
        ("KissXSis S01 1080p WEB", "X", 1, "named uncensored"),
        ("KissXSis S01 1080p BluRay x264", "X", 1, "Blu-ray/DVD source"),
        ("KissXSis S01 1080p BDRip", "Y", 1, "Blu-ray/DVD source")], out
    assert out[0]["size_gb"] == 12.0 and out[1]["custom_formats"] == ["Uncensored"]


def test_the_wiring():
    cust = (_ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
    entry = cust.split('Task("editions_sync"')[1].split("),")[0]
    assert "168.0" in entry and "takes_task=True" in entry and "needs_llm=True" not in entry
    enr = (_ROOT / "src/services/media_enricher.py").read_text(encoding="utf-8")
    assert 'add("Edition", _edl, cap=400)' in enr
    rec = (_ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
    assert "rows = rows + upgrade_rows()" in rec
    lib = (_ROOT / "src/routers/library.py").read_text(encoding="utf-8")
    assert '@router.get("/editions/{series_id}/releases")' in lib
    arr = (_ROOT / "src/services/arr_client.py").read_text(encoding="utf-8")
    assert "async def get_episode_files" in arr and "async def search_releases" in arr
    assert "rate_limit_rpm: int = 20" in arr.split("class SonarrClient")[1][:400], "the walker picks its own pace"
    conn = (_ROOT / "src/database/connection.py").read_text(encoding="utf-8")
    for col in ("files_disc", "anidb_disc_censored", "anidb_tags"):
        assert f'("series_editions",       "{col}"' in conn, col
    js = (_ROOT / "frontend/js/curation.js").read_text(encoding="utf-8")
    assert "act('checkUncensored', p.arr_id, EL)" in js and "export async function checkUncensored" in js
    assert "  checkUncensored," in (_ROOT / "frontend/js/app.js").read_text(encoding="utf-8")


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
