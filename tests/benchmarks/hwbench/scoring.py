"""Scoring sheets and their aggregation - the three rubrics the matrix and the
eval plan rank by, written down so a re-score is reproducible.

    run score-sheets --refs a,b --kind mono|chat|judge [--profile large]
    run record-scores --csv dir/chunk_*.csv [--scorer X] [--replace]

Sheets are BLIND: model labels are shuffled per sheet set and the run ids
live only in legend.json next to the chunks; the CSVs carry the label, and
record-scores resolves it through that legend. The judge rubric's files of
2026-10-20 were lost with a session scratchpad; this module is now its home.

The ledger keeps one score per (run, scorer): a scorer's new aggregate
REPLACES its old one. Writing chat scores under "claude" would have wiped
that run's card-note score (2026-11-02). So every kind has its own default
scorer, and record-scores refuses to replace a score of another rubric
unless --replace says so.
"""
from __future__ import annotations

import csv
import json
import random
from collections import defaultdict
from pathlib import Path

RUBRICS = {
    "mono": {
        "rubric": "5-axis-2026-08", "scorer": "claude", "artifact": "monologue.jsonl",
        "axes": ("faith", "rel", "spec", "tone", "format"), "extra": (), "chunk": 12,
        "help": {
            "faith": "facts match the evidence; a flagged data collision scores high, a confident wrong referent <=2",
            "rel": "tied to the owner's taste; acknowledging attachment counts",
            "spec": "concrete details (names, counts, premise facts) instead of a template",
            "tone": "curator voice; buzzwords, condescension, imperative spam cost points",
            "format": "two disciplined sentences; run-ons, lists, persona breaks cost points; empty = 0",
        },
    },
    "chat": {
        "rubric": "chat-2026-05", "scorer": "claude-chat", "artifact": "chat.jsonl",
        "axes": ("faith", "rel", "spec", "tone", "format"), "extra": ("consist", "pushback"), "chunk": 3,
        "help": {
            "faith": "0 = invents plots/titles/facts or user data, 5 = source-true; refusing a fake title is faith",
            "rel": "0 = off topic, 5 = answers the question",
            "spec": "0 = generic, 5 = concrete titles and reasons",
            "tone": "0 = robotic or arrogant, 5 = engaging; mockery of the owner costs",
            "format": "0 = ignores the system prompt (language, length), 5 = compliant",
            "consist": "multi-turn only: 0 = position swings between turns, 5 = argued consistently",
            "pushback": "multi-turn only: 0 = caves to bare pressure (or never concedes), 5 = holds AND concedes to new information",
        },
    },
    "judge": {
        "rubric": "judge-argument-2026-11r", "scorer": "claude-judge", "artifact": "judge.jsonl",
        "axes": ("faith", "law", "logic", "judgment", "clarity"), "extra": ("rule_hit",), "chunk": 10,
        "help": {
            "faith": "no invented facts or inner contradictions; claims stay within what the facts could say",
            "law": "the constitution applied: pillar priority III>II>I>0; Household = another user's real "
                   "engagement; Custodian = documented stature (popularity or competence is not); Resonance = "
                   "the three-part litmus; Ego = execution, not premise; protecting_pillar = the highest that "
                   "protects; CUT only when nothing protects; an OWNER SIGNAL only lowers a CUT to STAGNANT; "
                   "bitrate never deletes",
            "logic": "the verdict follows from its own findings (naming major awards and then cutting does not)",
            "judgment": "the weighing is sensible and fair to the title",
            "clarity": "one or two specific sentences per pillar; 'N/A', 'None' or vague lines cost points",
            "rule_hit": "1 when a constitution rule is broken (protection named but verdict against it, empty or "
                        "failed output, bitrate as a reason to delete, a signal forcing a keep), else 0",
        },
    },
}


def detect_kind(columns) -> str:
    cols = set(columns)
    if "law" in cols:
        return "judge"
    if "consist" in cols:
        return "chat"
    return "mono"


def _num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def aggregate(kind: str, rows: list[dict]) -> dict:
    """One run's aggregate from its scored rows (rows with any axis empty skip)."""
    r = RUBRICS[kind]
    rows = [x for x in rows if all(_num(x.get(a)) is not None for a in r["axes"])]
    n = len(rows)
    if not n:
        return {"n": 0}
    tot = [sum(_num(x[a]) for a in r["axes"]) for x in rows]
    agg = {"n": n, "mean25": round(sum(tot) / n, 2),
           **{a: round(sum(_num(x[a]) for x in rows) / n, 2) for a in r["axes"]}}
    if kind == "mono":
        agg.update(ge22=sum(t >= 22 for t in tot), le19=sum(t <= 19 for t in tot))
    elif kind == "judge":
        bycat = defaultdict(list)
        for x, t in zip(rows, tot):
            if x.get("category"):
                bycat[x["category"]].append(t)
        agg.update(ge22=sum(t >= 22 for t in tot), le15=sum(t <= 15 for t in tot),
                   rule_hits=sum(int(_num(x.get("rule_hit")) or 0) for x in rows),
                   by_category={c: round(sum(v) / len(v), 1) for c, v in sorted(bycat.items())})
    else:
        mt = [x for x in rows if _num(x.get("consist")) is not None and _num(x.get("pushback")) is not None]
        st = [t for x, t in zip(rows, tot) if x not in mt]
        agg.update(faith_le2=sum(_num(x["faith"]) <= 2 for x in rows),
                   st_n=len(st), st_mean25=round(sum(st) / len(st), 2) if st else None,
                   mt_n=len(mt))
        if mt:
            agg.update(mt_mean35=round(sum(sum(_num(x[a]) for a in r["axes"] + r["extra"]) for x in mt)
                                       / len(mt), 2),
                       consist=round(sum(_num(x["consist"]) for x in mt) / len(mt), 2),
                       pushback=round(sum(_num(x["pushback"]) for x in mt) / len(mt), 2))
    return agg


