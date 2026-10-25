"""Numbers and dates in the user's language (2026-10-25).

The front end formatted with the browser's locale: a German browser showed
"1.055GB" next to the curator's "3,152 GB". Both follow the language the user
writes to the curator in now (services/user_language), delivered with the
login (/api/auth/status "locale") and used by every toLocale* call (ui.loc).

    python tests/test_user_locale.py
"""
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from src.database.models import Base, ConversationMessage as M, User  # noqa: E402
import src.services.user_language as ul  # noqa: E402

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
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
    finally:
        db.close()


ul.get_db_session = fake_session
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="anna", is_admin=True))
db.add(User(id=2, plex_user_id="2", plex_username="ben"))
db.add(User(id=3, plex_user_id="3", plex_username="cleo"))
for text in ("is that a classic movie that everybody has seen?", "give me the raw metadata", "sounds valid"):
    db.add(M(user_id=1, role="user", content=text, thread_id="t1"))
for text in ("ist das nicht ein Klassiker, den man gesehen haben muss?", "das ist mir zu langweilig",
             "und was sagt die Kritik dazu?", "ok, lösch es"):
    db.add(M(user_id=2, role="user", content=text, thread_id="t2"))
db.commit()
db.close()

check("a user who writes English gets English numbers", ul.user_locale(1) == "en-US")
check("a user who writes mostly German gets German ones", ul.user_locale(2) == "de-DE")
check("no messages yet: English", ul.user_locale(3) == "en-US")
import time  # noqa: E402
ul._cache[2] = ("fr", time.monotonic() - ul._TTL_S - 1)
check("the language is read again after an hour", ul.user_language(2) == "de")

auth = (ROOT / "src/routers/auth.py").read_text(encoding="utf-8")
check("the login and the session status carry the locale",
      '"locale": user_locale(user.id),' in auth and '"locale": await run_read(user_locale, user_id)' in auth)

js_dir = ROOT / "frontend/js"
bare = []
for p in sorted(js_dir.glob("*.js")):
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if re.search(r"\.toLocale(?:String|DateString|TimeString)\((?!loc\(\))", line):
            bare.append(f"{p.name}:{n}")
check("every number and date is formatted in the user's locale, none in the browser's",
      not bare)
if bare:
    print("        ", bare[:10])
ui = (js_dir / "ui.js").read_text(encoding="utf-8")
auth_js = (js_dir / "auth.js").read_text(encoding="utf-8")
check("setUser sets the locale before anything renders",
      "export const loc = () => _locale;" in ui
      and auth_js.index("setLocale(u.locale);") < auth_js.index("loadUnreadMessages();"))

node = shutil.which("node")
if node:
    r = subprocess.run([node, "-e", "console.log((1055).toLocaleString('de-DE') + '|' + (1055).toLocaleString('en-US'))"],
                       capture_output=True, text=True)
    check("de-DE writes 1.055, en-US 1,055 (the case seen live)", r.stdout.strip() == "1.055|1,055")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
