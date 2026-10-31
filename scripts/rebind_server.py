"""Move this install from Plex to Jellyfin, or back, keeping everything.

The web page does the same with Curatarr running: Settings -> Media server
(src/services/media_server/switch.py). This script is the headless way.

    python scripts/rebind_server.py --to jellyfin                    # dry run (default)
    python scripts/rebind_server.py --to jellyfin --write
    python scripts/rebind_server.py --to plex --map-user anna=Anna_K --write
    python scripts/rebind_server.py --undo data/backups/rebind-20261026-2130/manifest.json

Watch history, enrichment, vectors, verdicts, recommendations and open
deletion proposals are re-keyed from the old server's ids to the new one's
(docs/JELLYFIN_DESIGN.md §8). Both servers must be configured in .env; the
old one may be down, then the id memory (server_id_map) alone matches.

The dry run reads both sides and prints what would happen: matches per
method, what stays unmatched, conflicts, title suggestions, users and
libraries. Nothing is written. ``--write`` backs the database up first and
leaves a manifest next to the backup; ``--undo`` replays it backwards.

Stop Curatarr first: --write and --undo refuse while something answers on
its port (like update_db.py). The re-key is one database transaction, and a
sync writing at the same time would wait for it or fail.

Options:
  --map-user OLD=NEW      a Curatarr user's name on the old server = their
                          name on the new one (repeatable)
  --skip-user NAME        leave this user on the old server's id on purpose
  --accept-suggestions    also re-key titles matched by title + year only
  --sync-state            afterwards, mark finished plays as played on
                          Jellyfin and turn Plex ratings >= 8/10 into likes
  --json FILE             write the report as JSON too
"""
import argparse
import asyncio
import json
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.services.media_server import active_kind              # noqa: E402
from src.services.media_server import rebind                   # noqa: E402


def _pairs(values) -> dict:
    out = {}
    for v in values or []:
        old, sep, new = v.partition("=")
        if not sep or not old.strip() or not new.strip():
            raise SystemExit(f"--map-user wants OLD=NEW, got {v!r}")
        out[rebind.normalize_flag(old)] = rebind.normalize_flag(new)
    return out


def _print_report(rep: dict) -> None:
    it = rep["items"]
    print(f"\nRebind {rep['from']} -> {rep['to']}"
          + ("" if rep["source_walked"] else f"  ({rep['from']} not reachable: id memory only)"))
    print(f"  titles matched: {it['matched']}  {it['by_method']}")
    print(f"  unmatched: {it['unmatched']}   conflicts: {it['conflicts']}   "
          f"title suggestions: {it['suggestions']}")
    print("  per table (rows matched / unmatched):")
    for name, t in sorted(rep["tables"].items()):
        if t["matched"] or t["unmatched"]:
            print(f"    {name:42} {t['matched']:>7} / {t['unmatched']}")
    print("  users:")
    for uid, u in rep["users"].items():
        print(f"    #{uid} {u.get('name') or ''}: {u['from']} -> {u['to']} ({u['how']})")
    for name in rep["users_unmatched"]:
        print(f"    NO MATCH: {name}")
    for name in rep["users_skipped"]:
        print(f"    skipped on purpose: {name}")
    print("  libraries:", ", ".join(rep["libraries"].values()) or "-",
          ("   unmatched: " + ", ".join(rep["libraries_unmatched"])) if rep["libraries_unmatched"] else "")
    for u in rep["unmatched_sample"]:
        print(f"    unmatched: {u['key']}  {u['title'] or '(no title on record)'}")
    for c in rep["conflicts"]:
        print(f"    conflict: {', '.join(c['sources'])} all claim {c['target']}")
    for s in rep["suggestions"]:
        print(f"    suggestion: {s['key']} {s['title']!r} -> {s['target']} (--accept-suggestions)")
    for b in rep["blocking"]:
        print(f"  BLOCKING: {b} (use --map-user or --skip-user)")


def _server_running() -> bool:
    """True when something answers on Curatarr's port on this machine."""
    from src.config import settings
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", int(settings.PORT))) == 0


async def main(args) -> int:
    if (args.write or args.undo) and _server_running():
        print("Curatarr is running - stop it first (the re-key is one database transaction).")
        return 2
    if args.undo:
        res = await rebind.undo(Path(args.undo))
        print(json.dumps(res, indent=1, default=str))
        return 0
    source = active_kind()
    target = args.to
    if source == target:
        print(f"This install already runs on {target}.")
        return 1
    src_adapter, tgt_adapter = rebind.server_adapters(source, target)
    plan = await rebind.build_plan(source, target, tgt_adapter, source_adapter=src_adapter,
                                   map_user=_pairs(args.map_user), skip_users=args.skip_user or (),
                                   accept_suggestions=args.accept_suggestions)
    rep = plan.report()
    _print_report(rep)
    if args.json:
        Path(args.json).write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    if not args.write:
        print("\nDry run - nothing changed. Add --write to re-key.")
        return 0
    if plan.blocking:
        print("\nNot written: resolve the blocking items above first.")
        return 2
    mpath = await rebind.apply_plan(plan, target_adapter=tgt_adapter, sync_state=args.sync_state)
    m = json.loads(mpath.read_text(encoding="utf-8"))
    stuck = sum(t.get("collisions", 0) for t in m["tables"].values())
    print(f"\nDone. Curatarr now runs on {target} - restart it."
          f"\n  manifest: {mpath}\n  vectors copied: {len(m['chroma'])}, facets moved: {m['facets']}, "
          f"proposals re-keyed: {len(m['proposals'])}, rows left on an old key (collisions): {stuck}"
          f"\n  undo: python scripts/rebind_server.py --undo {mpath}")
    if m.get("sync_state"):
        print(f"  synced to Jellyfin: {m['sync_state']}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--to", choices=("plex", "jellyfin"))
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--undo", metavar="MANIFEST")
    ap.add_argument("--map-user", action="append", metavar="OLD=NEW")
    ap.add_argument("--skip-user", action="append", metavar="NAME")
    ap.add_argument("--accept-suggestions", action="store_true")
    ap.add_argument("--sync-state", action="store_true")
    ap.add_argument("--json", metavar="FILE")
    a = ap.parse_args()
    if not a.undo and not a.to:
        ap.error("--to plex|jellyfin (or --undo MANIFEST)")
    try:
        sys.exit(asyncio.run(main(a)))
    except RuntimeError as e:             # a refusal (not configured, blocking, unfinished run)
        print(f"Not done: {e}")
        sys.exit(2)
