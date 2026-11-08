"""Tone bench: the curator's voices on replayed turns, for reading.

The cases are real chats and watch facts, so they stay private:
tests/benchmarks/tone_data/cases.json (gitignored with the rest of this
folder). Two kinds:

  {"chat": [{"id": "...", "lang": "en|de", "taste": "<taste block>",
             "history": [["assistant", "..."], ["user", "..."], ...],
             "user": "<the turn to answer>"}],
   "starters": [{"id": "...", "facts": [<collect_facts output>]}]}

Invented cases live in tests/benchmarks/tone_cases_public.json (committed,
every title made up); ``run`` reads both files, each case's "catch" goes
into the report above its replies. A case's optional "tonight" is the
[TONIGHT CANDIDATES] block its turn would carry (services/tonight).

Built 2026-11-06 from a copy of the owner's database: the turns where the
old persona read motives into viewing, argued with the user's own account
or grew a "pattern" from one title, plus pushback on the curator's own
takes. Add cases by hand; keep one failure each.

    python tests/benchmarks/tone_bench.py run [--voices elite,neutral,friendly]
           [--reps 3] [--only chat|starters] [--ollama http://host:11434]
    python tests/benchmarks/tone_bench.py report <run.jsonl>

``run`` writes tone_data/runs/<stamp>.jsonl, ``report`` a markdown next to
it. There is no score: the round trips (2026-11-06) showed the regex flags
miss most of it and a model checker rewrites instead of judging, so the
report is read - by the owner or a reviewing session - against what each
case was built to catch.

The chat system prompt is the router's own (services/chat_prompt, all its
rules) around the case's taste block; memories, RAG and the discussion
blocks stay empty, since they need the live library (2026-11-07; before,
only the voice part). The starters use the production prompt and persona.

Load: one curator call per case, voice and rep; 8k window and num_gpu 99
like curator_options (16k next to the desktop apps overfilled the 4090 and
Windows paged it to 1 token/s). Run with the game closed.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx  # noqa: E402

from src.services import chat_starters as cs  # noqa: E402
from src.services import curator_voice as cv  # noqa: E402
from src.services.chat_prompt import build_system_prompt  # noqa: E402

DATA = Path(__file__).resolve().parent / "tone_data"
CASES = DATA / "cases.json"
PUBLIC_CASES = Path(__file__).resolve().parent / "tone_cases_public.json"


def load_cases() -> dict:
    """The private cases (when this machine has them) plus the public ones."""
    out: dict = {"chat": [], "starters": []}
    for path in (CASES, PUBLIC_CASES):
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            for kind in out:
                out[kind] += data.get(kind, [])
    return out


def _call(url, model, messages, temperature, num_predict):
    t = time.time()
    r = httpx.post(f"{url.rstrip('/')}/api/chat", timeout=900, json={
        "model": model, "messages": messages, "stream": False, "keep_alive": "10m", "think": False,
        "options": {"temperature": temperature, "num_predict": num_predict,
                    "num_ctx": 8192, "num_gpu": 99}})
    r.raise_for_status()
    return r.json()["message"]["content"].strip(), round(time.time() - t, 1)


def chat_system(voice: str, taste: str, lang: str, tonight: str = "") -> str:
    return build_system_prompt(voice=voice, user_lang=lang, taste_context=taste,
                               tonight_context=tonight)


def run(a) -> Path:
    cases = load_cases()
    catch = {c["id"]: c.get("catch", "") for kind in cases.values() for c in kind}
    voices = [v for v in a.voices.split(",") if cv.normalize(v)]
    out = DATA / "runs" / f"{datetime.now():%Y%m%d_%H%M}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as f:
        for rep in range(a.reps):
            for v in voices:
                if a.only in (None, "starters"):
                    for c in cases.get("starters", []):
                        prompt = cs._PROMPT.format(facts=json.dumps(c["facts"], ensure_ascii=False, indent=1),
                                                   n=5, forms=list(cs.FORMS), voice=cv.STARTER_VOICE[v])
                        text, s = _call(a.ollama, a.model, [{"role": "system", "content": cv.persona(v)},
                                                            {"role": "user", "content": prompt}], 0.9, 900)
                        f.write(json.dumps({"kind": "starters", "case": c["id"], "voice": v, "rep": rep,
                                            "out": text, "s": s}, ensure_ascii=False) + "\n")
                        print("starters", c["id"], v, rep, s, "s", flush=True)
                if a.only in (None, "chat"):
                    for c in cases.get("chat", []):
                        msgs = [{"role": "system", "content": chat_system(
                            v, c.get("taste", ""), c.get("lang", "en"), c.get("tonight", ""))}]
                        msgs += [{"role": r, "content": t} for r, t in c.get("history", [])]
                        msgs.append({"role": "user", "content": c["user"]})
                        text, s = _call(a.ollama, a.model, msgs, 0.7, 700)
                        f.write(json.dumps({"kind": "chat", "case": c["id"], "voice": v, "rep": rep,
                                            "catch": catch.get(c["id"], ""),
                                            "out": text, "s": s}, ensure_ascii=False) + "\n")
                        print("chat", c["id"], v, rep, s, "s", flush=True)
    print("wrote", out)
    return out


def report(path: Path) -> Path:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    lines = [f"# Tone bench {path.stem}", ""]
    for kind in ("chat", "starters"):
        lines += [f"## {kind}", ""]
        for r in sorted((r for r in rows if r["kind"] == kind), key=lambda r: (r["case"], r["voice"], r["rep"])):
            lines.append(f"### {r['case']} · {r['voice']} · rep {r['rep']} ({r['s']} s)")
            if r.get("catch"):
                lines.append(f"_Built to catch: {r['catch']}_")
            if kind == "starters":
                try:
                    lines += [f"- *{c.get('form')}*: {c.get('text')}" for c in cs._extract_json_array(r["out"])]
                except Exception:  # noqa: BLE001 - an unparseable batch is shown as it came
                    lines.append(r["out"])
            else:
                lines.append(r["out"])
            lines.append("")
    md = path.with_suffix(".md")
    md.write_text("\n".join(lines), encoding="utf-8")
    print("wrote", md)
    return md


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--voices", default=",".join(cv.VOICES))
    r.add_argument("--reps", type=int, default=3)
    r.add_argument("--only", choices=("chat", "starters"))
    r.add_argument("--ollama", default="http://localhost:11434")
    r.add_argument("--model", default="curatarr-curator")
    p = sub.add_parser("report")
    p.add_argument("path", type=Path)
    a = ap.parse_args()
    if a.cmd == "run":
        report(run(a))
    else:
        report(a.path)
