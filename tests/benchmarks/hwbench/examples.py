"""Argumentation examples: how each curator the setup recommends judges five
titles for a FICTIONAL owner, Jane Doe.

    python -m tests.benchmarks.hwbench.run examples            # GPU: judge + render
    python -m tests.benchmarks.hwbench.run examples --render   # re-render from the last run

Each model runs through the production judge and card note (the quality
child: pillars.adjudicate + write_monologue, with the persona bake), with
the profile and context its matrix package uses. Nothing here belongs to a
real user: Jane Doe, her taste, her household member Sam, her signal and
four of the five titles are invented; the fifth (Night of the Living Dead)
carries public metadata only - so the outputs may be committed.

Outputs:
  hwbench_data/examples/run.json   every verdict, pillar finding and note
  docs/CURATOR_EXAMPLES.md          the full comparison with the review notes
                                    from examples_review.json (written by a
                                    person after reading the run, not by a model)
  src/services/curator_examples.json  the one wizard example (Flatmates) per model
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from . import models, quality, server

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
DATA = HERE.parent / "hwbench_data" / "examples"
REVIEW = HERE / "examples_review.json"
DOCS = REPO / "docs" / "CURATOR_EXAMPLES.md"
WIZARD = REPO / "src" / "services" / "curator_examples.json"
WIZARD_KEY = "show:Flatmates"

TASTE = ("OWNER TASTE: Jane Doe likes slow-burn horror and sharp social satire, values economical, "
         "well-crafted filmmaking over spectacle, and has little patience for formulaic sequels or "
         "long-running sitcoms.")
_VERIFIED = ("[VERIFIED DATA — reason ONLY from the facts below; do NOT add plot points, people, "
             "awards, year, or franchise context not listed here]")
_NONE = "  none have watched it."


def _facts(head, owner, household, meta, tech, signals=""):
    return (f"{head}\nOWNER: {owner}\nOTHER HOUSEHOLD USERS:\n{household}\nACCLAIM & METADATA:\n"
            f"{_VERIFIED}\n<<<UNTRUSTED_SOURCE:metadata>>>\n{meta}\n<<<END_UNTRUSTED_SOURCE>>>\n"
            f"{TASTE}\n{signals}TECH: {tech}")


# (key, category, title, what decides it, the setup in one line, what the law asks, facts)
CASES = [
    ("movie:Night of the Living Dead", "movie", "Night of the Living Dead (1968)", "Custodian",
     "A landmark of the genre, in the US National Film Registry. Jane has not seen it; the file is normal.",
     "Keep: it is a landmark (Custodian).",
     _facts("TITLE: Night of the Living Dead (1968) — movie, Horror", "not watched by the owner.", _NONE,
            "  Type: Film\n  Title: Night of the Living Dead\n  Year: 1968\n  Genres: Horror\n"
            "  Director: George A. Romero\n  Cast: Duane Jones, Judith O'Dea\n"
            "  Themes: siege, social breakdown, survival, racial tension\n  Mood: tense, bleak\n"
            "  Runtime: 96 min\n"
            "  Significance: Selected for the US National Film Registry; widely credited with founding the "
            "modern zombie film. The film is in the public domain.\n"
            "  Plot: Strangers take shelter in a rural farmhouse while the recently dead rise and lay siege to it.",
            "1080 h264, 6.9 GB, 72 MB/min, 1.31x class median (normal)")),
    ("movie:Harbor Light", "movie", "Harbor Light (2019, invented)", "Resonance",
     "A quiet, almost wordless drama: RT 88 %, one regional cinematography prize. Not Jane's genres.",
     "Gray zone: the Resonance litmus (intent, awe, rigor) decides.",
     _facts("TITLE: Harbor Light (2019) — movie, Drama", "not watched by the owner.", _NONE,
            "  Type: Film\n  Title: Harbor Light\n  Year: 2019\n  Genres: Drama\n"
            "  Themes: grief, a fishing town in winter, a lighthouse keeper's last season, silence\n"
            "  Mood: contemplative, melancholic\n  Runtime: 118 min\n  Rating: 7.4/10 (2,100 votes)\n"
            "  Critics: Rotten Tomatoes 88%\n"
            "  Notable: won Best Cinematography at a regional film festival\n"
            "  Community reception: Praised for long, patient takes of the coast and an almost wordless lead "
            "performance; some viewers call it slow.\n"
            "  Plot: An ageing keeper spends the final winter of his lighthouse before it is automated.",
            "1080 h264, 9.8 GB, 83 MB/min, 1.12x class median (normal)")),
    ("movie:The Example Sequel", "movie", "The Example Sequel (2014, invented)", "Cut",
     "A weak sequel, 4.9/10, with a bloated file (2.4x the median).",
     "A clear CUT. The big file is a side note, never a reason to delete.",
     _facts("TITLE: The Example Sequel (2014) — movie, Comedy", "not watched by the owner.", _NONE,
            "  Type: Film\n  Title: The Example Sequel\n  Year: 2014\n  Genres: Comedy\n"
            "  Themes: road trip, reunion, slapstick\n  Mood: light-hearted\n  Runtime: 101 min\n"
            "  Rating: 4.9/10\n"
            "  Community reception: Reviewers call it a by-the-numbers sequel that repeats the first film's jokes.",
            "1080 h264, 14.2 GB, 140 MB/min, 2.40x class median (bloated)")),
    ("show:Flatmates", "show", "Flatmates (2009, invented)", "Household",
     "A sitcom Jane does not like. Sam, who lives with her, has watched 34 of 36 episodes. Normal file (0.94x).",
     "Keep, because Sam watches it (Household) - whatever Jane's taste says.",
     _facts("TITLE: Flatmates (2009) — show, Comedy",
            "not watched by the owner. Availability: 36 episodes known, 36 aired, 36 on disk.",
            "  - Sam: 34 episode(s), last Aug 2026, completed",
            "  Type: TV series\n  Title: Flatmates\n  Year: 2009\n  Genres: Comedy\n"
            "  Themes: shared flat, friendship, office jobs, running gags\n  Mood: light-hearted\n"
            "  Format: 3 season(s), 36 episodes x 22 min\n  Rating: 6.8/10\n"
            "  Community reception: A comfortable, formulaic sitcom; fans like the cast chemistry.",
            "1080 h264, 21.0 GB, 26 MB/min, 0.94x class median (normal)")),
    ("movie:Midnight Static", "movie", "Midnight Static (1994, invented)", "Owner signal",
     "Cheap 90s horror, 5.6/10. Jane has said she wants to keep the 90s horror she grew up with.",
     "The signal lifts a CUT to STAGNANT (Jane decides), but never forces a keep on its own.",
     _facts("TITLE: Midnight Static (1994) — movie, Horror", "not watched by the owner.", _NONE,
            "  Type: Film\n  Title: Midnight Static\n  Year: 1994\n  Genres: Horror\n"
            "  Themes: late-night radio, a caller who should be dead, small-town paranoia\n"
            "  Mood: eerie, cheap\n  Runtime: 84 min\n  Rating: 5.6/10\n"
            "  Community reception: Clumsy acting and a tiny budget, but a sincere, creepy radio-booth premise "
            "with a small cult following.",
            "sd h264, 1.1 GB, 13 MB/min, 1.05x class median (normal)",
            signals="OWNER SIGNAL: Jane wants to keep the low-budget 90s horror she grew up with, even the "
                    "clumsy ones.\n")),
]
PILLARS = [("pillar_3_household", "III Household"), ("pillar_2_custodian", "II Custodian"),
           ("pillar_1_resonance", "I Resonance"), ("pillar_0_ego", "0 Ego"), ("bitrate_note", "File")]
VERDICT_WORDS = {"HARD_KEEP": "Keep", "KEEP_WITH_FLAG": "Keep, downscale", "CUT": "Delete",
                 "STAGNANT": "Ask the owner", "EVALUATE": "No verdict"}


def matrix_models(matrix: dict) -> list[dict]:
    """The curators the setup can hand out: every tier's recommendation plus
    each alternative cheaper on system RAM (the RAM fallback), with the
    profile and context their package runs at."""
    out: dict = {}
    for tier, row in sorted((matrix.get("tiers") or {}).items(), key=lambda kv: int(kv[0])):
        rec = row.get("recommended")
        if not rec:
            continue
        cands = [rec] + [a for a in row.get("alternatives") or []
                         if ((a.get("expected") or {}).get("ram_need_gb") or 99)
                         < ((rec.get("expected") or {}).get("ram_need_gb") or 99)]
        for o in cands:
            m = out.setdefault(o["model"], {"model": o["model"], "profile": o["profile"],
                                            "num_ctx": o["num_ctx"], "tiers": []})
            if o is rec:
                m["tiers"].append(int(tier))
    return list(out.values())


def snapshot() -> dict:
    return {"id": "janedoe", "lang_directive": "LANGUAGE: Respond in English.",
            "items": [{"key": k, "category": c, "title": t, "facts": f, "law_extra": "", "flags": {},
                       "item": {}} for k, c, t, _, _, _, f in CASES],
            "subset_40": [c[0] for c in CASES]}


def run(model_list: list[dict], log=print, keep_others: bool = False) -> dict:
    DATA.mkdir(parents=True, exist_ok=True)
    snap = DATA / "janedoe_snapshot.json"
    snap.write_text(json.dumps(snapshot(), ensure_ascii=False, indent=1), encoding="utf-8")
    results = []
    with server.BenchServer(log_dir=DATA / "logs") as bench:
        bench.ensure(server.ServerConfig())
        store = models.Store(bench.endpoint, log=log)
        for m in model_list:
            ref = m["model"]
            log(f"=== {ref} ({m['profile']}, ctx {m['num_ctx']})")
            if not store.ready(ref):
                log(f"  could not pull {ref}")
                continue
            bake = models.bake(bench.endpoint, ref, m["num_ctx"])
            out_dir = DATA / ref.replace(":", "_")
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "tests.benchmarks.hwbench.quality", json.dumps({
                        "bake": bake, "profile": m["profile"], "out_dir": str(out_dir),
                        "phases": ["judge", "mono"], "snapshot": str(snap), "items": "full"})],
                    cwd=REPO, env=quality.child_env(bench.endpoint, m["profile"], m["num_ctx"]),
                    capture_output=True, text=True, encoding="utf-8", timeout=3600)
                if proc.returncode:
                    log(proc.stderr[-1500:])
                    continue
            finally:
                server.unload_all(bench.endpoint)
                models.unbake(bench.endpoint, ref)
                store.cleanup(ref)
            judge = {r["key"]: r for r in map(json.loads, (out_dir / "judge.jsonl")
                                                 .read_text(encoding="utf-8").splitlines())}
            notes = {r["key"]: r for r in map(json.loads, (out_dir / "monologue.jsonl")
                                                 .read_text(encoding="utf-8").splitlines())} \
                if (out_dir / "monologue.jsonl").exists() else {}
            results.append({**m, "answers": {k: _answer(judge[k], notes.get(k)) for k in judge}})
            log(f"  {', '.join(f'{k.split(':')[1]}={a['verdict']}' for k, a in results[-1]['answers'].items())}")
    if keep_others and (DATA / "run.json").exists():
        # A partial run (--only) replaces its own models and keeps the rest in place.
        fresh = {r["model"]: r for r in results}
        old = json.loads((DATA / "run.json").read_text(encoding="utf-8"))["models"]
        results = [fresh.pop(r["model"], r) for r in old] + list(fresh.values())
    run_doc = {"generated": datetime.now().isoformat(timespec="seconds"), "models": results}
    (DATA / "run.json").write_text(json.dumps(run_doc, ensure_ascii=False, indent=1), encoding="utf-8")
    return run_doc


def _answer(j: dict, n: dict | None) -> dict:
    raw = j.get("raw") or {}
    return {"verdict": raw.get("verdict"), "protecting_pillar": raw.get("protecting_pillar"),
            **{f: (raw.get(f) or "").strip() for f, _ in PILLARS},
            "guards": raw.get("_guards") or [], "repaired": bool(j.get("repaired")),
            "error": str(raw.get("_error") or "")[:200] if j.get("error_kind") else "",
            "wall_s": j.get("wall_s"), "note": (n or {}).get("text", "").strip()}


def export_wizard(run_doc: dict, path: Path = WIZARD) -> Path:
    case = next(c for c in CASES if c[0] == WIZARD_KEY)
    doc = {"generated": run_doc["generated"], "title": case[2], "setup": case[4], "law": case[5],
           "owner": "Jane Doe (fictional)", "models": {}}
    for m in run_doc["models"]:
        a = m["answers"].get(WIZARD_KEY)
        if not a or a["error"]:
            continue
        decisive = {"HOUSEHOLD": "pillar_3_household", "CUSTODIAN": "pillar_2_custodian",
                    "RESONANCE": "pillar_1_resonance", "EGO": "pillar_0_ego"}.get(a["protecting_pillar"],
                                                                                 "pillar_0_ego")
        doc["models"][m["model"]] = {"verdict": a["verdict"], "verdict_words": VERDICT_WORDS.get(a["verdict"]),
                                     "protecting_pillar": a["protecting_pillar"],
                                     "reason": a[decisive], "note": a["note"]}
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return path


def render_docs(run_doc: dict, review: dict, path: Path = DOCS) -> Path:
    """Markdown comparison. ``review`` maps "model|key" to {"mark": ok|warn|bad, "text": ...}."""
    marks = {"ok": "✓", "warn": "!", "bad": "✕"}
    names = [m["model"] for m in run_doc["models"]]
    L = ["# How the curator judges", "",
         "The curators the setup recommends, judging the same five titles for a **fictional** owner, "
         "Jane Doe. Every verdict below comes from the production judge and card note, word for word; "
         "only the review lines are written by a person after reading the run.", "",
         f"Generated {run_doc['generated'][:10]} by `python -m tests.benchmarks.hwbench.run examples`.", "",
         "**Jane Doe** (invented) likes slow-burn horror and sharp social satire, values economical, "
         "well-crafted filmmaking, and has little patience for formulaic sequels or long sitcoms. "
         "**Sam** lives in her household. Four titles are invented; the fifth carries public metadata only.", "",
         "## What to look for", "",
         "- **The clear cases agree.** The landmark stays, the weak sequel goes, and the sitcom Sam watches "
         "stays although Jane dislikes it: the constitution ranks what the household watches and what is "
         "objectively significant above the owner's taste.",
         "- **The gray zones do not always agree, and each model argues its own way through them.** The "
         "standard is whether the reasoning holds together and follows the law, not whether two models "
         "reach the same verdict.",
         "- **A failed answer never deletes anything.** A verdict that fails validation twice becomes "
         "EVALUATE: neither proposed nor protected.",
         "- **The curator learns.** When the owner disagrees with a verdict and talks it through, Curatarr "
         "extracts a principle from the conversation, checks that it is new, and appends it to the judge's "
         "law. From the next run every model judges with it.", "",
         "## Overview", "",
         "| Title · what decides it | " + " | ".join(f"`{n}`" for n in names) + " |",
         "|---|" + "---|" * len(names)]
    for key, _, title, decides, *_ in CASES:
        cells = []
        for m in run_doc["models"]:
            a = m["answers"].get(key) or {}
            r = review.get(f"{m['model']}|{key}") or {}
            cells.append(f"{VERDICT_WORDS.get(a.get('verdict'), a.get('verdict'))} "
                         f"{marks.get(r.get('mark'), '')}".strip())
        L.append(f"| {title} · {decides} | " + " | ".join(cells) + " |")
    L += ["", "✓ consistent with the constitution · ! a defensible verdict with a flaw in the reasoning "
          "· ✕ an error", ""]
    for key, _, title, decides, setup, law, _f in CASES:
        L += [f"## {title}", "", setup, "", f"*What the constitution asks:* {law}", ""]
        for m in run_doc["models"]:
            a = m["answers"].get(key) or {}
            r = review.get(f"{m['model']}|{key}") or {}
            L += [f"### `{m['model']}` — {VERDICT_WORDS.get(a.get('verdict'), a.get('verdict'))} "
                  f"({a.get('protecting_pillar') or '—'})", ""]
            if a.get("error"):
                L += [f"Rejected twice: `{a['error']}`", ""]
            else:
                L += [f"- **{lab}:** {a.get(f) or '—'}" for f, lab in PILLARS]
                if a.get("guards"):
                    L.append(f"- **Corrected by the fact guards:** {', '.join(a['guards'])}")
                if a.get("note"):
                    L += ["", f"> {a['note']}"]
                L.append("")
            if r:
                L += [f"**Review {marks.get(r.get('mark'), '')}** {r.get('text', '')}", ""]
    path.write_text("\n".join(L).rstrip() + "\n", encoding="utf-8")
    return path
