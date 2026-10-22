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

Code that MANAGES models rather than using them (the setup wizard, model
updates, the VRAM choreography, the game-mode unload) still reads the
endpoint directly; it gets a loop over the hosts with the second one.
tests/test_ollama_hosts.py keeps that list closed.
"""

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


def ollama_for(role: str) -> str:
    """Base URL of the Ollama host for ``role`` (no trailing slash)."""
    if role not in ROLES:
        raise ValueError(f"unknown Ollama role {role!r} (one of {', '.join(ROLES)})")
    from src.config import settings
    return settings.effective_ollama
