"""Binge questions group by DAY (2026-10-21).

The verification session asks about series watched 3+ episodes in one day.
The query grouped by CAST(viewed_at AS DATE), and SQLite casts a timestamp
to its YEAR: three episodes on three different days counted as one binge,
and SQLAlchemy's Date column then choked on the integer 2026 ("fromisoformat:
argument must be str"), so post-enrichment verification failed whenever a
series qualified.

Throwaway in-memory DB.

    python tests/test_binge_question.py
"""
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.verification_session as vs  # noqa: E402
from src.database.models import Base, User, WatchHistoryEntry  # noqa: E402

from _checks import check  # noqa: E402


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


vs.get_db_session = fake_session

# noon two days ago: three episodes within an hour stay on one calendar day
binge_day = (datetime.utcnow() - timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    for i in range(3):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"b{i}", title=f"Ep {i}",
                                 series_title="Binged Show", media_type="show", completed=True,
                                 viewed_at=binge_day + timedelta(minutes=20 * i)))
    for i in range(3):   # three episodes, three different days: no binge
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"s{i}", title=f"Ep {i}",
                                 series_title="Spread Show", media_type="show", completed=True,
                                 viewed_at=binge_day - timedelta(days=i + 1)))

try:
    qs = vs._verification_questions(1)
    ok = True
except TypeError as e:
    qs, ok = [], False
    print(f"  ({e})")
check("the questions build when a series qualifies as a binge (no Date-column crash)", ok)
binges = {q["title"]: q["plays"] for q in qs if q["type"] == "binge"}
check("three episodes on one day are a binge", binges.get("Binged Show") == 3)
check("three episodes on three days are not", "Spread Show" not in binges)

check.done()