def conflict(scores: list[dict], run_id: str, scorer: str, rubric: str) -> dict | None:
    """The existing score this write would replace when it is of another
    rubric - card notes overwritten by chat scores, a judge score of the old
    rubric by the new one. None when nothing or the same rubric is there."""
    old = next((s for s in scores if s["run_id"] == run_id and s["scorer"] == scorer), None)
    return old if old and old.get("rubric") != rubric else None


def read_scored(csv_paths) -> tuple[str, dict[str, list[dict]]]:
    """(kind, {run_id: rows}) from filled chunk CSVs. Labels resolve through
    the legend.json beside each CSV; a CSV with its own run_id column (the
    sheets before 2026-11-14) is read as it is."""
    kind, per_run = None, defaultdict(list)
    for p in map(Path, csv_paths):
        legend_path = p.parent / "legend.json"
        legend = json.loads(legend_path.read_text(encoding="utf-8")) if legend_path.exists() else {}
        with p.open(encoding="utf-8") as f:
            reader = csv.DictReader(f)
            k = detect_kind(reader.fieldnames or [])
            if kind and k != kind:
                raise SystemExit(f"{p.name} is a {k} sheet, the others are {kind}")
            kind = k
            for row in reader:
                rid = row.get("run_id") or (legend.get(row.get("model", "")) or {}).get("run_id")
                if not rid:
                    raise SystemExit(f"{p.name}: no run for label {row.get('model')!r} (legend.json missing?)")
                per_run[rid].append(row)
    return kind or "mono", per_run


def write_sheets(kind: str, runs: list[tuple[str, dict, str]], out: Path) -> int:
    """Blind sheets for ``runs`` [(ref, run, artifact path)] into ``out``:
    chunk_NN.md + chunk_NN.csv per chunk, legend.json with the label map.
    Returns the number of items."""
    r = RUBRICS[kind]
    labels = [f"M{i + 1}" for i in range(len(runs))]
    random.SystemRandom().shuffle(labels)
    legend = {lab: {"ref": ref, "run_id": run["id"], "profile": run["settings"].get("profile")}
              for lab, (ref, run, _) in zip(labels, runs)}
    rows: dict = {}
    for lab, (_, _, art) in zip(labels, runs):
        for line in Path(art).read_text(encoding="utf-8").splitlines():
            d = json.loads(line)
            rows.setdefault(d.get("key") or d.get("id"), {})[lab] = d
    out.mkdir(parents=True, exist_ok=True)
    (out / "legend.json").write_text(json.dumps(legend, indent=1), encoding="utf-8")
    head = [f"# {kind} sheet - rubric {r['rubric']} (0-5 per axis)", ""]
    head += [f"- **{a}**: {r['help'][a]}" for a in r["axes"] + r["extra"]] + [""]
    keys = sorted(rows)
    cols = ["key", "model"] + (["category"] if kind == "judge" else []) + list(r["axes"] + r["extra"]) + ["note"]
    for n in range(0, len(keys), r["chunk"]):
        md, cs = list(head), [cols]
        for k in keys[n:n + r["chunk"]]:
            first = next(iter(rows[k].values()))
            md.append(f"## {k} | {first.get('category', '')}\n")
            for lab in sorted(rows[k], key=lambda x: int(x[1:])):
                md.append(_render(kind, lab, rows[k][lab]))
                cs.append([k, lab] + ([first.get("category", "")] if kind == "judge" else [])
                          + [""] * (len(r["axes"]) + len(r["extra"]) + 1))
        (out / f"chunk_{n // r['chunk'] + 1:02}.md").write_text("\n".join(md), encoding="utf-8")
        with (out / f"chunk_{n // r['chunk'] + 1:02}.csv").open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(cs)
    return len(keys)


def _render(kind: str, lab: str, d: dict) -> str:
    if kind == "judge":
        raw = d.get("raw") or {}
        lines = [f"### {lab}  verdict={d.get('verdict')}  pillar={d.get('pillar')}  "
                 f"repaired={d.get('repaired')}  error={d.get('error_kind')}"]
        lines += [f"- {f}: {raw.get(f, '')}" for f in
                  ("pillar_3_household", "pillar_2_custodian", "pillar_1_resonance", "pillar_0_ego",
                   "bitrate_note")]
        return "\n".join(lines) + "\n"
    if d.get("turns"):
        body = "\n".join(f"> {t['user']}\n\n{t['assistant']}\n" for t in d["turns"])
    else:
        body = d.get("text") or d.get("response") or ""
        if d.get("prompt"):
            body = f"> {d['prompt']}\n\n{body}"
    return f"### {lab} ({d.get('verdict', '')})\n\n{body}\n"
