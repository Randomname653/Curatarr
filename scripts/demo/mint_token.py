"""A session token for one demo account, signed with the demo copy's own
JWT_SECRET (a fresh one per build). Run with the copy as the working
directory; writes the token to a file and prints only whom it is for.

    python scripts/demo/mint_token.py <user id> <out file>
"""
import sys
from pathlib import Path

ROOT = Path.cwd()
if not (ROOT / ".curatarr-demo").exists():
    sys.exit("mint_token.py runs only inside a demo copy (scripts/demo/build_demo.py makes one)")
sys.path.insert(0, str(ROOT))
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import User  # noqa: E402
from src.routers.auth import _create_jwt  # noqa: E402

uid = int(sys.argv[1])
with get_db_session() as db:
    u = db.query(User).filter_by(id=uid).first()
    if u is None:
        sys.exit(f"no user {uid} in the demo copy")
    is_admin, name, ver = bool(u.is_admin), u.plex_username, getattr(u, "token_version", 0) or 0
Path(sys.argv[2]).write_text(_create_jwt(uid, is_admin, ver), encoding="utf-8")
print(f"token for user {uid} ({name}) written")
