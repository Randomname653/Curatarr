"""Enrichment healing, step 3 — the findings inbox (SoulSync repair_findings
contract, MIT): the audit's verdicts become rows with a lifecycle instead of
a counter, and each finding gets exactly ONE automatic requeue before a
human has to look.

    python tests/test_enrichment_findings.py
"""
import pathlib
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import enrichment_state as es


def _mem_session():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.database.models import Base
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)

    @contextmanager
    def fake():
        db = S()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
    return fake, S


def test_recurrence_contract():
    _, S = _mem_session()
    from src.database.models import EnrichmentFinding as F
    t0 = datetime(2026, 9, 6, 12, 0, 0)
    with S() as s:
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year",
                                   service="radarr", arr_id=1, category="movie", title="Blown Away",
                                   detail={"arr_year": 1994, "profile_year": 1992}, now=t0)
        assert a == "created" and row.status == "pending" and row.severity == "warning"
        s.commit()
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year",
                                   detail={"profile_year": 1993}, now=t0 + timedelta(days=1))
        assert a == "refreshed" and row.first_seen == t0 and row.last_seen == t0 + timedelta(days=1)
        assert es._parse_detail(row.detail) == {"arr_year": 1994, "profile_year": 1993}, "detail merged, not replaced"
        assert s.query(F).count() == 1, "one row per (item, kind)"
        # dismissed → silent forever
        row.status, row.dismissed_at = "dismissed", t0
        s.commit()
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year", now=t0 + timedelta(days=30))
        assert a == "silenced_dismissed" and row.status == "dismissed"
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year",
                                   now=t0 + timedelta(days=30), supersede=True)
        assert a == "refreshed" and row.status == "pending", "supersede re-raises a dismissed finding"
        # resolved → silent for the grace week, then re-raised
        row.status, row.resolved_at = "resolved", t0 + timedelta(days=40)
        s.commit()
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year", now=t0 + timedelta(days=43))
        assert a == "silenced_grace" and row.status == "resolved"
        row, a = es.record_finding(s, plex_rating_key="radarr:1", kind="wrong_entity:year", now=t0 + timedelta(days=48))
        assert a == "reopened" and row.status == "pending" and row.resolved_at is None
        # a full pass that no longer detects it resolves it
        n = es.resolve_stale_findings(s, seen=set(), kinds={"wrong_entity"}, now=t0 + timedelta(days=50))
        assert n == 1 and row.status == "resolved" and row.note == "no longer detected"
        assert es.resolve_stale_findings(s, seen=set(), kinds={"zero_rating"}, now=t0) == 0, "only scanned kinds"
        d = es.finding_to_dict(row)
        assert d["base"] == "wrong_entity" and d["label"] == "Wrong entity" and d["detail"]["arr_year"] == 1994


def test_audit_requeues_each_finding_exactly_once_then_escalates():
    import src.routers.enrichment as en
    _, S = _mem_session()
    t0 = datetime(2026, 9, 6, 12, 0, 0)
    hit = ("v2:enriched:movie:radarr:7", "radarr:7", "Good Boy", "movie", "wrong_entity:title",
           {"arr_title": "Good Boy", "profile_title": "Good Boy (2026)"})
    with S() as s:
        st = {}
        assert en._triage_audit_hit(s, hit, t0, st) is True, "first detection: one automatic requeue"
        assert st == {"created": 1}
        row = s.query(__import__("src.database.models", fromlist=["x"]).EnrichmentFinding).one()
        assert es.was_requeued(row) and row.severity == "warning"
        s.commit()
        assert en._triage_audit_hit(s, hit, t0 + timedelta(days=7), st) is False, \
            "second detection: the automatic path did not fix it — escalate, do not purge again"
        assert row.severity == "error" and st["escalated"] == 1 and st["refreshed"] == 1
        # the weekly audit resolves it when it is gone …
        es.resolve_stale_findings(s, seen=set(), kinds={"wrong_entity"}, now=t0 + timedelta(days=14))
        s.commit()
        # … and if it comes back after the grace week it goes straight to a human
        assert en._triage_audit_hit(s, hit, t0 + timedelta(days=30), st) is False
        assert row.status == "pending" and st["reopened"] == 1
        # never-requeue kinds
        pin_hit = ("k", "radarr:8", "X", "movie", "pin_violated", {"id": "tmdb_id", "pinned": 1, "profile": 2})
        assert en._triage_audit_hit(s, pin_hit, t0, st) is False and st["created"] == 2
        # a human settling the item closes its pending findings
        assert es.resolve_item_findings(s, "radarr:8", now=t0, by=1, note="pinned by the owner") == 1
        pend = es.pending_findings(s)
        assert set(pend) == {"radarr:7"} and len(pend["radarr:7"]) == 1


