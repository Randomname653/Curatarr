"""PROTOTYPE — principle condense (V2): the curator reviews ALL active
principles of one category group in a single call and proposes merge GROUPS.

V1 (embedding-threshold clustering) failed calibration: one-sentence rules sit
too close in nomic space — the top cosine pair (0.760) was semantically
DIFFERENT while the clearest real duplicate (#3 vs #17, "technical quality
alone is insufficient") measured only 0.690, below several unrelated pairs.
The merge decision must be the LLM's; embeddings can't rank it here.

READ-ONLY: prints proposals, writes nothing. Run from repo root:

    python tests/condense_proto.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

_CONDENSE_SYS = """You maintain a media curator's RULE-SET: the owner's established curation principles. Some principles have accumulated near-duplicates. Identify groups of principles that state the SAME underlying requirement, and write ONE consolidated principle per group.

RULES:
- Be CONSERVATIVE. Merge ONLY principles whose meaning is the same requirement or the same requirement plus a sharpening of it. Most principles stand alone; an EMPTY merges list is the expected common outcome.
- Never merge a KEEP-condition rule with a DELETE-condition rule, even on the same topic.
- The consolidated principle must preserve EVERY nuance that could change a future verdict — one sentence, title-agnostic, ENGLISH, no examples. If the nuances cannot fit one sentence, do not merge.
- Use the exact IDs shown. A group needs at least 2 ids."""

_CONDENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "merges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    "merged_principle": {"type": "string"},
                },
                "required": ["ids", "merged_principle"],
            },
        },
    },
    "required": ["merges"],
}


async def main():
    from src.database.connection import get_db_session
    from src.database.models import CuratorPrinciple
    from src.services.curator_principles import _curator_json

    with get_db_session() as db:
        rows = [{"id": p.id, "text": p.text, "category": p.category,
                 "basis": p.basis}
                for p in db.query(CuratorPrinciple).filter(
                    CuratorPrinciple.user_id == 1,
                    CuratorPrinciple.status == "active",
                ).order_by(CuratorPrinciple.id)]

    groups = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)
    print(f"{len(rows)} active principles in {len(groups)} category group(s)\n")

    for cat, members in groups.items():
        print(f"===== category group {cat!r} ({len(members)} principles) =====")
        listing = "\n".join(f"ID {m['id']}: {m['text']}" for m in members)
        print(listing)
        if len(members) < 2:
            print("--> (single principle, nothing to condense)\n")
            continue
        res = await _curator_json(
            _CONDENSE_SYS,
            f"ACTIVE PRINCIPLES:\n{listing}\n\nPropose merge groups (empty list if none).",
            _CONDENSE_SCHEMA, num_predict=600)
        merges = res.get("merges") or []
        valid_ids = {m["id"] for m in members}
        print(f"\n--> {len(merges)} merge proposal(s):")
        for mg in merges:
            ids = [i for i in (mg.get("ids") or []) if i in valid_ids]
            if len(ids) < 2:
                print(f"    (skipped invalid group {mg.get('ids')})")
                continue
            print(f"    ids {sorted(ids)}:")
            for i in sorted(ids):
                src = next(m for m in members if m["id"] == i)
                print(f"      - #{i} [{src['basis']}]: {src['text'][:110]}")
            print(f"      => {mg.get('merged_principle')}")
        print()


asyncio.run(main())
