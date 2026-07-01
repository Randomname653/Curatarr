"""
Curatarr — Pillars: the curation court.

This module is the SINGLE SOURCE OF TRUTH for the 3-pillar deletion model:

  * PILLAR_CONSTITUTION  — the law (system prompt). The pillars live here as
    philosophy, NOT as Python thresholds. We do not hard-code "Metacritic >= 85";
    the model judges nuance against this constitution.
  * VERDICT_SCHEMA       — the strict JSON shape the model must emit (forced via
    Ollama's `format`), so it can never skip a pillar or hallucinate the verdict.
  * build_evidence()     — the CLERK ("Gerichtsschreiber"). It makes ZERO
    retention decisions. It assembles every relevant FACT into one block, plus a
    handful of cheap deterministic flags the judge would otherwise have to
    eyeball (partner engagement, bitrate outlier, acclaim present).

Division of labour (validated in tests/pillar_json_stresstest.py):
    Python  -> clean facts + the law (this file)
    LLM     -> the verdict, applying nuance the law leaves open

The judge call itself (constitution + evidence + schema -> verdict, then a
separate creative monologue) is the NEXT step — gated on the warm-GPU latency
test. It is intentionally NOT wired here yet.

Pillars (priority high -> low), enforced in the prompt:
    III HOUSEHOLD  — another household user genuinely engaged with it -> protect
    II  CUSTODIAN  — objective masterwork / rare work -> preserve despite taste
    I   EGO        — the owner's elite taste filter -> cut mediocrity
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


# ── THE LAW ───────────────────────────────────────────────────────────────────
# The three pillars, shared VERBATIM by the deletion judge (PILLAR_CONSTITUTION)
# and the chat / Level-2 discussion (PILLAR_FRAMEWORK) so the pitch and the talk
# reason from the SAME law — no more "pitch via pillars, discussion via taste +
# bloated bitrate". First draft, validated against curatarr-curator (100%
# schema-valid, correct verdict on Tokyo Story). Iterate on THIS text.
_PILLARS_BODY = """PILLAR III — HOUSEHOLD (highest). The server serves every household user, not only the owner. If the facts show ANOTHER user (not the owner) genuinely engaged with this title — watched it, above all completed it — it is protected for them no matter the owner's taste, and its bitrate is never questioned (household media is sacred). A title another user merely sampled and abandoned (e.g. 2 of 12 episodes, no rating) does NOT trigger this pillar.

PILLAR II — CUSTODIAN. The server is also an archive of film history. A title of genuine objective stature — a landmark or masterwork of its form, or a rare/obscure work at real risk of being lost — is preserved EVEN IF it clashes with the owner's taste. High critical acclaim (Rotten Tomatoes / Metacritic) and major awards are your evidence; use judgment, not a fixed number. Mere competence or popularity is not enough.

PILLAR I — EGO (lowest). For everything else — titles that exist only for the owner's own taste — apply the owner's elite, uncompromising profile: psychological friction, taboo-breaking, calculating "polite-monster" intelligence, stylistic brilliance. Lazy fan-service, sanitized kitsch, and mediocrity are CUT.

BITRATE is a SEPARATE axis from retention. A kept title may be FLAGGED if its file is a clear bitrate outlier for its visual complexity — but bitrate alone never deletes a title, and never touches a Pillar III title."""

PILLAR_CONSTITUTION = f"""You are the curation court for Curatarr, deciding whether ONE title stays on a shared 105 TB home server. Judge it against THREE pillars in STRICT priority — a higher pillar's protection can NEVER be overruled by a lower one. Base every word ONLY on the FACTS given; never invent data.

{_PILLARS_BODY}

VERDICTS:
- HARD_KEEP — protected by Pillar III; or a Pillar II case at sane bitrate; or a strong Pillar I taste-match.
- KEEP_WITH_FLAG — kept under Pillar II or I, but a clear bitrate outlier worth downscaling.
- CUT — no pillar protects it.
- EVALUATE — the facts are genuinely insufficient to decide.

Keep each pillar analysis to ONE or TWO sentences. Fill every field."""

# Discussion-framed version of the SAME pillars — injected into the chat /
# Level-2 deletion talk (routers/chat.py) so it reasons from pillars, with
# bitrate as a downscale-only note, instead of raw taste-mismatch + "bloated
# bitrate". Same _PILLARS_BODY, so the law can never drift between the two.
PILLAR_FRAMEWORK = f"""CURATION FRAMEWORK — judge this title's fate within Curatarr's three pillars, in STRICT priority (a higher pillar overrides a lower); reason ONLY from the FACTS, never invent:

