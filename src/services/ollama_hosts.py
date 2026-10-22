"""Which Ollama host a piece of work goes to (2026-10-22, NAS move phase 2).

Curatarr is moving onto the always-on NAS, with a second Ollama host: the
gaming PC, whose 24 GB card runs the large bakes when it is on and nobody
plays. The bakes carry the same names on every host (build_models.py builds
them from a base that fits each card), so routing means picking a HOST, never
renaming a model.

Every model call asks ``ollama_for(role)`` instead of reading the endpoint
itself. The role says what kind of work it is, which is what the routing
policy decides on: a chat turn may fall back to the NAS, a deletion verdict
waits for the PC, every embedding comes from one host so stored and new
vectors stay comparable.

With one endpoint configured - every install today - each role answers
``settings.effective_ollama``, exactly what the call sites read before.

A host is more than an address: its card decides the curator's context
window and which constitution the judge gets (``LLM_PROFILE``: large on the
24 GB PC, small on the 16 GB NAS). A call site that sends a CURATOR request
takes ``host_for(role)`` once and uses that host's url AND options
(``curator_options(host=...)``), so the window, the prompt variant and the
machine can never come from two different hosts. Summariser and embedding
calls need only the address: those bakes run the same window everywhere.

Code that MANAGES models rather than using them (the setup wizard, model
updates, the VRAM choreography, the game-mode unload) still reads the
endpoint directly; it gets a loop over the hosts with the second one.
tests/test_ollama_hosts.py keeps that list closed.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class OllamaHost:
    """One Ollama machine as the curator sees it."""
    url: str                 # base URL, no trailing slash
    profile: str             # "large" | "small": the judge's constitution, the window default
    num_ctx: int             # the curator window every curator call on this host pins
    gpu: dict = field(default_factory=dict)   # the curator's layer placement ({} = Ollama decides)


ROLES = {
    "chat": "a user is waiting: chat replies, title extraction, the warm-up, "
            "the protection scan right after a reply",
    "curator": "background work on the curator bake: taste summaries, proactive "
               "messages, chat starters, principles, recommendation chains",
    "judge": "the deletion run: judge verdicts and pitches (pitcher or curator bake)",
    "enrich": "the summariser bake: enrichment, lyrics, reception, memory extraction "
              "and analysis, verification, search parsing",
    "embed": "embeddings: one host for every vector, so stored and new ones compare",
}


def host_for(role: str) -> OllamaHost:
    """The Ollama host for ``role``. One endpoint today: the configured one,
    with the profile, window and placement every curator call used before
    (the window resolved once at import - llm_utils.CURATOR_NUM_CTX)."""
    if role not in ROLES:
        raise ValueError(f"unknown Ollama role {role!r} (one of {', '.join(ROLES)})")
    from src.config import settings
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu, llm_profile
    return OllamaHost(url=settings.effective_ollama, profile=llm_profile(),
                      num_ctx=CURATOR_NUM_CTX, gpu=curator_gpu())


def ollama_for(role: str) -> str:
    """Base URL of the Ollama host for ``role`` (no trailing slash) - for
    calls that need only the address (summariser, embeddings)."""
    return host_for(role).url
