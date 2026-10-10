"""
Curatarr — fitting a prompt into the context window on purpose.

Ollama does not refuse an over-long prompt: it truncates it silently, from
the front, and the system prompt is what goes first (chat.py measured it).
So the prompt has to fit BEFORE it is sent, and when it does not, we choose
what goes — the least valuable context, in a stated order — instead of
Ollama choosing the most valuable.

Token counts are estimates. The models' own tokenisers are not reachable
from here, and ~4 chars/token (the old watchdog's figure) undercounts German,
CJK and punctuation-dense metadata; 3.2 errs toward fitting. Ollama's
``prompt_eval_count`` (llm_errors.check_generation) is the after-the-fact
check of the same number.
"""
from __future__ import annotations

from dataclasses import dataclass

CHARS_PER_TOKEN = 3.2
# Chat-template wrapping per message (role headers, turn markers).
_TEMPLATE_TOKENS = 64

_FENCE_OPEN = "<<<UNTRUSTED_SOURCE:"
_FENCE_CLOSE = "<<<END_UNTRUSTED_SOURCE>>>"
_TRIM_MARK = " …[trimmed]"


def est_tokens(text: str) -> int:
    return int(len(text or "") / CHARS_PER_TOKEN) + 1


def budget_for(num_ctx: int, num_predict: int, system_text: str = "") -> int:
    """Tokens left for the variable part of a prompt once the answer's room
    and the fixed system text are paid for."""
    return num_ctx - num_predict - est_tokens(system_text) - _TEMPLATE_TOKENS


@dataclass
class Section:
    """One block of a prompt. ``priority`` 0 is never touched (the title, who
    watched it, what the owner said); higher numbers go first. ``min_chars``
    is how far a big block may be trimmed before it is dropped outright; a
    section without one (a single line: tech, dialogue, a signal) is kept
    whole or dropped whole — half a line is noise."""
    key: str
    text: str
    priority: int
    min_chars: int = 0


def _close_fences(text: str) -> str:
    """A trimmed fenced block must still end with its END marker, or the rest
    of the prompt reads as untrusted data — or worse, the data as prompt."""
    missing = text.count(_FENCE_OPEN) - text.count(_FENCE_CLOSE)
    return text + ("\n" + _FENCE_CLOSE) * max(0, missing)


def _trim(text: str, max_chars: int) -> str:
    """Cut to at most ``max_chars`` INCLUDING what the cut adds back — the
    visible mark, a re-closed fence, the line terminator — at a word
    boundary. Shorter than its own overhead, the section is dropped."""
    if len(text) <= max_chars:
        return text
    # Sections are newline-terminated lines and blocks; keep the terminator
    # so the next section's label still starts a line.
    nl = "\n" if text.endswith("\n") else ""
    overhead = len(_TRIM_MARK) + len(nl) + (len(_FENCE_CLOSE) + 1) * text.count(_FENCE_OPEN)
    room = max_chars - overhead
    if room <= 0:
        return ""
    cut = text[:room]
    cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
    # A cut that landed inside a marker would leave half a marker behind.
    for marker in (_FENCE_OPEN, _FENCE_CLOSE):
        for i in range(1, len(marker)):
            if cut.endswith(marker[:i]):
                cut = cut[:-i]
                break
    return _close_fences(cut.rstrip() + _TRIM_MARK) + nl


def fit(sections: list[Section], budget_tokens: int) -> tuple[str, list[str]]:
    """Join ``sections`` in their given order, trimming or dropping the
    least important ones until the estimate fits ``budget_tokens``.

    Returns (text, keys trimmed or dropped). Priority-0 sections are never
    touched; if they alone exceed the budget the result is over budget, and
    the caller's pre-flight check (or Ollama's own count) reports it."""
    kept = {s.key: s.text for s in sections}
    touched: list[str] = []
    expendable = sorted((s for s in sections if s.priority > 0),
                        key=lambda s: -s.priority)

    def total() -> int:
        # The joined text, as the caller will measure it — a sum of
        # per-section estimates overcounts one rounding per section.
        return est_tokens("".join(kept.values()))

    def over_chars() -> int:
        return int((total() - budget_tokens) * CHARS_PER_TOKEN) + 1

    # Pass 1, least important first: drop single lines, trim blocks down to
    # their floor.
    for s in expendable:
        if total() <= budget_tokens:
            break
        current = kept[s.key]
        if not current:
            continue
        if not s.min_chars:
            kept[s.key] = ""
            touched.append(s.key)
            continue
        target = max(len(current) - over_chars(), s.min_chars)
        if target >= len(current):
            continue
        kept[s.key] = _trim(current, target)
        touched.append(s.key)
    # Pass 2: still over — drop whole sections, floors included.
    for s in expendable:
        if total() <= budget_tokens:
            break
        if kept[s.key]:
            kept[s.key] = ""
            if s.key not in touched:
                touched.append(s.key)
    return "".join(kept[s.key] for s in sections if kept[s.key]), touched


def fit_messages(messages: list[dict], budget_tokens: int) -> tuple[list[dict], int]:
    """Drop the OLDEST history turns until a chat fits.

    System messages and the final message (the user's current turn) always
    stay, and nothing is reordered. Turns go oldest-first; an answer whose
    question was just dropped goes with it, so the model never reads a reply
    to nothing. Returns (messages, dropped)."""
    def total(ms):
        return sum(est_tokens(m.get("content") or "") + 4 for m in ms)

    out = list(messages)
    dropped = 0
    while total(out) > budget_tokens:
        # The earliest droppable turn: not a system message, not the last.
        idx = next((i for i, m in enumerate(out[:-1]) if m.get("role") != "system"), None)
        if idx is None:
            break
        out.pop(idx)
        dropped += 1
        if (idx < len(out) - 1 and out[idx].get("role") == "assistant"):
            out.pop(idx)
            dropped += 1
    return out, dropped


def keep_tail(text: str, budget_tokens: int, marker: str = "[earlier turns omitted]\n\n") -> str:
    """The newest part of a transcript that fits: what was said last is what
    a debate settled on."""
    max_chars = int(budget_tokens * CHARS_PER_TOKEN)
    if len(text) <= max_chars:
        return text
    tail = text[-(max_chars - len(marker)):]
    # Start at a turn boundary rather than mid-sentence when one is near.
    cut = tail.find("\n\n")
    if 0 <= cut < len(tail) // 4:
        tail = tail[cut + 2:]
    return marker + tail