{_PILLARS_BODY}

So: a household-claimed title or an objective masterwork STAYS even against the owner's taste; a clear bitrate outlier is a DOWNSCALE note, never a reason to delete. Argue from the specific facts of THIS title, in fresh words."""

# The structured Chain-of-Thought shape. Forcing all three pillar fields BEFORE
# the verdict is what stops the model tunnel-visioning on taste and ignoring
# acclaim (the Tokyo Story "the void" bug).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "pillar_3_household": {"type": "string"},
        "pillar_2_archive":   {"type": "string"},
        "pillar_1_ego":       {"type": "string"},
        "bitrate_note":       {"type": "string"},
        "verdict": {"type": "string",
                    "enum": ["HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "EVALUATE"]},
    },
    "required": ["pillar_3_household", "pillar_2_archive", "pillar_1_ego", "verdict"],
}


# ── CLERK HELPERS ─────────────────────────────────────────────────────────────

def _watch_filters(item: dict, category: str):
    """SQL conditions (to AND in .filter) matching a watch_history row to this
    item: a title/id match AND the right MEDIA FAMILY.

    The media-family guard is essential — without it a movie/show matched any
    same-named MUSIC row, and the owner's Spotify history is huge (≈350k rows):
    "American Pie" the film matched Don McLean's song (≈18 plays), "Blindspot"
    the series matched a STRLGHT track — both wrongly counted as "you watched
    this". Music rows are media_type='music'; everything else is video.

    Returns a list to AND in .filter(...), or None when there's nothing to
    match on. We constrain in SQL because the owner has hundreds of thousands
    of watch rows."""
    from src.database.models import WatchHistoryEntry
    from sqlalchemy import or_
    title = (item.get("title") or "").strip()
    tmdb_id = item.get("tmdb_id")
    title_conds = []
    if title:
        title_conds.append(WatchHistoryEntry.title == title)
        title_conds.append(WatchHistoryEntry.series_title == title)
    if tmdb_id:
        title_conds.append(WatchHistoryEntry.tmdb_id == tmdb_id)
    if not title_conds:
        return None
    media = (WatchHistoryEntry.media_type == "music" if category == "music"
             else WatchHistoryEntry.media_type != "music")
    return [or_(*title_conds), media]


def _owner_watch(db, owner_id: int, item: dict, category: str) -> dict | None:
    """The OWNER's own watch status -> {count, completed, last} or None."""
    from src.database.models import WatchHistoryEntry
    filters = _watch_filters(item, category)
    if not filters:
        return None
    rows = (db.query(WatchHistoryEntry.viewed_at, WatchHistoryEntry.completed)
              .filter(WatchHistoryEntry.user_id == owner_id, *filters).all())
    if not rows:
        return None
    return {
        "count": len(rows),
        "completed": any(bool(r.completed) for r in rows),
        "last": max((r.viewed_at for r in rows if r.viewed_at), default=None),
    }


def _other_users_watch(db, owner_id: int, item: dict, category: str) -> list[dict]:
    """PILLAR III signal: which OTHER household users engaged with this title.

    Returns one summary per non-owner user with any matching watch row. We hand
    the judge the RAW counts (distinct episodes, completion, recency) — it
    decides whether that is genuine engagement or a sampled-and-abandoned bounce.
    """
    from src.database.models import WatchHistoryEntry, User
    filters = _watch_filters(item, category)
    if not filters:
        return []
    rows = (db.query(WatchHistoryEntry)
              .filter(WatchHistoryEntry.user_id != owner_id, *filters).all())
    if not rows:
        return []
    names = {u.id: (u.plex_username or f"user {u.id}")
             for u in db.query(User.id, User.plex_username).all()}
    by_user: dict[int, list] = {}
    for r in rows:
        by_user.setdefault(r.user_id, []).append(r)
    out = []
    for uid, urows in by_user.items():
        eps = {(r.season, r.episode) for r in urows if r.episode is not None}
        out.append({
            "name": names.get(uid, f"user {uid}"),
            "views": len(urows),
            "distinct_episodes": len(eps),
            "completed": any(bool(r.completed) for r in urows),
            "last": max((r.viewed_at for r in urows if r.viewed_at), default=None),
        })
    return out


