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
              hidden_metadata_context="HIDDEN-BLOCK")

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
