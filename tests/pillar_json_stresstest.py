#!/usr/bin/env python3
"""
Pillar JSON Stress Test — Step 1 of the "Supreme Court" curation refactor.

Goal: prove the LOCAL curator model can emit a STRICT, schema-valid JSON
verdict (the structured Chain-of-Thought) reliably enough to build the
3-pillar deletion cascade on top of it. If the model can't hold the schema,
the whole LLM-as-judge plan collapses and we fall back to deterministic rules.

Pure stdlib (urllib) — no deps, no venv. Mirrors the real app call path:
  POST /api/chat , format = JSON schema , think=False , num_gpu=99.

Run with the app STOPPED so the 31B model isn't fighting the live curator
for VRAM:

    python tests/pillar_json_stresstest.py            # full matrix
    python tests/pillar_json_stresstest.py --smoke     # 1 call, connectivity proof

It fires 5 extreme cases x N iterations against each model, forcing temp 0,
and checks:
  - valid JSON            - all required keys present
  - verdict in the enum   - verdict matches the expected call (constitution quality)
  - determinism across iterations (same verdict every run)

>=95% on JSON / keys / enum  ->  green light for the refactor.
"""
import json
import sys
import time
import urllib.request
import urllib.error

# ── Config (matches .env + the baked curatarr-curator Modelfile) ─────────────
OLLAMA           = "http://localhost:11434"          # .env OLLAMA_ENDPOINT
# NOTE 2026-08: the OWNER TASTE fixture lines were re-flavored to a FICTIONAL
# owner (the real profile is per-user data and left the repo). The expected
# verdicts are pillar-structure-driven and each case keeps its lever, but the
# gate's calibration should be re-confirmed with one live run before the
# stress_gate column is trusted for a new model.
MODELS           = [
    "gemma4:31b",        # incumbent base (clean)
    "curatarr-curator",  # persona-baked incumbent
    # 2026-08 curator A/B verdict: gemma stays curator; qwen3.8 base won the
    # pipeline bench → two-bake split. The pitcher bake is the artifact that
    # actually judges deletion runs, so it must pass this gate too. (This
    # test sends its own system message, so it validates SCHEMA fitness of
    # the bake — the baked persona voice is only exercised live.)
    "qwen3.8:27b",
    "curatarr-pitcher",  # deletion-run bake (its block 404s RED until built)
]
ITERATIONS       = 3
NUM_CTX          = 8192        # matches the baked Modelfile
NUM_PREDICT      = 800         # headroom so the JSON never truncates mid-object
TEMPERATURE      = 0.0         # the verdict call must be deterministic by construction
PER_CALL_TIMEOUT = 240         # s; a cold 31B load + generation can be slow

# ── The constitution (system prompt) — FIRST DRAFT of the 3 pillars ──────────
# Deliberately concise: one law per pillar, no stacked examples. This is the
# artefact we iterate on, not Python thresholds.
CONSTITUTION = """You are the curation court for Curatarr, deciding whether ONE title stays on a shared 105 TB home server. Judge it against THREE pillars in STRICT priority — a higher pillar's protection can NEVER be overruled by a lower one. Base every word ONLY on the FACTS given; never invent data.

PILLAR III — HOUSEHOLD (highest). The server serves every household user, not only the owner. If the facts show ANOTHER user (not the owner) genuinely engaged with this title — watched it, above all completed it — it is protected for them no matter the owner's taste, and its bitrate is never questioned (household media is sacred). A title another user merely sampled and abandoned (e.g. 2 of 12 episodes, no rating) does NOT trigger this pillar.

PILLAR II — CUSTODIAN. The server is also an archive of film history. A title of genuine objective stature — a landmark or masterwork of its form, or a rare/obscure work at real risk of being lost — is preserved EVEN IF it clashes with the owner's taste. High critical acclaim (Rotten Tomatoes / Metacritic) and major awards are your evidence; use judgment, not a fixed number. Mere competence or popularity is not enough.

PILLAR I — EGO (lowest). For everything else — titles that exist only for the owner's own taste — the OWNER TASTE line in the evidence is the sole taste authority; this constitution carries none of its own. A title that actively provides what that line rewards survives; generic, low-effort work that provides none of it is CUT.

BITRATE is a SEPARATE axis from retention. A kept title may be FLAGGED if its file is a clear bitrate outlier for its visual complexity — but bitrate alone never deletes a title, and never touches a Pillar III title.

VERDICTS:
- HARD_KEEP — protected by Pillar III; or a Pillar II case at sane bitrate; or a strong Pillar I taste-match.
- KEEP_WITH_FLAG — kept under Pillar II or I, but a clear bitrate outlier worth downscaling.
- CUT — no pillar protects it.
- EVALUATE — the facts are genuinely insufficient to decide.

Keep each pillar analysis to ONE or TWO sentences. Fill every field."""

