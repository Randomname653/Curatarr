"""Not-found backoff (services/enrichment_state.py) — SoulSync's
wishlist_backoff shape: two free tries, then 3 → 6 → 12 → 24 days, capped
at 30 days, forever (owner decision 2026-09-06: never give up).

The Python function and the SQL predicate must agree — the pre-filter
filters in SQL, the classifier decides in Python.

    python tests/test_enrichment_backoff.py
"""
import pathlib
import sys
from datetime import datetime, timedelta

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.services import enrichment_state as es


def test_backoff_table():
    assert [es.retry_delay_days(n) for n in range(0, 9)] == [0, 0, 0, 3, 6, 12, 24, 30, 30]
    assert es.retry_delay_days(40) == 30, "never gives up, never waits longer than a month"
    now = datetime(2026, 9, 6, 12, 0, 0)
    assert es.next_retry_at(1, now) is None and es.next_retry_at(2, now) is None
    assert es.next_retry_at(3, now) == now + timedelta(days=3)
    assert es.next_retry_at(6, now) == now + timedelta(days=24)
    assert es.next_retry_at(9, now) == now + timedelta(days=30)
    assert es.sentinel_cache_days(0) == 3 and es.sentinel_cache_days(5) == 12
    assert es.is_due(None, now)
    assert es.is_due(now - timedelta(seconds=1), now)
    assert not es.is_due(now + timedelta(days=1), now)


def test_due_clause_matches_python():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from src.database.models import Base, EnrichmentStatus

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine)
    now = datetime(2026, 9, 6, 12, 0, 0)
    rows = {"a": None, "b": now - timedelta(hours=1), "c": now,
            "d": now + timedelta(minutes=1), "e": now + timedelta(days=30)}
    with S() as s:
        for k, nra in rows.items():
            s.add(EnrichmentStatus(plex_rating_key=f"radarr:{k}", title=k, media_category="movie",
                                   enriched=True, error="Not found: no_source_data",
                                   next_retry_at=nra))
        s.commit()
        due_sql = {r.plex_rating_key.split(":")[1] for r in
                   s.query(EnrichmentStatus).filter(es.due_clause(EnrichmentStatus, now)).all()}
    due_py = {k for k, nra in rows.items() if es.is_due(nra, now)}
    assert due_sql == due_py == {"a", "b", "c"}, (due_sql, due_py)


def test_migration_declares_columns_index_and_backfill():
    conn = (_ROOT / "src/database/connection.py").read_text(encoding="utf-8")
    models = (_ROOT / "src/database/models.py").read_text(encoding="utf-8")
    for col in ("attempt_count", "last_attempt_at", "next_retry_at", "match_basis", "match_confidence"):
        assert f'"{col}"' in conn, f"{col} missing from _migrate_columns (existing installs)"
        assert f"{col} " in models, f"{col} missing from the model (fresh installs)"
    assert "idx_es_next_retry_at" in conn
    assert "SET attempt_count = 2" in conn and "'+3 days'" in conn, \
        "existing not-found rows must be backfilled once, dated from their own last write"


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
