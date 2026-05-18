"""
LLM response utilities.

Handles <think>...</think> chain-of-thought blocks emitted by models like
DeepSeek-R1. When settings.LLM_THINK_TAGS is True, think blocks are stripped
from non-streaming responses and filtered out of streaming token streams.
"""

import json
import re
from typing import Any


def strip_think_tags(content: str) -> str:
    """Remove <think>...</think> blocks AND freeform monologue patterns.

    If a <think> tag is opened but never closed (truncated model output), strip
    from the opening tag to the end so the half-formed reasoning never leaks
    into the user-facing answer.

    Pass 14.11: also strip freeform monologue patterns from reasoning-style
    models that don't use <think> tags. Catches phrases like "Wait, the
    instructions say…", "I must address…", "Actually, looking at the
    metadata…" which the curator model occasionally dumps when the system
    prompt has many rules. Best-effort safety net — the proper fix is the
    NO INTERNAL MONOLOGUE rule in the system prompt.
    """
    from src.config import settings
    cleaned = content

    # <think>-tag stripping is gated on the LLM_THINK_TAGS feature flag —
    # without that flag set, the model isn't expected to emit them.
    if settings.LLM_THINK_TAGS:
        cleaned = re.sub(r"<think>.*?</think>", "", cleaned, flags=re.DOTALL)
        if "<think>" in cleaned:
            cleaned = cleaned[: cleaned.find("<think>")]

    # Freeform monologue cleanup runs ALWAYS — independent of the tag flag —
    # because reasoning-style models occasionally dump self-talk regardless
    # of the modelfile's think setting. Look for the typical
    # "Wait,"/"Actually,"/"Let me"/etc. opener and remove everything up to
    # the next paragraph break IF it looks like deliberation text (contains
    # rule-quote markers like "the instructions say" / "I must" / etc.).
    monologue_markers = (
        r"(?:Wait,?\s|Actually,?\s|Let me\s|I must\s|I should\s|"
        r"Looking at\s|Per my instructions|The instructions say|The rules say|"
        r"My rule is|However, I am instructed|The current metadata block)"
    )
    cleaned = re.sub(
        rf"(?:^|\n\n){monologue_markers}[^\n]*?(?:instructions|rule|metadata|trust it|training memory|NO INVENTION|Item:|context block)[^\n]*(?:\n[^\n]+)*?(?=\n\n|$)",
        "",
        cleaned,
        flags=re.IGNORECASE | re.DOTALL,
    )

    return cleaned.strip()


def _strip_code_fence_lang(text: str) -> str:
    """Remove a leading ``json``/``python``/``text`` language hint, if present.

    Uses ``str.removeprefix`` (proper substring match) — NOT ``lstrip`` which
    chews characters and corrupts inputs that happen to start with j/s/o/n.
    """
    text = text.lstrip()
    for lang in ("json", "python", "text"):
        if text.startswith(lang):
            return text[len(lang):].lstrip()
    return text


def clean_llm_text(content: str) -> str:
    """Strip think tags and markdown code fences, return plain text."""
    text = strip_think_tags(content)
    if "```" in text:
        parts = text.split("```")
        # ```json\n...\n``` → take the inner part
        if len(parts) >= 3:
            text = _strip_code_fence_lang(parts[1].strip())
        else:
            text = parts[1].strip()
    return text.strip()


def parse_llm_json(content: str) -> Any:
    """Strip think tags + markdown fences, then parse JSON. Raises JSONDecodeError on failure."""
    return json.loads(clean_llm_text(content))


# ── Model lifetime tuning ────────────────────────────────────────────────────
#
# Ollama's default keep_alive is 5 minutes. That's too short for the curator
# (22 GB to reload between chat turns separated by even a brief pause) and
# too long for the summarizer (sits in 14 GB of VRAM that the curator needs).
#
# CURATOR_KEEP_ALIVE
#   The curator is the user-facing chat model; we want it resident as long
#   as it's plausible the user comes back. 1 hour balances "instant next
#   response" against "release VRAM eventually if the session is over".
#
# SUMMARIZER_KEEP_ALIVE
#   The summarizer fires from background tasks (memory extraction, protection
#   intent, entity detection, proactive messages, verification). If it
#   stuck around at default keep_alive, Ollama would evict the curator to
#   make room — exactly the aggressive churn the user complained about.
#   30 s lets back-to-back background tasks reuse a hot model, but releases
#   VRAM well before the user's next chat turn lands.
CURATOR_KEEP_ALIVE = "1h"
SUMMARIZER_KEEP_ALIVE = "30s"