def _tech_facts(item: dict, media_type: str) -> tuple[str, bool]:
    """Raw tech line + bitrate-outlier flag. ('', False) when no profile on file."""
    try:
        from src.services.size_norms import tech_profile_for, size_outlier
    except Exception:
        return "", False
    prof = tech_profile_for(tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
                            plex_rating_key=item.get("plex_rating_key"))
    if not prof or not prof.get("mb_per_min"):
        return "", False
    res, codec, mbpm = prof.get("resolution"), prof.get("codec"), prof.get("mb_per_min")
    gb = (prof.get("size_mb") or 0) / 1024.0
    parts = [f"{res or '?'} {codec or '?'}", f"{gb:.1f} GB", f"{mbpm:.0f} MB/min"]
    outlier = False
    out = size_outlier(media_type, res, codec, mbpm)
    if out and out.get("verdict") and out.get("median"):
        parts.append(f"{out.get('ratio')}x class median ({out['verdict']})")
        outlier = (out["verdict"] == "bloated")
    return ", ".join(parts), outlier


# ── THE CLERK ─────────────────────────────────────────────────────────────────

async def build_evidence(item: dict, user_id: int, category: str, db) -> dict:
    """Assemble the full FACTS block + cheap flags for ONE title. Makes NO verdict.

    Returns ``{"title": str, "facts": str, "flags": dict}`` where ``facts`` is the
    prompt-ready evidence the judge consumes (same shape the constitution was
    tuned on) and ``flags`` are the few deterministic signals a thin guardrail or
    a sort could use without re-deriving them:
        owner_watched, other_user_engaged, bitrate_outlier, acclaim_present.
    """
    title = item.get("title") or "Unknown"
    year = item.get("year") or "—"
    genres = item.get("genres")
    genres_str = ", ".join(genres) if isinstance(genres, list) else (genres or "Unknown")
    media_type = item.get("media_type") or category

    flags = {"owner_watched": False, "other_user_engaged": False,
             "bitrate_outlier": False, "acclaim_present": False}

    # ── OWNER watch ──
    try:
        ow = _owner_watch(db, user_id, item, category)
    except Exception as e:
        logger.debug("[pillars] owner-watch failed for %r: %s", title, e)
        ow = None
    if ow:
        flags["owner_watched"] = True
        when = f", last {ow['last'].strftime('%b %Y')}" if ow.get("last") else ""
        owner_line = (f"watched {ow['count']}x{when}, "
                      + ("completed" if ow.get("completed") else "not completed"))
    else:
        owner_line = "not watched by the owner"

    # ── OTHER household users (PILLAR III) ──
    try:
        others = _other_users_watch(db, user_id, item, category)
    except Exception as e:
        logger.debug("[pillars] other-users failed for %r: %s", title, e)
        others = []
    if others:
        lines = []
        for o in others:
            flags["other_user_engaged"] = True
            when = f", last {o['last'].strftime('%b %Y')}" if o.get("last") else ""
            detail = (f"{o['distinct_episodes']} episode(s)"
                      if o["distinct_episodes"] else f"{o['views']} view(s)")
            lines.append(f"  - {o['name']}: {detail}{when}, "
                         + ("completed" if o["completed"] else "not completed"))
        other_block = "\n".join(lines)
    else:
        other_block = "  none have watched or requested it."

    # ── ACCLAIM / METADATA (verified data — cache-first, no LLM) ──
    verified_text = ""
    try:
        from src.services.media_enricher import ensure_verified_data, format_verified_block
        vd = await ensure_verified_data(
            title, category,
            tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
            anilist_id=item.get("anilist_id"), anidb_id=item.get("anidb_id"),
            plex_rating_key=item.get("plex_rating_key"),
        )
        verified_text = format_verified_block(vd) or ""
    except Exception as e:
        logger.debug("[pillars] verified-data failed for %r: %s", title, e)
    if verified_text:
        if any(m in verified_text for m in ("RT:", "METACRITIC", "Awards:", " wins", "Significance:")):
            flags["acclaim_present"] = True
        meta_block = verified_text
    else:
        ov = (item.get("overview") or "").strip()
        meta_block = f"no verified enrichment — thin synopsis only: {ov[:300] or 'n/a'}"

    # ── OWNER taste (category-scoped) ──
    taste = ""
    try:
        from src.database.models import TasteVectorEntry
        from src.services.recommendations_engine import _taste_section
        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        if tv and tv.summary_text:
            taste = _taste_section(tv.summary_text, category)
    except Exception as e:
        logger.debug("[pillars] taste failed for %r: %s", title, e)

    # ── TECH / bitrate axis ──
    try:
        tech_line, outlier = _tech_facts(item, media_type)
    except Exception as e:
        logger.debug("[pillars] tech failed for %r: %s", title, e)
        tech_line, outlier = "", False
    flags["bitrate_outlier"] = outlier

    facts = (
        f"TITLE: {title} ({year}) — {category}, {genres_str}\n"
        f"OWNER: {owner_line}.\n"
        f"OTHER HOUSEHOLD USERS:\n{other_block}\n"
        f"ACCLAIM & METADATA:\n{meta_block}\n"
        + (f"OWNER TASTE: {taste}\n" if taste else "")
        + (f"TECH: {tech_line}\n" if tech_line else "TECH: no technical profile on record.\n")
    )
    return {"title": title, "facts": facts.strip(), "flags": flags}


