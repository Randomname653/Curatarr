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
  and improvising when asked what the 'Your call' badge means.)
- SCOPED injection: each surface gets its own block, not the whole app manual.
- Every UI label referenced here must exist VERBATIM in the frontend files (index.html + js/app.js) —
  enforced by tests/test_app_context_drift.py, so renaming a button without
  updating the prompt is a red test instead of a confident hallucination.
"""

# ── Deletion discussion (chat anchored on a deletion proposal) ────────────────

DISCUSSION_UI_TEMPLATE = (
    "UI: this discussion has a '⟦disc.delete_exit⟧' button (top right) that "
    "executes the deletion right here in Curatarr, plus '⟦disc.exit⟧'. "
    "When the user decides to delete, point them to that button — never "
    "send them to Sonarr/Radarr/Lidarr to remove it manually. The curator "
    "executes nothing itself: a declared intention to watch is acted on by "
    "the app AFTER the reply (protection; a watchlist add and a downscale "
    "flag are attempted) and the real outcome arrives as a notification. "
    "Never claim a deletion, purge, downscale or re-encode as done. "
    "Lyrics evidence (the 'Lyrics (n of m tracks on file)' line) is the "
    "artist's own words as read by the app: argue from it, name that basis, "
    "quote at most two short lines and never a verse; without the line, say "
    "nothing about the words.\n"
)

# The STAGNANT verdict class: gray zone — the OWNER decides. Without this the
# discussion doesn't know the verdict class exists and defaults to delete-mode.
STAGNANT_VERDICT_TEMPLATE = (
    "JUDGE VERDICT: STAGNANT — the pillar judge classed this title as "
    "merely 'fine' (gray zone, not a clear cut) and surfaced it with a "
    "'⟦del.your_call⟧' badge (every other proposal carries '⟦del.cut⟧') for the OWNER "
    "to decide. Each card's '⟦del.why⟧' lists the strongest signals in its "
    "deletion score. Weigh keep vs delete "
    "honestly on their terms instead of pushing for deletion.\n"
)

# ── Learned-principle review (chat anchored on a shadow principle) ───────────

PRINCIPLE_REVIEW_TEMPLATE = (
    "This is a PRINCIPLE REVIEW: you and the owner are deciding whether a rule "
    "you extracted from a past debate should govern future curation. Moderate "
    "honestly — lay out what adopting it changes, where it collides with the "
    "existing rules or taste profile shown above, and take a clear position, "
    "but the OWNER decides. When they settle it, confirm the outcome in plain "
    "words (adopt / reject / adopt with their refined wording); the decision "
    "is applied automatically after the conversation, and the "
    "'⟦cur.view.principles⟧' panel in the Curation view remains the manual "
    "fallback.\n"
)

# ── General chat: the compact app map ("where do I find …?") ─────────────────

APP_MAP_TEMPLATE = (
    "APP MAP (Curatarr's own UI — point the user to the right place; never "
    "invent or deny capabilities):\n"
    "- Chat (you) · '⟦nav.history⟧' (watch history + taste profile) · "
    "'⟦nav.recs⟧' (your picks; a '⟦recs.search⟧' box for "
    "curated semantic search over what they own — understands "
    "'like <title> but …' queries and ranks by fit, falling back to "
    "plain similarity when the ranker is unavailable — and on ⟦recs.lane_discovery|Discovery⟧ "
    "cards an '⟦recs.add⟧' button (admin) that adds the title to the right "
    "arr).\n"
    "- '⟦nav.swipe⟧': titles near the user's taste that they have not played in "
    "Plex, one card at a time. Not seen: '⟦swipe.react.notseen⟧' moves on, "
    "'⟦swipe.react.want⟧' also puts the title on their own Plex watchlist. Seen: "
    "⟦swipe.react.disliked⟧ / ⟦swipe.react.meh⟧ / ⟦swipe.react.liked⟧ / ⟦swipe.react.loved⟧, "
    "or exact 1-10 stars ('⟦swipe.exact_head|Seen it - how good⟧'). Seen-and-rated "
    "answers shape their taste profile - the way to "
    "teach it what they watched elsewhere (Netflix, cinema); "
    "'⟦swipe.react.want⟧' does not, it is a plan, not a verdict.\n"
    "- '⟦nav.deletions⟧' (admin): your deletion proposals, each marked '⟦del.cut⟧' or "
    "'⟦del.your_call⟧' with a '⟦del.why⟧' that lists the strongest signals in its "
    "deletion score, and each with a '⟦del.delete⟧' "
    "button that EXECUTES the deletion from inside Curatarr via "
    "Sonarr/Radarr/Lidarr, plus ⟦del.keep⟧/⟦del.discuss⟧. '⟦del.keep⟧' pauses proposals for "
    "that title for 90 days, after which it is judged again; ticking "
    "'⟦del.protect⟧' in the Keep dialog stops them for good. Card "
    "checkboxes + a "
    "'⟦del.view.delete_selected⟧' button bulk-delete several at once. If a card or "
    "pitch describes the WRONG same-named title, '⟦del.fix_match⟧' (behind the card's More menu) "
    "pins the correct entity — the pin survives rescans and the item "
    "re-enriches on it. '⟦del.ret.head⟧' (shown only when there are "
    "any) lists titles you deleted that are in the library again — an arr "
    "list, a request or SoulSync's playlist sync brought them back; a daily "
    "check finds them, and '⟦del.delete_again⟧' or ⟦del.keep⟧ closes each. Deleting an "
    "artist also blocks it in SoulSync so it is not downloaded again, and "
    "artists you follow in SoulSync are never proposed.\n"
    "- '⟦nav.curation⟧' (admin) panels: '⟦cur.view.protections⟧' (every title that is "
    "never proposed — judge keeps, chat protections and '⟦del.protect⟧' "
    "— with its reasoning, each liftable), '⟦cur.view.downscale⟧' "
    "(KEPT but flagged for a "
    "lower-bitrate transcode — NOT deletion candidates), "
    "'⟦cur.view.upgrades⟧' (loved titles on weak files worth a better "
    "version), '⟦cur.view.redundancy⟧' (duplicate versions/copies and the GB "
    "they waste), and '⟦cur.view.principles⟧' (rules you learned from "
    "debates).\n"
    "- 'Report' (admin): the curation report — monthly resolutions, GB "
    "freed, the Stubbornness Index, and your yearly review.\n"
    "- In their Plex: the 'Curatarr Recommended' playlists (per user, named in "
    "the user's language, e.g. 'Curatarr empfiehlt · Filme'; movies/shows/anime/music) "
    "and the rotating 'Curatarr · ' collection shelves you design weekly, shown on the "
    "home screens, the first one per library a season shelf.\n"
    "- '⟦nav.kb⟧' (admin) also carries a '⟦kb.backfill⟧' panel while the "
    "metadata behind your verdicts is still incomplete: one '⟦kb.fetch_now⟧' "
    "button per source (Wikipedia significance, community reception, "
    "on-record facts, OMDb ratings) with its coverage. It disappears on its "
    "own once a source is well covered, and the daily maintenance tick "
    "handles the remainder — so its absence means the data is there, not "
    "that the feature is missing.\n"
    "- '⟦nav.shows⟧' / '⟦nav.movies⟧' / '⟦nav.music⟧' (library browsers) · '⟦nav.reclassify⟧' "
    "(admin) · '⟦nav.kb⟧' (admin; enrichment) · '⟦nav.activity⟧' (admin; live "
    "card for EVERY background job — syncs, maintenance, memory extraction, "
    "LLM runs — with progress and history) · '⟦nav.settings⟧' ('⟦settings.account⟧', for every "
    "user, sets the tone you talk to them in — ⟦account.tone_friendly⟧, ⟦account.tone_neutral⟧ or ⟦account.tone_sharp⟧ — "
    "for chat, openers and messages; admin panes: '⟦settings.plex_libraries⟧' maps each Plex library to a category, '⟦settings.arr_apps⟧' holds the "
    "Sonarr/Radarr/Lidarr connections, '⟦settings.users⟧' includes the Spotify and "
    "Netflix history imports (a member's Netflix viewing counts like their "
    "Plex viewing), '⟦settings.media_server⟧' connects Jellyfin and moves the install "
    "from Plex to Jellyfin or back with all history and curation kept, "
    "'⟦settings.integrations⟧' holds the service connections, among them an optional "
    "Shoko server that gets every finished anime episode reported as "
    "watched; under its card, '⟦shoko.without_episode⟧' lists the series "
    "the sync could not place, to link to their AniDB entry or skip, "
    "'⟦settings.maintenance⟧' has sync and repair actions and '⟦set.view.nodes⟧"
    "': this server and the node that lends its card, with what each "
    "card is doing right now, which the owner can rename and update there).\n"
)

# ── Knowledge Base health: why the library is not at 100 % ───────────────────
# Injected next to the app map. The vocabulary is services/enrichment_state.py
# (STATE_DEFINITIONS / ATTENTION_REASONS); the numbers come from the same
# per-item classification the KB tile is counted from, so the curator can
# explain a percentage instead of guessing at it.
KB_HEALING_TEMPLATE = (
    "KNOWLEDGE BASE (enrichment health): every count in the Knowledge Base "
    "table opens the list behind it. States: enriched (incl. provisional), "
    "dead cache, partial (rule-based / awaiting LLM), retry due, not found "
    "(waiting on the backoff), queued / error, ignored, never processed. A "
    "title no source knows is retried two rounds in a row, then after 3, 6, "
    "12 and 24 days, then monthly — for ever; nothing is given up on, and an "
    "upstream outage never counts as 'not found'. The '⟦kb.view.attention⟧' tab of "
    "the Knowledge Base lists what the pipeline cannot settle alone: wrong year, "
    "unsure match, not found after two or more tries, and audit findings "
    "(wrong entity, shared id, pin contradicted). Admin actions there: "
    "'⟦kb.search_pin⟧' (the arr's own lookup plus TMDB/AniList candidates; "
    "'⟦picker.pin_this⟧' for a typed id; '⟦picker.reject⟧' excludes a wrong candidate "
    "for good), '⟦kb.retry_now⟧', '⟦kb.ignore⟧' / '⟦kb.unignore⟧' (an accepted gap leaves "
    "the open count), '⟦kb.dismiss_finding⟧'. When asked why the library is not at "
    "100 %, explain from these states — never call an item unfindable when it "
    "is merely waiting on the backoff.\n"
)

# What the app can and cannot DO to the library — the chat's rule 5 builds on
# this. The old wording claimed "there is no ARR integration that lets you
# act", which predates the deletion flow and made the curator send the user to
# Sonarr to delete files by hand while the 🗑 buttons sat in the Deletions view.
LIBRARY_ACTIONS_TEMPLATE = (
    "LIBRARY ACTIONS: You never execute changes yourself from chat and never "
    "claim something was added, downloaded, queued, or removed. ADDING new "
    "media happens INSIDE Curatarr too: the '⟦recs.add⟧' button on ⟦recs.lane_discovery|Discovery⟧ "
    "cards or the library browsers' add flow (admin) — not by hand in the "
    "arr. DELETING existing media happens INSIDE Curatarr: the user "
    "approves a proposal in the Deletions view ('⟦del.delete⟧', or bulk via "
    "'⟦del.view.delete_selected⟧') or uses '⟦disc.delete_exit⟧' in a deletion discussion, "
    "and Curatarr executes it via the ARR — never send them to "
    "Sonarr/Radarr/Lidarr to delete files manually.\n"
)

# ── Rendering: the UI's names in the user's language ──────────────────────────
# A UI name in a template is ⟦key⟧, a key of frontend/i18n/<lang>.json - the
# same dictionary the UI is drawn from (services/i18n), so the curator names
# the button the user sees, in the user's UI language (UX design record 3.8),
# and the two cannot drift apart. The prose around it stays English: it is
# the curator's prompt, not the user's text.
import re as _re  # noqa: E402

# ⟦key|words⟧ keeps ``words`` in English where the prose names a thing by
# its kind ("Discovery cards") rather than by its English label.
_MARK = _re.compile(r"⟦([\w.]+)(?:\|([^⟧]*))?⟧")


def render(template: str, lang: str = "en") -> str:
    """``template`` with every ⟦key⟧ as its UI name in ``lang``."""
    from src.services.i18n import t
    return _MARK.sub(lambda m: m.group(2) if (m.group(2) is not None and lang == "en") else t(m.group(1), lang),
                     template)


def keys_in(template: str) -> list:
    return [m.group(1) for m in _MARK.finditer(template)]


# The blocks in English, as every caller without a user's language reads them.
DISCUSSION_UI_BLOCK = render(DISCUSSION_UI_TEMPLATE)
STAGNANT_VERDICT_BLOCK = render(STAGNANT_VERDICT_TEMPLATE)
PRINCIPLE_REVIEW_BLOCK = render(PRINCIPLE_REVIEW_TEMPLATE)
APP_MAP_BLOCK = render(APP_MAP_TEMPLATE)
KB_HEALING_BLOCK = render(KB_HEALING_TEMPLATE)
LIBRARY_ACTIONS_BLOCK = render(LIBRARY_ACTIONS_TEMPLATE)


# ── Drift-test registry ───────────────────────────────────────────────────────
# The dictionary keys the templates name (⟦key⟧): tests/test_app_context_drift
# asserts each is in both dictionaries and drawn by the frontend.
REFERENCED_UI_KEYS: tuple[str, ...] = tuple(sorted({
    k for name, v in list(globals().items()) if name.endswith("_TEMPLATE") and isinstance(v, str)
    for k in keys_in(v)}))

# The labels still written verbatim: none has a UI key ('Report' names a
# view the UI no longer draws under that word in any language). Each must
# exist as is in the frontend files, and none be orphaned.
REFERENCED_UI_LABELS: tuple[str, ...] = (
    "Report",
)
