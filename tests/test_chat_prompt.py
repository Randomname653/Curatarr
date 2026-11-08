"""The chat's system prompt (services/chat_prompt): one builder for the
router and the chat benches. The router's system message replaces the
baked persona, so the rules a chat turn follows have to be in here - and a
bench that wants to measure the chat has to build its turns with it.
"""
from _harness import check  # noqa: I001 - first: temp stores

from pathlib import Path

from src.services import chat_prompt as cp  # noqa: E402
from src.services import curator_voice as cv  # noqa: E402
from src.services.llm_utils import language_directive  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

blocks = dict(taste_context="TASTE-BLOCK", memory_context="[MEMORIES] m", rag_context="RAG-BLOCK",
              discuss_block="DISCUSS-BLOCK", pillar_framework="PILLAR-BLOCK",
              hidden_metadata_context="HIDDEN-BLOCK", tonight_context="TONIGHT-BLOCK")

for v in cv.VOICES:
    sp = cp.build_system_prompt(voice=v, user_lang="de", active_title="Some Title", **blocks)
    check(f"{v}: the voice's identity opens it and its tone rule is in it",
          sp.startswith(cv.IDENTITY[v]) and cv.chat_tone(v) in sp)
check("every block the router passes lands in it, the discussion after the memories",
      all(b in sp for b in blocks.values()) and sp.index("[MEMORIES] m") < sp.index("DISCUSS-BLOCK"))
check("the detected language is rule 1", f"1. {language_directive('de')}" in sp)
check("a claimed earlier chat that is not in context is never confirmed",
      cp.EARLIER_CHATS_RULE in sp and "never confirm it as fact" in cp.EARLIER_CHATS_RULE)
check("with a title under discussion: topic lock and the current focus",
      cp.TOPIC_LOCK_RULE in sp and "CURRENT FOCUS: The title under discussion is 'Some Title'." in sp)

free = cp.build_system_prompt(voice="neutral", user_lang="en")
check("free chat: no topic lock, no focus line, and the empty taste block says so",
      cp.TOPIC_LOCK_RULE not in free and "CURRENT FOCUS" not in free and "No taste profile yet." in free)
check("the other rules come with every turn",
      all(r in free for r in (cp.NO_INVENTION_RULE, cp.NO_LIBRARY_ACTIONS_RULE,
                              cp.NO_MONOLOGUE_RULE, cp.ANALYTICAL_INTEGRITY_RULE)))

check("rule 8 (conversation shape) is one line and comes with every turn, free chat and a "
      "title discussion alike - a free-chat thread keeps its last title as the active one",
      "\n" not in cp.CONVERSATION_RULE and cp.CONVERSATION_RULE.startswith("8. ")
      and cp.CONVERSATION_RULE in free and cp.CONVERSATION_RULE in sp)
check("rule 8 leaves the user a longer list when they ask for one",
      "longer list only when they ask" in cp.CONVERSATION_RULE)

# The benches build their chat turns with the same builder (the router's use
# of it is pinned in test_watchlist_hook).
import importlib.util  # noqa: E402
import os  # noqa: E402


def _bench(name):
    cwd = os.getcwd()
    spec = importlib.util.spec_from_file_location(name, ROOT / "tests/benchmarks" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    finally:
        os.chdir(cwd)  # curator_bench moves to the repo root on import
    return mod


tb = _bench("tone_bench")
check("tone_bench: a chat case carries the router's prompt around its taste block",
      tb.chat_system("elite", "TASTE-BLOCK", "de")
      == cp.build_system_prompt(voice="elite", user_lang="de", taste_context="TASTE-BLOCK"))
import json  # noqa: E402

pub = json.loads(tb.PUBLIC_CASES.read_text(encoding="utf-8"))
check("tone_bench: the public cases are chat cases with an id, a language, a user turn and "
      "what each was built to catch; load_cases reads them without the private file",
      pub["chat"] and all(c.get("id") and c.get("lang") in ("en", "de") and c.get("user")
                          and c.get("catch") for c in pub["chat"])
      and {c["id"] for c in pub["chat"]} <= {c["id"] for c in tb.load_cases()["chat"]})
from src.services import tonight as _tonight  # noqa: E402

check("tone_bench: every public case's [TONIGHT CANDIDATES] block opens with the header the chat "
      "sends today; a typed targeted ask has a case of its own and, as in the app, no block",
      all(c["tonight"].startswith(_tonight.HEADER) for c in pub["chat"] if c.get("tonight"))
      and any(c["id"] == "pub_specific_ask_en" and not c.get("tonight") for c in pub["chat"])
      and any(c["id"] == "pub_tonight_en" and c.get("tonight") for c in pub["chat"]))
cb = _bench("curator_bench")
ctx = 'You previously proposed deleting "Some Title" (anime series, 6 GB).'
sp = cb.system_prompt(ctx)
check("curator_bench: a hot-start case is the discussion block, its title the active one",
      sp.startswith(cv.IDENTITY["elite"]) and ctx in sp and cp.TOPIC_LOCK_RULE in sp
      and "CURRENT FOCUS: The title under discussion is 'Some Title'." in sp)
cb.PROMPT["mode"] = "bake"
check("curator_bench --prompt bake still sends the baked persona, for the old runs",
      cb.system_prompt(ctx).startswith(cb.CURATOR_SYSTEM_PROMPT) and cp.EARLIER_CHATS_RULE not in cb.system_prompt(ctx))

check.done()
