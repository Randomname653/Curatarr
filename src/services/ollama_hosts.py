"""Which Ollama host a piece of work goes to (2026-10-22, NAS move phase 2).

Curatarr is moving onto the always-on NAS, with a second Ollama host: the
node, whose 24 GB card runs the large bakes when it is on and nobody
plays. The bakes carry the same names on every host (build_models.py builds
them from a base that fits each card), so routing means picking a HOST, never
renaming a model.

Every model call asks ``ollama_for(role)`` / ``host_for(role)`` instead of
reading the endpoint itself. The role says what kind of work it is, which is
what the routing decides on:

    role       PC on and free     PC off, asleep, shutting down or gaming
    chat       PC                 OLLAMA_ENDPOINT (the NAS)
    curator    PC                 NAS
    judge      PC                 raises PcUnavailable - deletion runs wait
    enrich     NAS, always (ENRICH_ON_PC=true: PC when free, else NAS)
    embed      NAS, always - one host for every vector, so stored and new compare

Both cards work at once (2026-10-23): enrichment keeps the NAS's card busy
while chat, background curator work and the deletion run use the PC's.
llm_priority keeps one queue per host, so work waits only for work on the
same card.

"The PC" exists only with OLLAMA_PC_ENDPOINT set AND its agent reporting
(PC_AGENT_URL, services/pc_host): without the agent nobody can say the PC is
on or that nobody is playing, so it is never used blind. Without
OLLAMA_PC_ENDPOINT every role answers ``settings.effective_ollama`` with the
profile every call used before - each install that is not split.

A host is more than an address: its card decides the curator's context
window and which constitution the judge gets (``LLM_PROFILE``: large on the
24 GB PC, small on the 16 GB NAS). A call site that sends a CURATOR request
takes ``host_for(role)`` once and uses that host's url AND options
(``curator_options(host=...)``), so the window, the prompt variant and the
machine can never come from two different hosts. Summariser and embedding
calls need only the address: those bakes run the same window everywhere.

Code that MANAGES models rather than using them (the setup wizard, model
updates, the model check) still reads the endpoint directly.
tests/test_ollama_hosts.py keeps that list closed.
"""

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class OllamaHost:
    """One Ollama machine as the curator sees it."""
    url: str                 # base URL, no trailing slash
    profile: str             # "large" | "small": the judge's constitution, the window default
    num_ctx: int             # the curator window every curator call on this host pins
    gpu: dict = field(default_factory=dict)   # the curator's layer placement ({} = Ollama decides)


class PcUnavailable(RuntimeError):
    """A role that waits for the PC (the deletion run) asked while the PC is
    off, asleep, shutting down or gaming. Callers defer, they do not fall back."""


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
# Roles that wait for the PC instead of falling back to the NAS: a wrong
# deletion verdict from the small model costs more than a day's delay.
PC_ONLY = frozenset({"judge"})
# Roles that never leave the main host.
MAIN_ONLY = frozenset({"embed"})


def main_host() -> OllamaHost:
    """OLLAMA_ENDPOINT with this process's profile, window and placement
    (the window resolved once at import - llm_utils.CURATOR_NUM_CTX)."""
    from src.config import settings
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu, llm_profile
    return OllamaHost(url=settings.effective_ollama, profile=llm_profile(),
                      num_ctx=CURATOR_NUM_CTX, gpu=curator_gpu())


def pc_host() -> Optional[OllamaHost]:
    """The node's Ollama (OLLAMA_PC_ENDPOINT) with its own profile, or
    None when the install is not split."""
    from src.config import settings
    url = (getattr(settings, "OLLAMA_PC_ENDPOINT", "") or "").strip().rstrip("/")
    if not url:
        return None
    from src.services.llm_utils import _PROFILE_NUM_CTX
    profile = str(getattr(settings, "PC_LLM_PROFILE", "large") or "large").strip().lower()
    profile = profile if profile in _PROFILE_NUM_CTX else "large"
    try:
        ctx = int(getattr(settings, "PC_CURATOR_NUM_CTX", 0) or 0)
    except (TypeError, ValueError):
        ctx = 0
    ctx = max(4096, ctx) if ctx else _PROFILE_NUM_CTX[profile]
    try:
        n = int(getattr(settings, "PC_CURATOR_NUM_GPU", 99))
    except (TypeError, ValueError):
        n = 99
    return OllamaHost(url=url, profile=profile, num_ctx=ctx, gpu={} if n < 0 else {"num_gpu": n})


def split() -> bool:
    """Two hosts configured: the NAS and the PC."""
    return pc_host() is not None


def pc_usable() -> bool:
    """The PC may take work now: split, its agent answering, not shutting
    down, nobody playing and its card not held (services/pc_host)."""
    if not split():
        return False
    from src.services import pc_host as pc
    return pc.configured() and pc.available() and not pc.busy()


def _main_only(role: str) -> bool:
    if role in MAIN_ONLY:
        return True
    if role == "enrich":
        from src.config import settings
        return not getattr(settings, "ENRICH_ON_PC", False)
    return False


def host_for(role: str) -> OllamaHost:
    """The Ollama host for ``role`` now (see the table above). Raises
    PcUnavailable for a PC-only role while the PC cannot take it."""
    if role not in ROLES:
        raise ValueError(f"unknown Ollama role {role!r} (one of {', '.join(ROLES)})")
    pc = pc_host()
    if pc is None or _main_only(role):
        return main_host()
    if pc_usable():
        return pc
    if role in PC_ONLY:
        from src.services.pc_host import node_name
        raise PcUnavailable(f"'{role}' waits for {node_name()} (off, busy or shutting down)")
    return main_host()


def judge_ready() -> bool:
    """A deletion run can start now: not split, or the PC is free."""
    return not split() or pc_usable()


def ollama_for(role: str) -> str:
    """Base URL of the Ollama host for ``role`` (no trailing slash) - for
    calls that need only the address (summariser, embeddings)."""
    return host_for(role).url
