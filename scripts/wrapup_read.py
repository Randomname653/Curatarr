"""Read a member's Wrap-Up texts from a database copy, saving none of them.

For the owner, before a merge that touches the wrap-up's facts or texts
(UX design record, stage 3.2 / 3.7): what the narrative facts found, the
cards the writer is offered, the fallback text each card falls back to
(the templates), and - with --write, on a machine with the curator - the
story the editor and writer make of them. Nothing of the Wrap-Up is
stored: the facts are computed and printed, the story is printed.

The one write: the copy is first brought to this checkout's schema - the
same migration Curatarr runs when it starts (``init_db``: new tables and
columns, retired ones dropped) - so a copy taken before a merge that adds a
column (``cached_recommendations.lifted_at``) can be read. Run it on a
COPY, never on the live database.

    DATABASE_URL=sqlite:///path/to/copy.db python scripts/wrapup_read.py \\
        --user Member --period month:2026-10 [--scope all] [--lang de] [--write] [--out read.md]

``--user`` is a user id or a Plex/Jellyfin user name. ``--period`` any
period the Wrap-Up knows: week:2026-W45, month:2026-10, 30d, 6m, year:2026,
season:2026-fall, all.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _user_id(arg: str) -> int:
    from src.database.connection import get_db_session
    from src.database.models import User
    with get_db_session() as db:
        q = db.query(User.id, User.is_admin)
        row = (q.filter(User.id == int(arg)).first() if arg.isdigit()
               else q.filter(User.plex_username == arg).first())
    if not row:
        raise SystemExit(f"no user {arg!r} in this database")
    return row


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--user", required=True)
    ap.add_argument("--period", required=True)
    ap.add_argument("--scope", default="all")
    ap.add_argument("--lang", default=None, help="the template language (default: the member's)")
    ap.add_argument("--write", action="store_true", help="also run the editor and the writer (needs the curator)")
    ap.add_argument("--out", type=Path, default=None)
    a = ap.parse_args()

    from src.database.connection import init_db
    init_db()                       # the copy at this checkout's schema - the only write

    from src.services.user_language import user_language
    from src.services.wrapup_facts import build_wrapup_facts
    from src.services.wrapup_insights import mine
    from src.services.wrapup_story import render_fact_sheet
    from src.services.wrapup_writer import _template, write

    uid, is_admin = _user_id(a.user)
    facts = build_wrapup_facts(uid, a.scope, a.period, is_admin=bool(is_admin))
    lang = a.lang or user_language(uid)
    lines = [f"# Wrap-Up read: user {uid}, {a.scope}, {a.period} ({lang})", ""]
    if facts.get("empty") or not facts.get("eligible"):
        lines.append("Not enough played in this period (the page says \"Not enough yet\").")
    narrative = {k: facts[k] for k in ("ritual", "comfort_genre", "of_the_month", "night_shift") if k in facts}
    lines += ["## Narrative facts", "", "```json", json.dumps(narrative, indent=1, ensure_ascii=False) or "{}",
              "```", ""]
    cards = mine(facts)
    lines += ["## Cards the writer is offered", ""]
    for c in cards:
        head, body = _template(c["id"], c, None, lang)
        lines += [f"- **{c['id']}** [{c['slide']}, {c['score']}] {c['claim']}",
                  f"  - why: {c['why']}",
                  f"  - fallback: **{head}** - {body}"]
    if a.write and cards:
        sheet, idx = render_fact_sheet(facts)
        story, source, model = asyncio.run(write(facts, sheet, idx, cards, lang=lang))
        lines += ["", f"## Story ({source}, {model})", ""]
        for s in (story or {}).get("slides") or []:
            lines += [f"### {s['headline']}  _({s['id']}, {s['source']})_", s["text"], ""]
        if story:
            lines += [f"**Archetype:** {story['archetype_title']} - {story['archetype_subhead']}",
                      f"**Share line:** {story['share_line']}"]
    text = "\n".join(lines) + "\n"
    if a.out:
        a.out.write_text(text, encoding="utf-8")
        print("wrote", a.out)
    else:
        print(text)


if __name__ == "__main__":
    main()