# ── Curator idle eviction (Pass 14.7) ────────────────────────────────────────
#
# Even with CURATOR_KEEP_ALIVE=1h, we want to actively evict the curator after
# a stretch of idle time so the summarizer / enrichment workers don't fight
# Ollama for VRAM. Without explicit eviction, the curator sits in 22 GB of
# VRAM long after the user stopped chatting, blocking background pipelines.
#
# CURATOR_IDLE_EVICT_SECONDS — default delay after the last curator call
#                              before we evict the curator. Tuned for a
#                              typical chat-read + follow-up cycle (read
#                              answer ~30s, type follow-up ~30s).
#
# CURATOR_IDLE_EVICT_BUSY    — shorter delay used when at least one summarizer
#                              call is queued / waiting at the moment the
#                              curator finishes. The user's session may still
#                              be active, but the background queue is paying
#                              the price in latency — get the curator out
#                              faster so background can drain.
CURATOR_IDLE_EVICT_SECONDS = 60
CURATOR_IDLE_EVICT_BUSY    = 10


def ollama_options(temperature: float = 0.7, num_predict: int = 1024, **extra) -> dict:
    """
    Build Ollama request parameters meant to be **spread into the request dict**::

        json={"model": m, "messages": msgs, "stream": False, **ollama_options(...)}

    Returns two top-level keys:

    ``think``
        Always ``False``.  Suppresses ``<think>`` blocks at the API level.
        Harmless for models without think blocks; prevents token-budget drain
        on reasoning models (DeepSeek-R1, Qwen3, etc.) so num_predict is
        fully available for actual output.

    ``options``
        Nested Ollama options dict.  ``num_gpu=99`` forces maximum GPU-layer
        offloading — without it Ollama can silently fall back to 100 % CPU.

    Pass any extra Ollama options as keyword arguments::

        ollama_options(temperature=0.1, num_predict=700)
        # → {"think": False, "options": {"temperature": 0.1, "num_predict": 700, "num_gpu": 99}}
    """
    return {
        "think": False,
        "options": {"temperature": temperature, "num_predict": num_predict, "num_gpu": 99, **extra},
    }


class ThinkTagStreamFilter:
    """
    Stateful token filter for streaming LLM responses.

    Suppresses tokens inside <think>...</think> blocks so callers only see
    the actual answer. Handles tags that are split across chunk boundaries.

    Usage::

        f = ThinkTagStreamFilter()
        for token in stream:
            to_emit = f.feed(token)
            if to_emit:
                send_to_client(to_emit)
        remainder = f.flush()   # emit anything still buffered
    """

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._in_think = False
        self._buf = ""

    def feed(self, token: str) -> str:
        """Return text to emit (empty string means suppress this token)."""
        if not self.enabled:
            return token

        self._buf += token
        output = ""

        while True:
            if self._in_think:
                end = self._buf.find("</think>")
                if end >= 0:
                    self._in_think = False
                    self._buf = self._buf[end + 8:]
                else:
                    # Still inside think; keep only a potential partial closing tag
                    for tail in ("</think", "</thin", "</thi", "</th", "</t", "</"):
                        if self._buf.endswith(tail):
                            self._buf = tail
                            return output
                    self._buf = ""
                    return output
            else:
                start = self._buf.find("<think>")
                if start >= 0:
                    output += self._buf[:start]
                    self._in_think = True
                    self._buf = self._buf[start + 7:]
                else:
                    # Keep potential partial opening tag at end of buffer
                    for head in ("<think", "<thin", "<thi", "<th", "<t", "<"):
                        if self._buf.endswith(head):
                            output += self._buf[: -len(head)]
                            self._buf = head
                            return output
                    output += self._buf
                    self._buf = ""
                    break

        return output

    def flush(self) -> str:
        """Return any buffered content that was held for tag detection."""
        if self._in_think:
            self._buf = ""
            return ""
        result = self._buf
        self._buf = ""
        return result