# ── JSON schema handed to Ollama's `format` param (structured outputs) ────────
SCHEMA = {
    "type": "object",
    "properties": {
        "pillar_3_household": {"type": "string"},
        "pillar_2_archive":   {"type": "string"},
        "pillar_1_ego":       {"type": "string"},
        "bitrate_note":       {"type": "string"},
        "verdict": {"type": "string",
                    "enum": ["HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "EVALUATE"]},
    },
    "required": ["pillar_3_household", "pillar_2_archive", "pillar_1_ego", "verdict"],
}
REQUIRED = SCHEMA["required"]
ENUM     = set(SCHEMA["properties"]["verdict"]["enum"])

# ── 5 extreme test cases: (name, evidence, expected_verdict) ─────────────────
CASES = [
    ("Tokyo Story", """TITLE: Tokyo Story (1953) — Film, Drama
OWNER: not watched.
OTHER HOUSEHOLD USERS: none have watched or requested it.
ACCLAIM: Rotten Tomatoes 100%, Metacritic 100, 3 award wins; routinely ranked among the greatest films ever made (Sight & Sound #1, 2012).
CONTENT TAGS: quiet domestic drama, intergenerational conflict, aging, tradition vs modernity, melancholic, contemplative, static cinematography, minimalist.
OWNER TASTE: craves kinetic formal experimentation and paranoid systems-fiction; explicitly dismisses quiet domestic drama as inert.
TECH: 1080p h264, 23 GB, 173 MB/min — 2.4x the median bitrate for its class (clear outlier).""",
     "KEEP_WITH_FLAG"),

    ("Twilight", """TITLE: Twilight (2008) — Film, Romance/Fantasy
OWNER: not watched.
OTHER HOUSEHOLD USERS: User 2 (the owner's partner) watched it to completion (100%), rates the saga highly, and rewatches it.
ACCLAIM: Rotten Tomatoes 49%, Metacritic 56, no major awards.
CONTENT TAGS: teen paranormal romance, melodrama, sanitized.
OWNER TASTE: craves kinetic formal experimentation; dismisses sanitized teen melodrama outright.
TECH: 1080p h264, 9 GB, 78 MB/min — within the normal range for its class.""",
     "HARD_KEEP"),

    ("Manyu Scroll", """TITLE: Manyu Hiken-chou (Manyu Scroll) — Anime series
OWNER: sampled, did not finish; flagged it as lazy fan-service.
OTHER HOUSEHOLD USERS: none have watched or requested it.
ACCLAIM: no notable critical scores or awards; low audience ratings.
CONTENT TAGS: ecchi, heavy fan-service, thin plot, exploitative, no narrative backbone.
OWNER TASTE: rewards formal daring and thematic depth; rejects lazy fan-service outright.
TECH: 1080p h264, 12 GB, ~80 MB/min — normal for its class.""",
     "CUT"),

    ("Abandoned partner anime", """TITLE: Catch Me at the Ballpark! — Anime series (12 episodes)
OWNER: not watched.
OTHER HOUSEHOLD USERS: User 2 (partner) watched 2 of 12 episodes, then stopped two months ago; no rating, no rewatch.
ACCLAIM: no notable critical scores or awards.
CONTENT TAGS: sports comedy, episodic, light, conventional.
OWNER TASTE: craves paranoid systems-fiction and formal daring; no interest in light episodic sports comedy.
TECH: 1080p h264, 8 GB, ~75 MB/min — normal for its class.""",
     "CUT"),

    ("Mr. Robot", """TITLE: Mr. Robot (2015) — TV series
OWNER: watched all 4 seasons to completion; ranks it among their favourites; rewatches it.
OTHER HOUSEHOLD USERS: none.
ACCLAIM: Rotten Tomatoes 93%, Metacritic 79, Golden Globe and Emmy wins.
CONTENT TAGS: hacker thriller, mental illness, unreliable narrator, anti-capitalist, psychological friction, subversive structure.
OWNER TASTE: craves paranoid systems-thrillers, unreliable narration and formal daring — a core taste match.
TECH: 1080p h265, 45 GB across 4 seasons, ~70 MB/min — normal for its class.""",
     "HARD_KEEP"),
]


def call(model: str, evidence: str):
    """One /api/chat call with the schema forced. Returns (content, seconds)."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": CONSTITUTION},
            {"role": "user",   "content": "FACTS:\n" + evidence},
        ],
        "format": SCHEMA,
        "stream": False,
        "think": False,                      # matches llm_utils.ollama_options
        "keep_alive": "10m",
        "options": {
            "temperature": TEMPERATURE,
            "num_predict": NUM_PREDICT,
            "num_ctx": NUM_CTX,
            "num_gpu": 99,                   # force GPU offload, matches the app
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}/api/chat", data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=PER_CALL_TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8"))
    dt = time.time() - t0
    content = (body.get("message") or {}).get("content", "") or ""
    return content, dt


def validate(content: str):
    """(json_ok, keys_ok, enum_ok, verdict, parsed)."""
    try:
        obj = json.loads(content)
    except Exception:
        return False, False, False, None, None
    if not isinstance(obj, dict):
        return True, False, False, None, obj
    keys_ok = all(k in obj for k in REQUIRED)
    verdict = obj.get("verdict")
    return True, keys_ok, verdict in ENUM, verdict, obj


def main():
    smoke = "--smoke" in sys.argv
    models = ["curatarr-curator"] if smoke else MODELS
    iters  = 1 if smoke else ITERATIONS
    cases  = CASES[:1] if smoke else CASES

    print(f"Ollama      : {OLLAMA}")
    print(f"Models      : {models}")
    print(f"Cases x iter: {len(cases)} x {iters}   (temp={TEMPERATURE}, num_ctx={NUM_CTX})")
    print("=" * 72)

    overall_green = True
    for model in models:
        n = json_ok = keys_ok = enum_ok = correct = 0
        latencies, det_cases, first_obj = [], 0, None
        print(f"\n### MODEL: {model}")

        for name, evidence, expected in cases:
            verdicts = []
            for i in range(iters):
                try:
                    content, dt = call(model, evidence)
                except urllib.error.HTTPError as e:
                    print(f"  [{name} #{i+1}] HTTP {e.code} — {e.read()[:200]!r}")
                    n += 1
                    continue
                except Exception as e:
                    print(f"  [{name} #{i+1}] CALL FAILED: {type(e).__name__}: {e}")
                    n += 1
                    continue
                n += 1
                latencies.append(dt)
                ok_j, ok_k, ok_e, verdict, obj = validate(content)
                json_ok += ok_j; keys_ok += ok_k; enum_ok += ok_e
                if ok_e and verdict == expected:
                    correct += 1
                if ok_j and first_obj is None:
                    first_obj = obj
                verdicts.append(verdict)
                mark = "OK " if (ok_j and ok_k and ok_e) else "BAD"
                hit  = "==" if verdict == expected else "!="
                print(f"  [{name} #{i+1}] {mark} verdict={verdict} {hit}{expected}  ({dt:.1f}s)")
            if verdicts and len(set(verdicts)) == 1 and len(verdicts) == iters:
                det_cases += 1

        # ── per-model report ──
        if n == 0:
            print("  no calls completed."); overall_green = False; continue
        pj, pk, pe = 100*json_ok/n, 100*keys_ok/n, 100*enum_ok/n
        pc = 100*correct/n
        pd = 100*det_cases/len(cases)
        avg = sum(latencies)/len(latencies) if latencies else 0
        print(f"  ---")
        print(f"  JSON valid : {json_ok}/{n}  ({pj:.0f}%)")
        print(f"  keys present: {keys_ok}/{n}  ({pk:.0f}%)")
        print(f"  enum valid : {enum_ok}/{n}  ({pe:.0f}%)")
        print(f"  verdict hit: {correct}/{n}  ({pc:.0f}%)   <- constitution quality")
        print(f"  determinism: {det_cases}/{len(cases)} cases stable ({pd:.0f}%)")
        print(f"  avg latency: {avg:.1f}s")
        gate = (pj >= 95 and pk >= 95 and pe >= 95)
        print(f"  RELIABILITY GATE (JSON/keys/enum >=95%): {'GREEN' if gate else 'RED'}")
        if not gate:
            overall_green = False
        if first_obj is not None:
            print(f"  sample output:\n    " +
                  json.dumps(first_obj, indent=2, ensure_ascii=False).replace("\n", "\n    "))

    print("\n" + "=" * 72)
    print(f"OVERALL: {'GREEN — schema is reliable, proceed with the refactor' if overall_green else 'RED — schema unreliable, rethink before building'}")


if __name__ == "__main__":
    main()
