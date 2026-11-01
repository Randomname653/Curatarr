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
    judge      PC                 raises PcUnavailable - deletion runs wait;
                                  NAS once the PC has not been free for
                                  JUDGE_FALLBACK_HOURS (judge_fallback)
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

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class OllamaHost:
    """One Ollama machine as the curator sees it."""
    url: str                 # base URL, no trailing slash
    profile: str             # "large" | "small": the judge's constitution, the window default
    num_ctx: int             # the curator window every curator call on this host pins
    gpu: dict = field(default_factory=dict)   # the curator's layer placement ({} = Ollama decides)
    # Runner options EVERY call to this host carries, for every model on it
    # ({"use_mmap": True} or {}). A runner option that differs between two
    # calls reloads the model, so it is decided per host, here, once.
    runner: dict = field(default_factory=dict)


def _mmap(key: str) -> dict:
    """{"use_mmap": bool} from the setting ``key``, or {} (Ollama decides).
    Measured on the NAS 2026-10-24: Ollama loaded the 8B summariser without
    mmap and kept a 4.5 GB copy in anonymous memory next to the one on the
    card; with use_mmap the weights are file pages the kernel can drop."""
    from src.config import settings
    v = getattr(settings, key, None)
    return {} if v is None else {"use_mmap": bool(v)}


class PcUnavailable(RuntimeError):
    """A role that waits for the PC (the deletion run) asked while the PC is
    off, asleep, shutting down or gaming, and not yet for long enough to
    fall back (judge_fallback). Callers defer."""


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
# deletion verdict from the small model costs more than a day's delay. Not
# more than weeks of no verdicts, though (the PC abroad, 2026-11-01): after
# JUDGE_FALLBACK_HOURS the NAS judges, and the PC re-judges those proposals
# once it is back (recommendations_engine._pool_plan).
PC_ONLY = frozenset({"judge"})
# Roles that never leave the main host.
MAIN_ONLY = frozenset({"embed"})


def main_host() -> OllamaHost:
    """OLLAMA_ENDPOINT with this process's profile, window and placement
    (the window resolved once at import - llm_utils.CURATOR_NUM_CTX)."""
    from src.config import settings
    from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu, llm_profile
    return OllamaHost(url=settings.effective_ollama, profile=llm_profile(),
                      num_ctx=CURATOR_NUM_CTX, gpu=curator_gpu(), runner=_mmap("OLLAMA_USE_MMAP"))


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
    return OllamaHost(url=url, profile=profile, num_ctx=ctx, gpu={} if n < 0 else {"num_gpu": n},
                      runner=_mmap("PC_OLLAMA_USE_MMAP"))


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
    if role in PC_ONLY and not judge_fallback():
        from src.services.pc_host import node_name
        raise PcUnavailable(f"'{role}' waits for {node_name()} (off, busy or shutting down)")
    return main_host()


def fallback_hours() -> float:
    """JUDGE_FALLBACK_HOURS: how long deletion runs wait for the PC
    (negative: always)."""
    from src.config import settings
    try:
        return float(getattr(settings, "JUDGE_FALLBACK_HOURS", 72))
    except (TypeError, ValueError):
        return 72.0


_FALLBACK_LOGGED = [False]


def judge_fallback() -> bool:
    """Split, the PC not free now, and not free for JUDGE_FALLBACK_HOURS:
    the deletion run judges on the NAS with the NAS's own profile."""
    if not split() or pc_usable():
        _FALLBACK_LOGGED[0] = False
        return False
    hours = fallback_hours()
    if hours < 0:
        return False
    from src.services import pc_host as pc
    away = time.time() - pc.last_free()
    on = away >= hours * 3600
    if on and not _FALLBACK_LOGGED[0]:
        _FALLBACK_LOGGED[0] = True
        logger.info(
            "[deletions] %s not free for %.0f h (JUDGE_FALLBACK_HOURS=%g): the server "
            "judges with its own model until it is back", pc.node_name(), away / 3600, hours)
    return on


def routed_host(role: str) -> Optional[OllamaHost]:
    """host_for(role), or None where it cannot answer (a PC-only role while
    the node is away, a half-configured install): for code that only builds
    request options and must never fail over routing."""
    try:
        return host_for(role)
    except Exception:  # noqa: BLE001 - PcUnavailable or no routing at all
        return None


def judge_ready() -> bool:
    """A deletion run can start now: not split, the PC is free, or it has
    been away long enough for the NAS to judge (judge_fallback)."""
    return not split() or pc_usable() or judge_fallback()


def ollama_for(role: str) -> str:
    """Base URL of the Ollama host for ``role`` (no trailing slash) - for
    calls that need only the address (summariser, embeddings)."""
    return host_for(role).url