# ── Pass 40: language detection + directive helpers ────────────────────────
#
# Five LLM surfaces (chat, proactive messages, recs pitches, memory
# extraction, verification) used to have inconsistent language policies:
# chat matched the user, proactive forced English, recs had no directive
# and defaulted to whatever the model preferred (usually English). A
# German user could ask a German question, get a German chat answer,
# then receive a proactive nudge or deletion pitch in English. Real
# persona break.
#
# Resolution:
#   USER-FACING SURFACES → match the user's language (this module).
#   INTERNAL DATA STORAGE → keeps English (memory extraction stays as-is
#   because memories are canonical retrieval indexes — switching language
#   per session would fragment the embedding space).
#
# The detector is a deliberately tiny heuristic. No new dependency, no
# external API. Distinguishes German from English well; everything else
# silently falls back to English. Extend the table below if you need more.

_LANGUAGE_NAMES = {
    "de": "German (Deutsch)",
    "en": "English",
}

# Whole-word tokens that strongly indicate German content. The list is
# kept short on purpose — too many entries and the heuristic starts
# matching anglicised loan-words.
_DE_TOKENS = (
    "der die das und nicht ist ich wir dass weil "
    "auch noch schon haben werden können bin bist "
    "doch oder aber wenn dann mal jetzt sehr eine einen "
    "ja nein bitte vielleicht denke meine"
).split()


def detect_user_language(user_id: int, db) -> str:
    """Return ISO-639-1 code based on the user's recent chat content.

    Looks at the last 20 user-role messages. If none exist or content is
    too short to judge, returns 'en'. Currently distinguishes German vs
    English; other languages fall through to 'en'.

    Pass 99-fu6: density-based detection. The previous single-threshold
    formula ``de_chars + de_words*2 >= 5`` tripped on as little as three
    German cognates ("die", "ist", "ich") buried in 11 K of English text.
    Real-world case: a user with one German sentence ("hier ist die liste
    an titeln dazu") in months of English chat got German pitches on every
    deletion proposal. New logic trips ``de`` only when ONE of:

      - 2+ umlauts (very strong DE signal, hard to false-positive)
      - 1+ umlaut AND 2+ tokens (umlaut + corroborator)
      - 5+ distinct tokens AND density >= 1 / 1000 chars

    The density floor stops a brief German line in a long English context
    from tripping; the 5-token floor protects against random 1-2 cognate
    hits in any-length English text. Umlauts stay the strongest signal —
    they're nearly impossible to encounter accidentally in English.
    """
    from src.database.models import ConversationMessage
    msgs = (
        db.query(ConversationMessage.content)
        .filter(
            ConversationMessage.user_id == user_id,
            ConversationMessage.role == "user",
        )
        .order_by(ConversationMessage.created_at.desc())
        .limit(20)
        .all()
    )
    if not msgs:
        return "en"
    text = " ".join((m.content or "") for m in msgs).lower()
    if len(text) < 20:
        return "en"
    # German signals: umlauts/ß characters + common whole-word tokens.
    # Whole-word matching avoids matching "der" inside "wonder", etc.
    padded = f" {text} "
    de_chars = sum(text.count(c) for c in "äöüß")
    de_words = sum(1 for w in _DE_TOKENS if f" {w} " in padded)

    if de_chars >= 2:
        return "de"
    if de_chars >= 1 and de_words >= 2:
        return "de"
    # Density check: tokens-per-1000-chars. Catches genuinely-German users
    # with no umlauts in their last 20 messages (rare but possible —
    # umlautless German is a thing). Rejects long-English texts with a
    # handful of accidental cognate hits.
    density = (de_words * 1000.0) / max(len(text), 1)
    if de_words >= 5 and density >= 1.0:
        return "de"
    return "en"


def language_directive(code: str) -> str:
    """1-line prompt directive telling the LLM which language to answer in.

    Always returns a non-empty string so callers can blindly include it
    without conditional branches. The second sentence keeps technical
    fields (ratings, tags, identifiers) untranslated.
    """
    name = _LANGUAGE_NAMES.get(code, "English")
    return (
        f"LANGUAGE: Respond in {name}. "
        f"Keep proper nouns, ratings, tags, and identifiers in their original form."
    )
