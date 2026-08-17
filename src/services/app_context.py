"""
Curatarr — App Context: the single source of truth for what the curator knows
ABOUT THE APP IT LIVES IN — surfaces, buttons, badges, verdict classes.

Knowledge layering by change velocity (each layer has exactly ONE home):

    Persona   (the voice)         — baked in the Modelfile; changes over months
    Law       (pillars, verdicts) — src/services/pillars.py; injected per call
    App       (UI, features)      — THIS module; injected per surface
    Evidence  (title facts)       — per-request builders (build_evidence, chat)

Rules:
- Routers COMPOSE these blocks; prompt prose about the app never lives inline
  in a router again. (That's how the curator ended up sending the user to
  Sonarr to delete a title while the 'Delete & exit' button sat top right,
  and improvising when asked what the 'Stagnant' badge means.)
- SCOPED injection: each surface gets its own block, not the whole app manual.
- Every UI label referenced here must exist VERBATIM in frontend/index.html —
  enforced by tests/test_app_context_drift.py, so renaming a button without
  updating the prompt is a red test instead of a confident hallucination.
"""

# ── Deletion discussion (chat anchored on a deletion proposal) ────────────────

DISCUSSION_UI_BLOCK = (
    "UI: this discussion has a 'Delete & exit' button (top right) that "
    "executes the deletion right here in Curatarr, plus 'Exit discussion'. "
    "When the user decides to delete, point them to that button — never "
    "send them to Sonarr/Radarr/Lidarr to remove it manually.\n"
)

# The STAGNANT verdict class: gray zone — the OWNER decides. Without this the
# discussion doesn't know the verdict class exists and defaults to delete-mode.
STAGNANT_VERDICT_BLOCK = (
    "JUDGE VERDICT: STAGNANT — the pillar judge classed this title as "
    "merely 'fine' (gray zone, not a clear cut) and surfaced it with a "
    "'Stagnant' badge for the OWNER to decide. Weigh keep vs delete "
    "honestly on their terms instead of pushing for deletion.\n"
)

# ── Learned-principle review (chat anchored on a shadow principle) ───────────

PRINCIPLE_REVIEW_BLOCK = (
    "This is a PRINCIPLE REVIEW: you and the owner are deciding whether a rule "
    "you extracted from a past debate should govern future curation. Moderate "
    "honestly — lay out what adopting it changes, where it collides with the "
    "existing rules or taste profile shown above, and take a clear position, "
    "but the OWNER decides. When they settle it, confirm the outcome in plain "
    "words (adopt / reject / adopt with their refined wording); the decision "
    "is applied automatically after the conversation, and the "
    "'Learned principles' panel in the Deletions view remains the manual "
    "fallback.\n"
)

# ── General chat: the compact app map ("where do I find …?") ─────────────────

APP_MAP_BLOCK = (
    "APP MAP (Curatarr's own UI — point the user to the right place; never "
    "invent or deny capabilities):\n"
    "- Chat (you) · 'History & Taste' (watch history + taste profile) · "
    "'Recommendations' (your picks; a 'Search your library…' box for "
    "curated semantic search over what they own — understands "
    "'like <title> but …' queries and ranks by fit, falling back to "
    "plain similarity when the ranker is unavailable — and on Discovery "
    "cards an '+ Add' button (admin) that adds the title to the right "
    "arr).\n"
    "- 'Deletions' (admin): your deletion proposals, each with a 'Delete' "
    "button that EXECUTES the deletion from inside Curatarr via "
    "Sonarr/Radarr/Lidarr, plus Keep/Discuss; card checkboxes + a "
    "'Delete selected' button bulk-delete several at once.\n"
    "- 'Curation' (admin) panels: 'Judge-protected titles' (what the judge "
    "KEPT, with reasoning), 'Downscale candidates' (KEPT but flagged for a "
    "lower-bitrate transcode — NOT deletion candidates), "
    "'Upgrade candidates' (loved titles on weak files worth a better "
    "version), 'Redundant storage' (duplicate versions/copies and the GB "
    "they waste), and 'Learned principles' (rules you learned from "
    "debates).\n"
    "- 'Report' (admin): the curation report — monthly resolutions, GB "
    "freed, the Stubbornness Index, and your yearly review.\n"
    "- In their Plex: the 'Curatarr Recommended' playlists (per user; "
    "movies/shows/anime/music) and the rotating 'Curatarr · ' collection "
    "shelves you design weekly.\n"
    "- 'TV Shows' / 'Movies' / 'Music' (library browsers) · 'Reclassify' "
    "(admin) · 'Knowledge Base' (enrichment) · 'Activity' (live card for "
    "EVERY background job — syncs, maintenance, memory extraction, LLM "
    "runs — with progress and history) · "
    "'Libraries' / 'Users' (admin) · 'Settings'.\n"
)

# What the app can and cannot DO to the library — the chat's rule 5 builds on
# this. The old wording claimed "there is no ARR integration that lets you
# act", which predates the deletion flow and made the curator send the user to
# Sonarr to delete files by hand while the 🗑 buttons sat in the Deletions view.
LIBRARY_ACTIONS_BLOCK = (
    "LIBRARY ACTIONS: You never execute changes yourself from chat and never "
    "claim something was added, downloaded, queued, or removed. ADDING new "
    "media happens INSIDE Curatarr too: the '+ Add' button on Discovery "
    "cards or the library browsers' add flow (admin) — not by hand in the "
    "arr. DELETING existing media happens INSIDE Curatarr: the user "
    "approves a proposal in the Deletions view ('Delete', or bulk via "
    "'Delete selected') or uses 'Delete & exit' in a deletion discussion, "
    "and Curatarr executes it via the ARR — never send them to "
    "Sonarr/Radarr/Lidarr to delete files manually.\n"
)

# ── Drift-test registry ───────────────────────────────────────────────────────
# Every label the blocks above mention. tests/test_app_context_drift.py asserts
# each exists verbatim in frontend/index.html (and that none is orphaned here).
REFERENCED_UI_LABELS: tuple[str, ...] = (
    "Delete & exit",
    "Delete",
    "Exit discussion",
    "Stagnant",
    "Judge-protected titles",
    "Downscale candidates",
    "Upgrade candidates",
    "Redundant storage",
    "Learned principles",
    "History & Taste",
    "Recommendations",
    "Search your library…",
    "+ Add",
    "Delete selected",
    "Curation",
    "Report",
    "Deletions",
    "TV Shows",
    "Movies",
    "Music",
    "Reclassify",
    "Knowledge Base",
    "Activity",
    "Libraries",
    "Users",
    "Settings",
)