# ── THE JUDGE ─────────────────────────────────────────────────────────────────
# WRITTEN BUT NOT YET VALIDATED ON A WARM GPU. The schema/verdict path passed a
# smoke test (curatarr-curator, 100% valid, correct verdict); latency + the full
# matrix are pending a free GPU. The model for the verdict call (persona-baked
# curatarr-curator vs the clean base gemma4:31b) is settled by that matrix — for
# now we default to settings.CURATOR_MODEL, swappable via the `model` arg.

_JUDGE_TIMEOUT = 300.0   # s; a cold 31B load is slow — generous on purpose
_VALID_VERDICTS = {"HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "EVALUATE"}


async def adjudicate(evidence_facts: str, *, model: str = None,
                     skip_priority: bool = False) -> dict:
    """STAGE 1 — the structured verdict. Constitution + evidence + forced schema.

    temperature 0 (determinism by construction, not luck); the JSON shape is
    forced via Ollama `format`. Returns the parsed 5-field dict. On ANY error
    (network, malformed JSON, bad enum) returns a safe EVALUATE fallback — a
    flaky model response must never crash a library scan.
    """
    import httpx
    from src.config import settings
    from src.services.llm_priority import curator_priority
    from src.services.llm_utils import parse_llm_json

    model = model or settings.CURATOR_MODEL
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PILLAR_CONSTITUTION},
            {"role": "user", "content": "FACTS:\n" + evidence_facts},
        ],
        "format": VERDICT_SCHEMA,
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.0, "num_predict": 800,
                    "num_ctx": 8192, "num_gpu": 99},
    }
    async def _post():
        async with httpx.AsyncClient(timeout=_JUDGE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.effective_ollama}/api/chat", json=payload)
        resp.raise_for_status()
        return resp
    try:
        # skip_priority=True when an OUTER curator_start already holds the GPU
        # gate (the batch deletion loop) — re-acquiring it would deadlock.
        if skip_priority:
            r = await _post()
        else:
            async with curator_priority():
                r = await _post()
        content = (r.json().get("message") or {}).get("content", "") or ""
        data = parse_llm_json(content)
        if not isinstance(data, dict) or data.get("verdict") not in _VALID_VERDICTS:
            raise ValueError(f"bad verdict payload: {content[:200]!r}")
        return data
    except Exception as e:
        logger.warning("[pillars] adjudicate failed (%s) — defaulting to EVALUATE", e)
        return {"pillar_3_household": "", "pillar_2_archive": "", "pillar_1_ego": "",
                "bitrate_note": "", "verdict": "EVALUATE", "_error": str(e)}


def _lean_facts(facts: str) -> str:
    """Strip the two parrot/bloat sources before the prose pass: the OWNER TASTE
    blob (the model lifts its words verbatim → every pitch sounds identical) and
    the TECH line (a CUT shouldn't be padded with file-size). Keeps the title,
    watch status, and the ACCLAIM & METADATA block (themes / plot / significance)
    — the title's OWN specifics, which is exactly what we want it to argue from."""
    return "\n".join(
        ln for ln in facts.splitlines()
        if not ln.lstrip().startswith(("OWNER TASTE:", "TECH:"))
    )


def _governing(verdict: dict) -> str:
    """The pillar finding that DROVE the verdict — the seed the monologue expands
    (so the prose stays specific to this title instead of re-deriving a generic
    taste-mismatch)."""
    if verdict.get("verdict") in ("HARD_KEEP", "KEEP_WITH_FLAG"):
        return (verdict.get("pillar_2_archive") or verdict.get("pillar_3_household")
                or verdict.get("pillar_1_ego") or "")
    return verdict.get("pillar_1_ego") or verdict.get("pillar_3_household") or ""


