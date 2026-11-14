"""Day-zero probe: is a new model even benchable? Minutes, not hours.

    python -m tests.benchmarks.hwbench.run probe --model qwen4:27b [--profile small]

Runs against an Ollama that already has the model (default the owner's,
:11434) and checks the four things that sank or nearly sank earlier
candidates before a single bench cell was spent on them:

  think     a plain chat call with ``think: false``: does the answer land in
            ``message.content``, or does the model think anyway? gpt-oss:20b
            ignored think:false and spent 440 of its tokens thinking - on real
            evidence the judge's budget ran out before the JSON was done
            (2026-11-03); gemma4's new weights answered only in ``thinking``.
  judge     the production judge call on the two Jane Doe fixtures of the
            setup mini-bench: the profile's constitution, VERDICT_SCHEMA,
            temperature 0, the first-try budget (pillars._JUDGE_PREDICT),
            validated by pillars._parse_verdict - bare labels ("NONE"),
            truncation and enum misses show up here.
  speed     the same prompt over /api/chat and /api/generate: gemma4's new
            format wrote 10.5 tok/s over generate against 33 over chat
            (2026-10-15); the bench and production use chat.
  language  the production chat prompt (chat_prompt.build_system_prompt) with
            a German question: does the answer follow the user's language?

  system    a system message that asks for one code word: is it followed? A
            model that drops the system role would never see the constitution.

Plus what /api/show says: capabilities, context length, renderer. No owner data: fixtures and questions are
the fictional Jane Doe ones. The result is printed and saved under
hwbench_data/probes/. A probe on a busy GPU (a game, a running bench) is
refused - it would disturb what is running.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path

import httpx

DATA = Path(__file__).resolve().parent.parent / "hwbench_data" / "probes"
PROFILE_CTX = {"large": 16384, "small": 8192}
THINK_PROMPT = "Name one film by Akira Kurosawa and say in one sentence why it matters."
SPEED_PROMPT = "Write about 150 words on why film restoration matters."
SPEED_PREDICT = 160
LANG_PROMPT = "Was hältst du von Stalker von Andrei Tarkowski? Bitte in zwei Sätzen."
SYSTEM_WORD = "PELICAN"
SYSTEM_PROMPT = f"Whatever the user writes, answer with the single word {SYSTEM_WORD} and nothing else."
SPEED_RATIO_WARN = 0.66      # one endpoint under 2/3 of the other's speed
_GERMAN = re.compile(r"\b(und|nicht|der|die|das|ist|ein|eine|mit|auch|sich|wie)\b", re.I)
_ENGLISH = re.compile(r"\b(and|not|the|is|a|an|with|also|it|this|that|of)\b", re.I)


def _chat(client: httpx.Client, url: str, payload: dict) -> dict:
    t0 = time.monotonic()
    r = client.post(f"{url}/api/chat", json={"stream": False, **payload})
    r.raise_for_status()
    d = r.json()
    msg = d.get("message") or {}
    return {"content": msg.get("content") or "", "thinking": msg.get("thinking") or "",
            "eval_count": d.get("eval_count"), "done_reason": d.get("done_reason"),
            "tok_s": _tok_s(d), "wall_s": round(time.monotonic() - t0, 2)}


def _tok_s(d: dict) -> float | None:
    if d.get("eval_count") and d.get("eval_duration"):
        return round(d["eval_count"] / (d["eval_duration"] / 1e9), 1)
    return None


def language_of(text: str) -> str:
    de, en = len(_GERMAN.findall(text)), len(_ENGLISH.findall(text))
    if not de and not en:
        return "unknown"
    return "de" if de > en else "en"


def assess(r: dict) -> list[tuple[str, str, str]]:
    """(check, PASS|WARN|FAIL, detail) per check from the raw results. Pure,
    so the rules are testable without a model."""
    out = []
    t = r.get("think") or {}
    if t:
        if not t["content"].strip() and t["thinking"]:
            out.append(("think", "FAIL", f"answer only in 'thinking' ({len(t['thinking'])} chars), "
                                         "content empty - the bench would score nothing"))
        elif t["thinking"]:
            out.append(("think", "WARN", f"think:false ignored - {len(t['thinking'])} chars of thinking "
                                         "count against every num_predict"))
        elif "<think>" in t["content"]:
            out.append(("think", "WARN", "<think> tags inside content"))
        else:
            out.append(("think", "PASS", "answers in content, no thinking"))
    judges = r.get("judge") or []
    if judges:
        ok = [j for j in judges if j.get("valid")]
        faults = "; ".join(f"{j['fixture']}: {j['error']}" for j in judges if not j.get("valid"))
        thought = max((len(j.get("thinking") or "") for j in judges), default=0)
        extra = f" (up to {thought} chars of thinking per verdict)" if thought else ""
        status = "PASS" if len(ok) == len(judges) else ("WARN" if ok else "FAIL")
        out.append(("judge", status, f"{len(ok)}/{len(judges)} verdicts pass the schema{extra}"
                                     + (f" - {faults}" if faults else "")))
    s = r.get("speed") or {}
    if s.get("chat_tok_s") and s.get("generate_tok_s"):
        lo, hi = sorted((s["chat_tok_s"], s["generate_tok_s"]))
        status = "WARN" if lo < SPEED_RATIO_WARN * hi else "PASS"
        out.append(("speed", status, f"chat {s['chat_tok_s']} tok/s, generate {s['generate_tok_s']} tok/s"))
    lang = r.get("language") or {}
    if lang:
        status = "PASS" if lang.get("detected") == "de" else "WARN"
        out.append(("language", status, f"German question answered in '{lang.get('detected')}'"))
    sysm = r.get("system") or {}
    if sysm:
        out.append(("system", "PASS" if sysm.get("obeyed") else "FAIL",
                    "the system message is followed" if sysm.get("obeyed")
                    else "the system message was ignored - the constitution would never reach the judge"))
    show = r.get("show") or {}
    if show:
        out.append(("info", "PASS",
                    f"capabilities {', '.join(show.get('capabilities') or []) or '?'}; "
                    f"context {show.get('context_length') or '?'}; "
                    f"renderer {show.get('renderer') or 'template'}"))
    return out


def _show(client: httpx.Client, url: str, model: str) -> dict:
    d = client.post(f"{url}/api/show", json={"model": model}).json()
    info = d.get("model_info") or {}
    ctx = next((v for k, v in info.items() if k.endswith(".context_length")), None)
    # Newer models render through a built-in renderer (gemma4, qwen3.8: the
    # template is a 13-character stub) or loop over .Messages (granite4.1),
    # so the template text cannot tell whether a system message arrives -
    # the "system" check asks the model instead.
    return {"capabilities": d.get("capabilities") or [], "context_length": ctx,
            "renderer": d.get("renderer"), "family": (d.get("details") or {}).get("family")}


def run_probe(url: str, model: str, profile: str = "large", log=print) -> dict:
    from src.services.chat_prompt import build_system_prompt
    from src.services.pillars import (VERDICT_SCHEMA, _JUDGE_PREDICT, _parse_verdict,
                                      judge_system_prompt)
    from src.services.setup_minibench import FIXTURES
    ctx = PROFILE_CTX[profile]
    res: dict = {"model": model, "profile": profile, "num_ctx": ctx, "endpoint": url,
                 "at": datetime.now().isoformat(timespec="seconds")}
    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=600.0, write=10.0, pool=10.0)) as c:
        res["show"] = _show(c, url, model)
        opts = {"num_ctx": ctx}
        _chat(c, url, {"model": model, "think": False, "keep_alive": "5m",
                       "messages": [{"role": "user", "content": "hi"}],
                       "options": {**opts, "num_predict": 4}})       # load once, untimed
        log("  system …")
        sysr = _chat(c, url, {"model": model, "think": False,
                              "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                                           {"role": "user", "content": "Say hello."}],
                              "options": {**opts, "temperature": 0.0, "num_predict": 20}})
        res["system"] = {"obeyed": SYSTEM_WORD in sysr["content"].upper()}
        log("  think …")
        res["think"] = _chat(c, url, {"model": model, "think": False,
                                      "messages": [{"role": "user", "content": THINK_PROMPT}],
                                      "options": {**opts, "temperature": 0.0, "num_predict": 300}})
        res["judge"] = []
        for key, facts in FIXTURES:
            log(f"  judge {key} …")
            j = _chat(c, url, {"model": model, "think": False, "format": VERDICT_SCHEMA,
                               "messages": [{"role": "system", "content": judge_system_prompt(profile=profile)},
                                            {"role": "user", "content": "FACTS:\n" + facts}],
                               "options": {**opts, "temperature": 0.0, "seed": 7, "repeat_penalty": 1.0,
                                           "num_predict": _JUDGE_PREDICT}})
            j["fixture"] = key
            try:
                j["verdict"], j["valid"] = _parse_verdict(j["content"]).verdict, True
            except Exception as e:      # a schema miss is a RESULT here
                cut = " (cut off: num_predict exhausted)" if j.get("done_reason") == "length" else ""
                j["valid"], j["error"] = False, str(e)[:120] + cut
            res["judge"].append(j)
        log("  speed …")
        sp = {"model": model, "think": False, "options": {**opts, "temperature": 0.0, "seed": 7,
                                                          "num_predict": SPEED_PREDICT}}
        chat = _chat(c, url, {**sp, "messages": [{"role": "user", "content": SPEED_PROMPT}]})
        g = c.post(f"{url}/api/generate", json={**sp, "prompt": SPEED_PROMPT, "stream": False})
        g.raise_for_status()
        res["speed"] = {"chat_tok_s": chat["tok_s"], "generate_tok_s": _tok_s(g.json())}
        log("  language …")
        system = build_system_prompt(voice="neutral", user_lang="de", ui_lang="de")
        lang = _chat(c, url, {"model": model, "think": False,
                              "messages": [{"role": "system", "content": system},
                                           {"role": "user", "content": LANG_PROMPT}],
                              "options": {**opts, "temperature": 0.7, "num_predict": 300}})
        res["language"] = {"detected": language_of(lang["content"]), "chars": len(lang["content"])}
        c.post(f"{url}/api/generate", json={"model": model, "keep_alive": 0})   # leave the card free
    res["assessment"] = [list(a) for a in assess(res)]
    return res


def save(res: dict) -> Path:
    DATA.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-zA-Z0-9.]+", "-", res["model"]).strip("-")
    path = DATA / f"{slug}_{res['profile']}_{datetime.now():%Y%m%d-%H%M}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    return path