def test_findings_feed_needs_attention():
    A = es.attention_reasons
    f_err = {"kind": "wrong_entity:title", "severity": "error"}
    f_warn = {"kind": "id_conflict:tmdb_id", "severity": "warning"}
    f_info = {"kind": "zero_rating", "severity": "info"}
    assert A("enriched", None, findings=[f_err, f_warn, f_info]) == ["wrong_entity", "id_conflict"], \
        "info-level data-quality findings stay out of the page"
    assert A("enriched", None, findings=[{"kind": "pin_violated", "severity": "error"}]) == ["pin_violated"]
    assert A("not_found", "Not found: no_source_data", 2, has_external_id=False) == \
        ["not_found_repeatedly", "no_external_id"]
    assert A("enriched", None, has_external_id=False) == [], "an enriched item does not need an id"
    assert A("ignored", "ignored by owner", has_external_id=False) == []
    assert list(es.ATTENTION_ORDER)[:3] == ["pin_violated", "wrong_entity", "id_conflict"], \
        "human-only findings sort first"
    assert set(es.FINDING_KINDS) == {"wrong_entity", "id_conflict", "pin_violated", "zero_rating", "malformed"}


def test_wiring():
    en = (_ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
    for needle in ('@router.get("/findings")', '@router.post("/findings/{finding_id}/dismiss")',
                   '@router.post("/findings/{finding_id}/reopen")', "_triage_audit_hit(db, hit, _now, fstats)",
                   "resolve_stale_findings(", "resolve_item_findings(db, f\"{service}:{arr_id}\"",
                   '"pin_violated"', "findings=pend_dicts.get(prk, ())", '"findings_summary"'):
        assert needle in en, needle
    body = en[en.index("async def _audit_enrichments"):en.index("def _triage_audit_hit")]
    assert "for cache_key, *_ in hits:" not in body, "the blanket purge of every hit is gone"
    assert "for cache_key, *_ in to_requeue:" in body
    models = (_ROOT / "src/database/models.py").read_text(encoding="utf-8")
    assert "class EnrichmentFinding(Base)" in models and "uq_enrichment_finding" in models
    fe = (_ROOT / "frontend/index.html").read_text(encoding="utf-8")
    for needle in ("Dismiss finding", "kbDismissFinding", "_kbFindingText", "findings_summary"):
        assert needle in fe, needle




def test_unmatched_page_survives_a_pending_finding():
    """2026-09-06: with ONE pending finding in the table, /unmatched raised
    DetachedInstanceError — the findings summary read f.kind after the
    session had closed and expired the rows. Every Needs-attention load was
    a 500 from the first audit hit on. The summary is built inside the
    session now; the reason filter added the same day is covered too."""
    import asyncio
    from src.routers import enrichment as en
    from src.services import kb_overview as kb
    fake, S = _mem_session()
    with S() as s:
        es.record_finding(s, plex_rating_key="radarr:102", kind="wrong_entity:year",
                          service="radarr", arr_id=102, category="movie", title="Collateral",
                          detail={"arr_year": 2004, "profile_year": 1968},
                          now=datetime(2026, 9, 6, 12, 0, 0))
        s.commit()

    def item(prk, svc, arr_id, title, state, error, attempts):
        return {"category": "movie", "service": svc, "arr_id": arr_id, "plex_rating_key": prk,
                "title": title, "year": 2004, "tmdb_id": 1, "tvdb_id": None, "imdb_id": None,
                "mbid": None, "downloaded": True, "state": state, "has_live": True, "vector": False,
                "attempt_count": attempts, "next_retry_at": None, "error": error,
                "match_basis": None, "match_confidence": None}

    async def fake_items():
        return [item("radarr:102", "radarr", 102, "Collateral", "enriched", None, 0),
                item("radarr:104", "radarr", 104, "The Thing", "not_found", "Not found: no_source_data", 3)]

    orig = (en.get_db_session, kb.classified_items)
    en.get_db_session, kb.classified_items = fake, fake_items
    try:
        page = asyncio.run(en.enrichment_unmatched(user=None))
        assert page["findings_summary"] == {"wrong_entity": 1}
        assert page["total"] == page["total_all"] == 2
        titles = {i["title"] for i in page["items"]}
        assert titles == {"Collateral", "The Thing"}
        only = asyncio.run(en.enrichment_unmatched(reason="not_found_repeatedly", user=None))
        assert only["total"] == 1 and only["total_all"] == 2 and only["reason"] == "not_found_repeatedly"
        assert [i["title"] for i in only["items"]] == ["The Thing"]
        assert only["by_reason"] == page["by_reason"], "chip counts describe the whole set"
    finally:
        en.get_db_session, kb.classified_items = orig

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