# Per-verdict stance — frames the pitch WITHOUT making the model announce the
# verdict label ("CUT.") as the opener (the card already shows it).
_MONOLOGUE_STANCE = {
    "CUT": "This title does NOT earn its place — make the sharp case for removing it",
    "HARD_KEEP": "This title earns its place — make the sharp case for keeping it",
    "KEEP_WITH_FLAG": "This title earns its place for its stature, but its file is a "
                      "bitrate outlier — make the case for keeping it AND that it "
                      "should be downscaled to reclaim space",
    "EVALUATE": "Assess this title from what little is known",
}


async def write_monologue(evidence_facts: str, verdict: dict, *,
                          lang_directive: str = "", model: str = None,
                          skip_priority: bool = False) -> str:
    """STAGE 2 (LAZY) — the bissige user-facing prose. Persona model, higher
    temperature, NO schema. Call this ONLY for titles actually shown in the UI,
    so the expensive creative pass never runs on the whole candidate pool.

    Sends NO system message on purpose: the baked curatarr-curator persona
    drives the voice here, whereas adjudicate() overrides it with the neutral
    constitution. Same model, two framings."""
    import httpx
    from src.config import settings
    from src.services.llm_priority import curator_priority
    from src.services.llm_utils import clean_llm_text

    model = model or settings.CURATOR_MODEL
    v = verdict.get("verdict")
    # Bitrate is the DOWNSCALE axis — mentioned ONLY for KEEP_WITH_FLAG; a CUT
    # stands on the title, not file size.
    no_size = "" if v == "KEEP_WITH_FLAG" else "; do not mention file size or storage"
    flag = (f"\nBitrate (DO mention — it should be downscaled): "
            f"{verdict.get('bitrate_note', '')}" if v == "KEEP_WITH_FLAG" else "")
    lang_prefix = f"{lang_directive}\n\n" if lang_directive else ""
    # Characterize the title on ITS OWN terms; the pillar finding is INTERNAL
    # reasoning ("don't quote"); NEVER recite the user's tastes back at them — that
    # recitation ("incompatible with your appetite for…", "you demand…") was the
    # flat, repetitive boilerplate. _lean_facts strips the taste-blob + tech line.
    prompt = (
        lang_prefix
        + "You are the curator. In your uncompromising, opinionated voice, write the "
        "2-3 sentence note the user reads on this title's card. "
        f"{_MONOLOGUE_STANCE.get(v, 'Assess this title')}. Characterize what this title "
        "concretely IS — its premise, style, what it actually does — and let the "
        "verdict land on ITS own specifics, sharp and fresh. Do NOT recite the user's "
        "tastes back at them (they already know what they like); do NOT open with a "
        f"verdict label or the genre{no_size}.\n\n"
        f"{_lean_facts(evidence_facts)}\n\n"
        f"(For your reasoning only — do NOT quote this: {_governing(verdict)}){flag}\n\n"
        "Write the verdict. No headers, no lists."
    )
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False, "think": False, "keep_alive": "10m",
        "options": {"temperature": 0.7, "num_predict": 500, "num_gpu": 99},
    }

    async def _post():
        async with httpx.AsyncClient(timeout=_JUDGE_TIMEOUT) as client:
            resp = await client.post(
                f"{settings.effective_ollama}/api/chat", json=payload)
        resp.raise_for_status()
        return resp
    try:
        if skip_priority:
            r = await _post()
        else:
            async with curator_priority():
                r = await _post()
        return clean_llm_text((r.json().get("message") or {}).get("content", "") or "")
    except Exception as e:
        logger.warning("[pillars] monologue failed: %s", e)
        return ""


async def judge(item: dict, user_id: int, category: str, db, *,
                with_monologue: bool = False, lang_directive: str = "",
                skip_priority: bool = False) -> dict:
    """Full pipeline for ONE title: clerk -> verdict (-> optional monologue).

    Returns ``{title, facts, flags, verdict}`` (+ ``monologue`` when requested).
    The verdict dict carries the structured pillar CoT — log it to
    curator_resolution_log for an audit trail of WHY each title was kept/cut."""
    ev = await build_evidence(item, user_id, category, db)
    verdict = await adjudicate(ev["facts"], skip_priority=skip_priority)
    out = {**ev, "verdict": verdict}
    if with_monologue:
        out["monologue"] = await write_monologue(
            ev["facts"], verdict, lang_directive=lang_directive,
            skip_priority=skip_priority)
    return out
