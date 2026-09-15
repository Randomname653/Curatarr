"""Series editions: do we own the uncensored cut, does one exist?

Fake Sonarr, fake AniDB lookup, an in-memory DB. The classification
rules, the walker's cursor and budget, the verified-block line, the upgrade
rows and the on-demand release filter.

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


def _file(scene=None, path=None, cfs=()):
    return {"sceneName": scene, "relativePath": path, "customFormats": [{"id": i, "name": n} for i, n in enumerate(cfs)]}


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


def test_release_names_and_custom_formats_classify_the_files():
    c = ed.classify_files([
        _file("[Group] Show - 01 [BD 1080p Uncensored]"),
        _file(path="Season 01/Show - S01E02 - [WEBDL-1080p][Censored]-Grp.mkv"),
        _file("Show.S01E03.1080p.WEB", cfs=("Uncensored", "x265")),
        _file("Show.S01E04.UNCUT.1080p"),
        _file("Show.S01E05.1080p"),
        "not a dict",
    ])
    assert (c["episode_files"], c["files_uncensored"], c["files_censored"]) == (5, 3, 1), c
    assert c["custom_formats"] == "Uncensored, x265" and c["sample_release"].startswith("[Group] Show - 01")
    assert ed.classify_files([]) == {"episode_files": 0, "files_uncensored": 0, "files_censored": 0, "custom_formats": "", "sample_release": ""}
    assert ed.classify_files([_file("Show.Uncensored.S01E01")])["files_censored"] == 0, "uncensored is not censored"


def test_anidb_flags_come_from_the_offline_tags():
    lk = _lookup({"KissXSis": ["ecchi", "censored uncensored version"], "Air Gear": ["excessive censoring"], "Plain": ["comedy"]})
    assert ed.anidb_flags("KissXSis", 2010, lookup=lk) == (True, True)
    assert ed.anidb_flags("Air Gear", 2006, lookup=lk) == (False, True)
    assert ed.anidb_flags("Plain", None, lookup=lk) == (False, False)
    assert ed.anidb_flags("Unknown", None, lookup=lk) == (False, False)
    assert ed.anidb_flags("Boom", None, lookup=lambda **k: (_ for _ in ()).throw(RuntimeError("x"))) == (False, False)


def test_the_walker_checks_series_once_a_week_with_cursor_and_budget():
    _reset()
    series = [{"id": 3, "title": "KissXSis", "seriesType": "anime", "year": 2010, "tvdbId": 1, "titleSlug": "kissxsis"},
              {"id": 1, "title": "Plain Show", "seriesType": "standard", "year": 2019, "tvdbId": 2, "titleSlug": "plain-show"},
              {"id": 2, "title": "Broken", "seriesType": "standard", "year": 2020, "tvdbId": 3, "titleSlug": "broken"}]
    files = {3: [_file("KissXSis - 01 [TV Censored]"), _file("KissXSis - 02 [TV Censored]")], 1: [_file("Plain.S01E01.1080p")]}
    sonarr = _Sonarr(series, files, fail_ids={2})
    lk = _lookup({"KissXSis": ["censored uncensored version"]})
    now = datetime(2026, 9, 15, 12, 0, 0)
    res = asyncio.run(ed.sync_editions(sonarr, budget=2, now=now, lookup=lk, get_state=_gs, set_state=_ss))
    assert res == {"series": 3, "todo": 3, "checked": 1, "failed": 1, "left": 1, "done": False}, res
    assert sonarr.calls == [1, 2] and _STATE["editions_cursor"] == "2", "id order, the cursor after the failed one too"
    res = asyncio.run(ed.sync_editions(sonarr, budget=2, now=now, lookup=lk, get_state=_gs, set_state=_ss))
    assert res["checked"] == 1 and res["done"] is True and _STATE["editions_cursor"] == "0"
    kiss = ed.edition_for(3)
    assert kiss["category"] == "anime" and kiss["files_censored"] == 2 and kiss["anidb_uncensored"] is True and kiss["title_slug"] == "kissxsis"
    plain = ed.edition_by_title("plain show", "show")
    assert plain["episode_files"] == 1 and plain["anidb_uncensored"] is False
    assert ed.edition_for(2) is None, "the failed one is not written"
    # a fresh row is skipped next run; the failed one is retried
    sonarr.calls.clear()
    res = asyncio.run(ed.sync_editions(sonarr, budget=10, now=now + timedelta(hours=1), lookup=lk, get_state=_gs, set_state=_ss))
    assert sonarr.calls == [2] and res["failed"] == 1
    sonarr.fail_ids.clear()
    res = asyncio.run(ed.sync_editions(sonarr, budget=10, now=now + timedelta(days=8), lookup=lk, get_state=_gs, set_state=_ss))
    assert res["checked"] == 3, "a week later everything is checked again"


def test_the_curator_line_and_the_upgrade_rows():
    _reset()
    assert ed.edition_line(None) is None
    assert ed.edition_line({"episode_files": 0, "files_uncensored": 0, "files_censored": 0, "custom_formats": "",
                            "anidb_uncensored": False, "anidb_censored": False}) is None
    line = ed.edition_line({"episode_files": 24, "files_uncensored": 0, "files_censored": 24, "custom_formats": "",
                            "anidb_uncensored": True, "anidb_censored": True})
    assert line == "24 of 24 episode files named censored (the TV cut); AniDB: the TV airing was censored and an uncensored version exists", line
    line = ed.edition_line({"episode_files": 12, "files_uncensored": 12, "files_censored": 0, "custom_formats": "Uncensored",
                            "anidb_uncensored": True, "anidb_censored": False})
    assert line.startswith("12 of 12 episode files named uncensored; custom formats: Uncensored; AniDB:")
    line = ed.edition_line({"episode_files": 5, "files_uncensored": 0, "files_censored": 0, "custom_formats": "",
                            "anidb_uncensored": False, "anidb_censored": True})
    assert line == "5 episode files on disk, none named uncensored; AniDB: the airing was censored"
    with _sess() as db:
        db.add(SeriesEdition(service="sonarr", arr_id=3, title="KissXSis", category="anime", title_slug="kissxsis",
                             episode_files=24, files_uncensored=0, files_censored=24, anidb_uncensored=True,
                             sample_release="KissXSis - 01 [TV]", checked_at=datetime.utcnow()))
        db.add(SeriesEdition(service="sonarr", arr_id=4, title="Owned", category="anime", title_slug="owned",
                             episode_files=12, files_uncensored=12, anidb_uncensored=True, checked_at=datetime.utcnow()))
        db.add(SeriesEdition(service="sonarr", arr_id=5, title="Plain", category="show", title_slug="plain",
                             episode_files=10, anidb_uncensored=False, checked_at=datetime.utcnow()))
    from src.config import settings
    old = settings.SONARR_URL
    settings.SONARR_URL = "http://sonarr:8989"
    try:
        rows = ed.upgrade_rows()
    finally:
        settings.SONARR_URL = old
    assert [r["title"] for r in rows] == ["KissXSis"], "owned uncensored and plain shows are not upgrade candidates"
    r = rows[0]
    assert r["kind"] == "edition" and r["arr_id"] == 3 and r["weakness"] == "TV cut — uncensored version exists"
    assert "24 named censored" in r["love_reason"] and "sample: KissXSis - 01 [TV]" in r["love_reason"]
    assert r["arr_url"] == "http://sonarr:8989/series/kissxsis"


def test_the_release_search_keeps_only_uncensored_offers():
    rels = {(3, 1): [{"title": "[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "indexer": "Nyaa", "size": 12e9, "seeders": 40},
                     {"title": "[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "indexer": "Nyaa", "size": 12e9, "seeders": 40},
                     {"title": "KissXSis S01 1080p WEB", "indexer": "X", "size": 5e9, "customFormats": [{"name": "Uncensored"}]},
                     {"title": "KissXSis S01 720p TV", "indexer": "X", "size": 3e9}],
            (3, 2): "not a list"}
    out = asyncio.run(ed.check_releases(_Sonarr([], {}, rels), 3))
    assert [(o["title"], o["indexer"], o["season"]) for o in out] == [
        ("[Grp] KissXSis 01-12 [BD 1080p Uncensored]", "Nyaa", 1), ("KissXSis S01 1080p WEB", "X", 1)]
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
