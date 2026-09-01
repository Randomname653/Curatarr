"""
LLM response utilities.

Handles <think>...</think> chain-of-thought blocks emitted by models like
DeepSeek-R1. When settings.LLM_THINK_TAGS is True, think blocks are stripped
from non-streaming responses and filtered out of streaming token streams.
"""

import json
import re
from datetime import datetime
from typing import Any


def seasonal_context(now: datetime | None = None) -> str:
    """ONE concise prompt line anchoring 'now' — month + holiday window.
    Used as a soft ingredient by the recommendation and collection-designer
    prompts; deliberately says nothing about availability or streaming."""
    now = now or datetime.utcnow()
    m, d = now.month, now.day
    if m == 10:
        season = "Halloween season"
    elif (m == 11 and d >= 25) or (m == 12 and d <= 26):
        season = "the holiday season"
    elif m in (6, 7, 8):
        season = "summer"
    elif m in (12, 1, 2):
        season = "winter"
    elif m in (3, 4, 5):
        season = "spring"
    else:
        season = "autumn"
    return f"Seasonal context: it is {now.strftime('%B')} — {season}."


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

    return strip_latex_artifacts(cleaned).strip()


# Models sometimes reach for LaTeX when they mean an arrow or a symbol, and
# "$\rightarrow$" then reaches the reader verbatim — it turned up inside a
# pillar breakdown shown to a user. Map the handful that actually occur.
# A bare "$" is deliberately left alone: in this app it is far more often a
# price than a formula.
_LATEX_SYMBOLS = {
    r"\rightarrow": "\u2192",
    r"\Rightarrow": "\u21d2",
    r"\leftarrow": "\u2190",
    r"\Leftarrow": "\u21d0",
    r"\times": "\u00d7",
    r"\approx": "\u2248",
    r"\neq": "\u2260",
    r"\geq": "\u2265",
    r"\leq": "\u2264",
    r"\pm": "\u00b1",
    r"\to": "\u2192",          # last: a prefix of nothing else above
}

_LATEX_WRAPPER = re.compile(r"\\(?:text|textbf|mathrm|mathbf)\{([^}]*)\}")


def strip_latex_artifacts(content: str) -> str:
    """Turn stray inline LaTeX into the character it stood for."""
    if not content or "\\" not in content:
        return content
    for command, char in _LATEX_SYMBOLS.items():
        content = content.replace(f"${command}$", char)
        content = content.replace(f"{command} ", f"{char} ")
    # \text{...} and friends: keep the words, drop the markup.
    return _LATEX_WRAPPER.sub(r"\1", content)


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

# Curator context window — benchmarked 2026-07-08 (tests/benchmarks/
# num_ctx_bench.py, nomic resident, RTX 4090 24GB): 16384 runs 100% GPU at
# full speed (32-34 t/s, 23.96 GB total); 20480 tips over (generation
# halves). Passed as a REQUEST option — no model rebuild involved. MUST be
# identical on EVERY curator call site: a num_ctx mismatch between requests
# forces a full model reload (~6.5 s) on each switch. This constant also
# governs the PITCHER bake's calls (two-bake split): the deletion run's
# judge + monologue both pin it, so one pitcher instance stays resident
# for the whole batch — do NOT introduce a separate PITCHER_NUM_CTX.
CURATOR_NUM_CTX = 16384


def curator_options(temperature: float = 0.7, num_predict: int = 1024, **extra) -> dict:
    """ollama_options for CURATOR calls — pins the shared num_ctx so chat,
    judge, proactive and principle calls all reuse one resident instance."""
    return ollama_options(temperature, num_predict,
                          num_ctx=CURATOR_NUM_CTX, **extra)


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

# The English mirror of _DE_TOKENS. Umlauts alone are NOT the un-fakeable
# German signal the first cut assumed: a German keyboard puts ö next to l,
# so an English sentence ending "…at aöö" (meant: "at all") carries two
# umlauts — and band names do it on purpose (Motörhead, Blue Öyster Cult).
# Only function words that are not also German words; "was"/"man"/"die"
# style collisions are deliberately absent.
_EN_TOKENS = (
    "the and you that this what but not have with "
    "they from your does are of it is to did"
).split()


