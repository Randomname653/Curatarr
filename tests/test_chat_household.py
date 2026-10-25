"""The owner's free chat knows who else in the household watches a title
(2026-10-25, routers/chat._household_use_block).

The deletion judge and the proposal cards had the other members' use
(pillar III); free chat did not, and called a sitcom another member
watches every evening "a prime candidate for the chopping block". The
owner now gets the raw counts; a member's chat does not (household compare
stays opt-in).

    python tests/test_chat_household.py
"""
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, User, WatchHistoryEntry as W  # noqa: E402
from src.routers.chat import _household_use_block  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
db.add(User(id=1, plex_user_id="1", plex_username="Jane Doe", is_admin=True))
db.add(User(id=2, plex_user_id="2", plex_username="Sam"))
for i in range(5):
    db.add(W(user_id=2, plex_user_id="2", plex_item_id=f"e{i}", title=f"Ep {i}", series_title="The Big Bang Theory",
             media_type="show", season=1, episode=i + 1, viewed_at=datetime(2026, 10, i + 1), completed=True))
db.add(W(user_id=2, plex_user_id="2", plex_item_id="e0", title="Ep 0", series_title="The Big Bang Theory",
         media_type="show", season=1, episode=1, viewed_at=datetime(2026, 10, 8), completed=True))
db.add(W(user_id=1, plex_user_id="1", plex_item_id="x", title="Ep 0", series_title="The Big Bang Theory",
         media_type="show", season=1, episode=1, viewed_at=datetime(2026, 2, 1), completed=True))
db.add(W(user_id=2, plex_user_id="2", plex_item_id="k", title="Knives Out", media_type="movie",
         viewed_at=datetime(2026, 8, 29), completed=True))
db.commit()

b = _household_use_block(1, "The Big Bang Theory", db)
check("the other member's episodes, plays and last play, not the owner's own",
      "HOUSEHOLD USE of 'The Big Bang Theory'" in b and "Sam: 5 distinct episodes (6 plays, last 2026-10-08)" in b
      and "Jane" not in b)
check("...with the rule that the household outranks the owner's taste", "outranks the owner's taste" in b)
check("a film a member finished", "Sam: finished it (1 plays" in _household_use_block(1, "Knives Out", db))
check("nobody else watched it: nothing", _household_use_block(1, "Inception", db) == "")
db.close()

src = (ROOT / "src/routers/chat.py").read_text(encoding="utf-8")
check("free chat adds it for the owner only",
      "if user.is_admin:" in src and "_in_session, _household_use_block, user.id, active_title" in src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
