"""
Curatarr — why an LLM call failed, said precisely.

Every failure of the pillar judge used to end as the same EVALUATE fallback
and the same "the model did not answer" banner: Ollama down, a model that
could not be loaded next to a game, a prompt too long for the window, an
answer cut off by num_predict, JSON that did not parse, and a crash in our
own evidence code were indistinguishable. They need different responses —
an outage should stop a run, a malformed answer deserves one repair attempt,
a genuine EVALUATE is not a failure at all — so the kind travels with the
error.

``post_chat`` is the one non-streaming /api/chat call that knows these
kinds. It also reads what Ollama reports about the generation itself
(``done_reason``, ``prompt_eval_count``), which no call site looked at:
Ollama truncates an over-long prompt silently and an exhausted num_predict
just stops mid-JSON.
"""
from __future__ import annotations

import enum

import httpx


class LLMFailure(str, enum.Enum):
    # Infrastructure: nothing about this prompt — retrying the next title
    # will fail the same way.
    INFRA_OFFLINE = "infra_offline"        # connection refused / DNS / reset
    INFRA_LOAD = "infra_load"              # model could not be loaded (memory, runner start)
    INFRA_TIMEOUT = "infra_timeout"        # no answer within the read timeout
    INFRA_ERROR = "infra_error"            # any other 5xx
    MODEL_MISSING = "model_missing"        # 404: the tag is not installed
    LANE_CLOSED = "lane_closed"            # a game / another program holds the GPU
    # Payload: this prompt, this time.
    CONTEXT_OVERFLOW = "context_overflow"  # the prompt filled the num_ctx window
    REQUEST = "request"                    # 4xx: Ollama rejected the request itself
    # Answer: the model replied, but not usably.
    OUTPUT_TRUNCATED = "output_truncated"  # done_reason == "length"
    PARSE = "parse"                        # not JSON
    SCHEMA = "schema"                      # JSON of the wrong shape, or self-contradictory
    # Ours: the evidence for the prompt could not be assembled.
    EVIDENCE = "evidence"


INFRA = frozenset({LLMFailure.INFRA_OFFLINE, LLMFailure.INFRA_LOAD,
                   LLMFailure.INFRA_TIMEOUT, LLMFailure.INFRA_ERROR,
                   LLMFailure.MODEL_MISSING, LLMFailure.LANE_CLOSED})

# Worth exactly one corrective round trip: the model answered, and telling it
# what was wrong with the answer usually fixes it.
REPAIRABLE = frozenset({LLMFailure.PARSE, LLMFailure.SCHEMA,
                        LLMFailure.OUTPUT_TRUNCATED})

# Plain sentences for the Deletions banner and the Activity log.
DESCRIPTIONS = {
    LLMFailure.INFRA_OFFLINE: "Ollama is not answering",
    LLMFailure.INFRA_LOAD: "the model could not be loaded (not enough free memory on the graphics card)",
    LLMFailure.INFRA_TIMEOUT: "the model took too long to answer",
    LLMFailure.INFRA_ERROR: "Ollama reported an error",
    LLMFailure.MODEL_MISSING: "the model is not installed (run build_models.py)",
    LLMFailure.LANE_CLOSED: "another program took the graphics card",
    LLMFailure.CONTEXT_OVERFLOW: "the evidence did not fit the model's context window",
    LLMFailure.REQUEST: "Ollama rejected the request",
    LLMFailure.OUTPUT_TRUNCATED: "the answer was cut off before it was complete",
    LLMFailure.PARSE: "the model's answer was not readable",
    LLMFailure.SCHEMA: "the model's verdict was incomplete or contradicted itself",
    LLMFailure.EVIDENCE: "the facts for the title could not be assembled",
}


class LLMCallError(Exception):
    """A failed call, with its kind and — when the model did answer — the raw
    answer, so a repair prompt can show the model what it said."""

    def __init__(self, kind: LLMFailure, detail: str = "", raw: str = ""):
        super().__init__(f"{kind.value}: {detail}" if detail else kind.value)
        self.kind = kind
        self.detail = detail
        self.raw = raw


