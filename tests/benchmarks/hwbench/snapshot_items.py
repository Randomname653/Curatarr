"""Freeze the bench items: real library titles + the EXACT evidence the
production judge would get for them today.

Why freeze: build_evidence reads the live library, the enrichment cache,
watch history, learned considerations and principles. All of that moves
every day, so two models benched a week apart would answer different
questions. The snapshot pins the facts once; every model then judges the
same bytes, and the ledger records the snapshot id.

Why production code, not copies: the 2026-08 pipeline bench carried a copy
of the legacy pitch template and of the persona; by 2026-10 both had
drifted (persona lines reworded in PR #150, pitch rules 8 and 10 rewritten,
data-poor and size blocks added) — and with PILLARS_ENABLED the deletion
run does not use that pitch at all any more but pillars.build_evidence ->
adjudicate -> write_monologue. This script calls build_evidence itself.

Item pool = the 148 titles of the 2026-08 pipeline run (continuity with
model_baselines.csv). Each is re-checked against today's standards and
dropped with a reason when it fails:
  * still in the library (_fetch_arr_candidates finds it),
  * evidence not thin / not mismatched / significance checked,
  * watch data readable (owner + household).
The 40-item screening subset takes 10 per category: the 2026-08 owner
spot-check titles that still pass first, then a seeded round-robin over
the evidence flags (household claim, acclaim, bitrate outlier, owner
watched, owner signal) so every pillar gets exercised.

PRIVACY: a snapshot holds the owner's taste profile and watch data. It is
written to ``tests/benchmarks/hwbench_data/`` and must never be committed.

Run with the app STOPPED (the vector store is single-process), once per
profile — the evidence budget depends on LLM_PROFILE / CURATOR_NUM_CTX,
which are read at import time, hence the subprocess per profile:

    venv/Scripts/python.exe -m tests.benchmarks.hwbench.snapshot_items
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATA = HERE.parent / "hwbench_data"
DEFAULT_SOURCE = HERE.parent / "curator_pipeline_2026-08-15_13-44.jsonl"
SPOTCHECK = HERE.parent / "curator_ab_2026-08_spotcheck.md"
CATEGORIES = ["movie", "show", "anime", "music"]
PER_CAT_SUB = 10
SEED = 20261015

REJECT_FLAGS = {
    "evidence_thin": "evidence too thin to judge",
    "evidence_mismatched": "enrichment profile describes another work",
    "significance_unchecked": "Wikipedia significance never checked",
    "owner_watch_unknown": "owner watch data unreadable",
    "household_unknown": "household watch data unreadable",
}
STRATA = ("household_claim", "acclaim_present", "bitrate_outlier",
          "owner_watched", "owner_signal")


def _norm(t: str) -> str:
    return " ".join((t or "").lower().split())


def source_titles(path: Path) -> list[tuple[str, str]]:
    seen, out = set(), []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        key = (d.get("category"), d.get("title"))
        if all(key) and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def spotcheck_titles(path: Path) -> set[tuple[str, str]]:
    out = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith("## [") and "] " in line:
                cat, title = line[4:].split("] ", 1)
                out.add((cat, title.strip()))
    return out


def _git_sha() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"],
                                       cwd=REPO, text=True).strip()
    except (OSError, subprocess.SubprocessError):
        return ""


async def _snapshot(profile: str, pool: list[tuple[str, str]], user_id: int) -> dict:
    sys.path.insert(0, str(REPO))
    os.chdir(REPO)
    from src.config import settings
    from src.database.connection import get_db_session
    from src.routers.recommendations import _fetch_arr_candidates
    from src.services import pillars
    from src.services.llm_utils import (CURATOR_NUM_CTX, detect_user_language,
                                        language_directive, llm_profile)
    assert llm_profile() == profile, f"LLM_PROFILE env {profile} not applied"

    with get_db_session() as db:
        lang = language_directive(detect_user_language(user_id, db))
    library = defaultdict(dict)
    for it in await _fetch_arr_candidates():
        library[it.get("category")].setdefault(_norm(it.get("title")), it)

    items, rejected = [], []
    for cat, title in pool:
        item = library[cat].get(_norm(title))
        if not item:
            rejected.append({"category": cat, "title": title,
                             "reason": "no longer in the library"})
            continue
        try:
            ev = await pillars.build_evidence(item, user_id, cat)
        except Exception as e:   # one broken title must not kill the snapshot
            rejected.append({"category": cat, "title": title,
                             "reason": f"build_evidence crashed: {type(e).__name__}: {e}"})
            continue
        flags = ev.get("flags") or {}
        bad = [msg for f, msg in REJECT_FLAGS.items() if flags.get(f)]
        if bad:
            rejected.append({"category": cat, "title": title, "reason": "; ".join(bad)})
            continue
        items.append({
            "key": f"{cat}:{title}",
            "category": cat,
            "title": title,
            "facts": ev["facts"],
            "law_extra": ev.get("law_extra", ""),
            "flags": flags,
            "item": {k: item.get(k) for k in (
                "title", "year", "genres", "media_type", "tmdb_id", "tvdb_id",
                "anilist_id", "plex_rating_key", "size_mb")},
        })
    return {
        "profile": profile,
        "num_ctx": CURATOR_NUM_CTX,
        "lang_directive": lang,
        "pillars_enabled": bool(getattr(settings, "PILLARS_ENABLED", False)),
        "items": items,
        "rejected": rejected,
    }


def select_subset(items: list[dict], spot: set[tuple[str, str]],
                  per_cat: int = PER_CAT_SUB, seed: int = SEED) -> list[str]:
    """10 keys per category: spot-check titles first, then round-robin over
    the evidence-flag strata (seeded, so the subset is reproducible)."""
    rng = random.Random(seed)
    chosen = []
    for cat in CATEGORIES:
        pool = [i for i in items if i["category"] == cat]
        pick = [i for i in pool if (cat, i["title"]) in spot][:per_cat]
        rest = [i for i in pool if i not in pick]
        rng.shuffle(rest)
        buckets = defaultdict(list)
        for i in rest:
            hit = next((s for s in STRATA if i["flags"].get(s)), "none")
            buckets[hit].append(i)
        order = [*STRATA, "none"]
        while len(pick) < per_cat and any(buckets.values()):
            for s in order:
                if buckets[s] and len(pick) < per_cat:
                    pick.append(buckets[s].pop())
        chosen += [i["key"] for i in pick]
    return chosen


def write_report(snap: dict, subset: list[str], path: Path) -> None:
    by_cat = Counter(i["category"] for i in snap["items"])
    sub = [i for i in snap["items"] if i["key"] in set(subset)]
    lines = [f"# Bench items — snapshot {snap['id']}",
             "",
             f"profile {snap['profile']}, num_ctx {snap['num_ctx']}, code {snap['git_sha']}, "
             f"source {snap['source']}",
             "",
             f"**{len(snap['items'])} of {len(snap['items']) + len(snap['rejected'])} pass** "
             f"today's standards ({', '.join(f'{c} {by_cat[c]}' for c in CATEGORIES)}); "
             f"{len(subset)} in the screening subset.",
             "",
             "## Subset flag coverage",
             ""]
    for s in STRATA:
        lines.append(f"- {s}: {sum(1 for i in sub if i['flags'].get(s))}")
    lines += ["", "## Rejected", "", "| category | title | reason |", "|---|---|---|"]
    lines += [f"| {r['category']} | {r['title']} | {r['reason']} |" for r in snap["rejected"]]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    ap.add_argument("--user-id", type=int, default=1)
    ap.add_argument("--profiles", default="large,small")
    ap.add_argument("--_child", default="", help=argparse.SUPPRESS)
    ap.add_argument("--_pool", default="", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args._child:
        pool = [tuple(x) for x in json.loads(Path(args._pool).read_text(encoding="utf-8"))]
        snap = asyncio.run(_snapshot(args._child, pool, args.user_id))
        print(json.dumps(snap, ensure_ascii=False))
        return

    DATA.mkdir(parents=True, exist_ok=True)
    pool = source_titles(Path(args.source))
    pool_file = DATA / ".pool.json"
    pool_file.write_text(json.dumps(pool), encoding="utf-8")
    stamp = datetime.now().strftime("%Y-%m-%d")
    spot = spotcheck_titles(SPOTCHECK)
    subset = None
    for profile in args.profiles.split(","):
        env = {**os.environ, "LLM_PROFILE": profile, "CURATOR_NUM_CTX": "0",
               "PYTHONIOENCODING": "utf-8"}
        out = subprocess.run([sys.executable, "-m", "tests.benchmarks.hwbench.snapshot_items",
                              "--_child", profile, "--_pool", str(pool_file),
                              "--user-id", str(args.user_id)],
                             cwd=REPO, env=env, capture_output=True, text=True,
                             encoding="utf-8")
        if out.returncode != 0:
            raise SystemExit(f"snapshot {profile} failed:\n{out.stderr[-3000:]}")
        snap = json.loads(out.stdout.strip().splitlines()[-1])
        snap.update(id=f"{stamp}-{profile}", git_sha=_git_sha(),
                    source=Path(args.source).name, created=datetime.now().isoformat())
        # One selection for every profile: chosen on the first, kept after.
        if subset is None:
            subset = select_subset(snap["items"], spot)
        snap["subset_40"] = [k for k in subset if any(i["key"] == k for i in snap["items"])]
        (DATA / f"evidence_{snap['id']}.json").write_text(
            json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        write_report(snap, snap["subset_40"], DATA / f"evidence_{snap['id']}.md")
        print(f"{profile}: {len(snap['items'])} items pass, {len(snap['rejected'])} rejected, "
              f"subset {len(snap['subset_40'])} -> {DATA / ('evidence_' + snap['id'] + '.json')}")
    pool_file.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