def detect_user_language(user_id: int, db, thread_id: str = None,
                         current_message: str = None) -> str:
    """Return ISO-639-1 code based on the user's recent chat content.

    Precedence: the CURRENT message (when substantial) > this thread's own
    user messages > English. A user whose app history is German but who is
    holding THIS conversation in English gets English — the live
    conversation outranks the average, and a caller with no live
    conversation at all (batch pitches, proactive nudges) gets English
    rather than a guess drawn from unrelated chat. If nothing is
    classifiable, returns 'en'. Currently distinguishes German vs English;
    other languages fall through to 'en'.

    Pass 99-fu6: density-based detection. The previous single-threshold
    formula ``de_chars + de_words*2 >= 5`` tripped on as little as three
    German cognates ("die", "ist", "ich") buried in 11 K of English text.
    Real-world case: a user with one German sentence ("hier ist die liste
    an titeln dazu") in months of English chat got German pitches on every
    deletion proposal. Current logic trips ``de`` only when ONE of:

      - 2+ umlauts AND the text doesn't read as English around them
        (a German token present, or at most one English function word)
      - 1+ umlaut AND 2+ tokens (umlaut + corroborator)
      - 5+ distinct tokens AND density >= 1 / 1000 chars

    The umlaut rule was unconditional once ("hard to false-positive") and
    the false positive arrived promptly: an English sentence ending in the
    typo "aöö" — l and ö are neighbours on a German keyboard — flipped a
    live discussion to German mid-thread, and the model then invented a
    reason for the switch when the user asked. English evidence now vetoes
    stray umlauts; genuine German keeps at least one function word or
    carries no English ones. The density floor stops a brief German line
    in a long English context from tripping; the 5-token floor protects
    against random 1-2 cognate hits in any-length English text.
    """
    from src.database.models import ConversationMessage

    def _classify(text: str):
        """'de' / 'en' for a text, or None when it's too thin to judge."""
        text = (text or "").lower()
        if len(text) < 20:
            return None
        # German signals: umlauts/ß characters + common whole-word tokens.
        # Whole-word matching avoids matching "der" inside "wonder", etc.
        padded = f" {text} "
        de_chars = sum(text.count(c) for c in "äöüß")
        de_words = sum(1 for w in _DE_TOKENS if f" {w} " in padded)
        en_words = sum(1 for w in _EN_TOKENS if f" {w} " in padded)
        # Umlauts decide only when the text doesn't read as English around
        # them. Live failure (Supervixens discussion): an English sentence
        # ending in the typo "aöö" (l/ö adjacency on a German keyboard) hit
        # the old unconditional 2-umlaut rule, flipped one reply to German,
        # and poisoned the thread fallback for the short follow-up — after
        # which the curator, asked why it switched, invented a motive from
        # the taste profile. Genuine German with umlauts virtually always
        # carries a German function word, or at least no English ones.
        if de_chars >= 2 and (de_words >= 1 or en_words <= 1):
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

    # 1. The CURRENT message wins when it's substantial enough to judge. The
    # global history said "de" for a user whose whole app history is German —
    # and the curator then insisted on German through an all-English
    # conversation ("Deine Sprache ist irrelevant") because the directive it
    # quoted really did say German. The person typing RIGHT NOW outranks
    # their historical average.
    if current_message:
        lang = _classify(current_message)
        if lang:
            return lang

    # 2. This thread's own user messages — a conversation held in English
    # stays English even when the rest of the app history is German.
    if thread_id:
        t_msgs = (
            db.query(ConversationMessage.content)
            .filter(
                ConversationMessage.user_id == user_id,
                ConversationMessage.role == "user",
                ConversationMessage.thread_id == thread_id,
            )
            .order_by(ConversationMessage.created_at.desc())
            .limit(10)
            .all()
        )
        lang = _classify(" ".join((m.content or "") for m in t_msgs))
        if lang:
            return lang

    # 3. No live conversation to read: answer in English.
    #
    # This used to classify the user's last 20 messages ACROSS ALL THREADS,
    # which is the wrong question for a surface that isn't part of any
    # conversation. Deletion pitches are generated by a batch run with no
    # thread and no message, so they always landed here — and a few German
    # sentences anywhere in the household's chat history (any user, any
    # thread, any age) flipped EVERY pitch of that run into German, while a
    # Reevaluate on the same title minutes later came out English because it
    # passes its live message and resolves at rule 1. Same title, same day,
    # two languages, decided by unrelated chat.
    #
    # Following the user's language is a real feature, but it belongs to
    # surfaces that can see what language is being spoken to them. Batch
    # surfaces need a stable answer, and the UI they appear in is English.
    # A per-user locale setting is the proper home for the general case
    # (see ROADMAP) — until then this is deterministic rather than lucky.
    return "en"


def language_directive(code: str) -> str:
    """1-line prompt directive telling the LLM which language to answer in.

    Always returns a non-empty string so callers can blindly include it
    without conditional branches. The second sentence keeps technical
    fields (ratings, tags, identifiers) untranslated.
    """
    name = _LANGUAGE_NAMES.get(code, "English")
    return (
        f"LANGUAGE: Respond in {name} — but if the user writes in a different "
        f"language, follow THEIR language instead of this default; never argue "
        f"about language. This default mirrors the user's own recent messages — "
        f"if asked why the language changed, say exactly that; never invent a "
        f"motive for it. "
        f"Keep proper nouns, ratings, tags, and identifiers in their original form."
    )
