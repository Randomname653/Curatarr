"""The protection classifier's action line splits only before a field label,
so a title with a pipe in it is protected as one title (2026-09-25).

    python tests/test_protection_line_parse.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.services.episodic_memory as em
from src.database.models import Base, ProtectedMedia

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


TITLE = "Cowboy Bebop | Knockin' on Heaven's Door"
LINE = (f"ACTION: PROTECT_MEDIA | TITLE: {TITLE} | REASON: keep | RESOLUTION: consensus"
        " | CURATOR_STANCE: conceded | OVERRIDE_REASON: - | WATCHLIST: -")

parts = em._split_action_line(LINE)
check("a pipe inside the title does not split the line",
      parts[1].strip() == f"TITLE: {TITLE}" and len(parts) == 7)
check("every field label still starts its own part",
      [p.split(":", 1)[0].strip() for p in parts[2:]] == ["REASON", "RESOLUTION", "CURATOR_STANCE",
                                                          "OVERRIDE_REASON", "WATCHLIST"])
check("labels are matched case-insensitively and without a space before the colon",
      len(em._split_action_line("ACTION: PROTECT_MEDIA|title: X|reason : r")) == 3)
check("a plain line splits as before",
      em._split_action_line("ACTION: PROTECT_MEDIA | TITLE: Lost | REASON: r")[1].strip() == "TITLE: Lost")

out = em._ground_protection_actions(LINE, f"please keep {TITLE}", None)
check("grounding keeps the piped title when the user wrote it", f"TITLE: {TITLE}" in out)
out = em._ground_protection_actions(LINE, "keep it", TITLE)
check("grounding matches the piped anchor and rewrites it whole",
      f"TITLE: {TITLE}" in out and out.count("PROTECT_MEDIA") == 1)

# end to end: the protection row carries the whole title
engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_db_session():
    s = Session()
    try:
        yield s
    finally:
        s.close()


em.get_db_session = fake_db_session
res = asyncio.run(em.handle_protection_intent(1, LINE))
with fake_db_session() as s:
    rows = [r.identifier for r in s.query(ProtectedMedia).all()]
check("handle_protection_intent protects the whole title", rows == [TITLE] and res and TITLE in res)

src = (Path(__file__).resolve().parents[1] / "src/services/episodic_memory.py").read_text(encoding="utf-8")
check("no bare split on the pipe is left in the parser", 'line.split("|")' not in src
      and src.count("_split_action_line(") == 3)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