# Ollama's wording when a model cannot come up — measured on the owner's box
# ("timed out waiting for llama-server to start" under a 17.5 GB image job)
# and from the server source (insufficient memory, runner terminated, CUDA).
_LOAD_MARKERS = ("memory", "llama-server", "llama runner", "runner process",
                 "cuda", "out of memory", "unable to allocate", "error loading model")
_CONTEXT_MARKERS = ("context length", "context window", "exceeds the context",
                    "input length")


def classify_status(status: int, body: str) -> LLMFailure:
    """The failure kind for a non-2xx Ollama response."""
    b = (body or "").lower()
    if any(m in b for m in _CONTEXT_MARKERS):
        return LLMFailure.CONTEXT_OVERFLOW
    if status == 404:
        return LLMFailure.MODEL_MISSING
    if any(m in b for m in _LOAD_MARKERS):
        return LLMFailure.INFRA_LOAD
    if status >= 500:
        return LLMFailure.INFRA_ERROR
    return LLMFailure.REQUEST


def check_generation(data: dict, options: dict) -> None:
    """Raise when Ollama's own accounting says the answer is not whole.

    ``prompt_eval_count`` counts only the tokens Ollama had to evaluate — a
    prefix reused from its prompt cache is not in it — so this catches an
    overflow on the first call of a run and in the evidence part of later
    ones, not every case. The pre-flight estimate in prompt_budget is the
    primary guard; this is the confirmation Ollama gives us for free."""
    num_ctx = int(options.get("num_ctx") or 0)
    num_predict = int(options.get("num_predict") or 0)
    evaluated = int(data.get("prompt_eval_count") or 0)
    if num_ctx and evaluated and evaluated >= num_ctx - num_predict:
        raise LLMCallError(
            LLMFailure.CONTEXT_OVERFLOW,
            f"{evaluated} prompt tokens left no room for {num_predict} in a "
            f"{num_ctx}-token window")
    if data.get("done_reason") == "length":
        raise LLMCallError(LLMFailure.OUTPUT_TRUNCATED,
                           f"num_predict={num_predict} exhausted",
                           (data.get("message") or {}).get("content", "") or "")


def timeout_for(read: float) -> httpx.Timeout:
    """Connect fails fast; only the read may take long. A single scalar used
    to give a blackholed host the full 300 s just to refuse the connection."""
    return httpx.Timeout(connect=10.0, read=read, write=30.0, pool=10.0)


async def post_chat(base_url: str, payload: dict, *, read_timeout: float) -> dict:
    """POST a non-streaming /api/chat and return the parsed response, or
    raise LLMCallError with the kind of failure."""
    try:
        async with httpx.AsyncClient(timeout=timeout_for(read_timeout)) as client:
            resp = await client.post(f"{base_url}/api/chat", json=payload)
    except httpx.TimeoutException as e:
        # ConnectTimeout is a TimeoutException too, but it means the host is
        # unreachable rather than slow.
        kind = (LLMFailure.INFRA_OFFLINE if isinstance(e, httpx.ConnectTimeout)
                else LLMFailure.INFRA_TIMEOUT)
        raise LLMCallError(kind, type(e).__name__) from e
    except httpx.TransportError as e:
        raise LLMCallError(LLMFailure.INFRA_OFFLINE, f"{type(e).__name__}: {e}") from e
    if resp.status_code >= 400:
        body = resp.text[:400]
        raise LLMCallError(classify_status(resp.status_code, body),
                           f"HTTP {resp.status_code}: {body}")
    try:
        data = resp.json()
    except ValueError as e:
        raise LLMCallError(LLMFailure.INFRA_ERROR, "Ollama sent a non-JSON response") from e
    check_generation(data, payload.get("options") or {})
    return data
