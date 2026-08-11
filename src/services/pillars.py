"""
Curatarr — Pillars: the curation court.

This module is the SINGLE SOURCE OF TRUTH for the 4-pillar deletion model:

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
    III HOUSEHOLD  — another household user engaged with it -> protect its existence
    II  CUSTODIAN  — objective masterwork / rare work -> preserve despite taste
    I   RESONANCE  — sublime/meditative work that passes the Intent/Awe/Rigor litmus
    0   EGO        — the owner's elite edge; must ACTIVELY stimulate or it is cut
"""
from __future__ import annotations

import logging

from src.services.llm_utils import CURATOR_NUM_CTX

logger = logging.getLogger(__name__)


# ── THE LAW ───────────────────────────────────────────────────────────────────
# The FOUR pillars (the RESONANCE reframe Curatarr designed itself), shared
# VERBATIM by the deletion judge (PILLAR_CONSTITUTION) and the chat / Level-2
# discussion (PILLAR_FRAMEWORK) so the pitch and the talk reason from the SAME
# law. Validated prototype-first (tests/resonance_proto.py) against
# curatarr-curator: correct verdicts on 7 edge titles incl. the Resonance-litmus
# discriminator (Tokyo Story passes / America's National Parks fails), the Ego
# execution-guard (Butterfly Effect → CUT), Quality-Floor downscale, and STAGNANT.
_PILLARS_BODY = """PILLAR III — HOUSEHOLD (highest, Sacred). If the facts show ANOTHER household user (not the owner) genuinely engaged with — above all COMPLETED — this title, it is protected for them regardless of the owner's taste. QUALITY FLOOR: this protects the title's EXISTENCE, not its fidelity — if such a household title is objectively mediocre, KEEP it but flag it for downscaling (don't hoard 4K space on mediocrity someone merely watched). A title another user only sampled and abandoned (e.g. 2 of 12 episodes, no rating) does NOT trigger this pillar.

PILLAR II — CUSTODIAN (Archive). A title of genuine OBJECTIVE stature — a landmark or masterwork of its form, or a rare work at real risk of being lost — is preserved even against the owner's taste. High critical acclaim (Rotten Tomatoes / Metacritic) and major awards are your evidence; use judgment, not a fixed number. Mere competence, popularity, or being a "precursor / foundational to a style" is NOT objective stature. When the FACTS document significance (awards, milestones, documented cultural impact), your verdict MUST engage it explicitly — concede it or argue it down; silently omitting it is a broken verdict.

PILLAR I — RESONANCE (Expansion). This protects the QUIET intellect — sublime observation, meditative depth, patient exploration: works that hum rather than scream, offering awe and a mental reset rather than adrenaline. BUT to keep this from becoming a backdoor for boredom, a slow / low-friction title must PASS a 3-part LITMUS or it is Generic Filler:
  1. INTENT — Observation, not Tourism: does it capture the essence/weight of its subject and invite contemplation, rather than treat it as a pleasant checklist of attractions?
  2. AWE, not Comfort: does it evoke awe (fear + respect + wonder; feeling small in a stimulating way), rather than mere soothing comfort / the absence of tension?
  3. RIGOR — Mastery, not Competence: is its slowness intentional and masterful (pacing, craft, insight), rather than a generic formula any studio could produce?
A title that FAILS the litmus is filler and drops to Pillar 0.

PILLAR 0 — EGO (lowest, the Edge). The owner's elite, uncompromising taste: psychological friction, calculating "polite-monster" intelligence, taboo-breaking, stylistic/kinetic brilliance. OFFENSIVE, not defensive: a title must ACTIVELY provide intellectual or stylistic stimulation to survive here — not merely "not be bad". Beware PREMISE vs EXECUTION: a work whose premise CLAIMS depth (dark themes, high-concept) but whose EXECUTION is populist, manipulative, or generic does NOT pass — darkness is not depth. Lazy fan-service, sanitized kitsch, and crass novelty are CUT.

BITRATE is a SEPARATE axis from retention: a kept title that is a clear bitrate outlier may be flagged for downscaling; bitrate alone never deletes."""

PILLAR_CONSTITUTION = f"""You are the curation court for Curatarr, deciding whether ONE title stays on a shared 105 TB home server. Judge it against FOUR pillars in STRICT priority — a higher pillar's protection can NEVER be overruled by a lower one. Base every word ONLY on the FACTS given; never invent data. Default to demanding EXCELLENCE: a title EARNS its place; it is never kept merely for "not being bad".

{_PILLARS_BODY}

VERDICTS:
- HARD_KEEP — protected by III (sacred) / II (masterwork) / I (passes the Resonance litmus) at sane bitrate, or a strong Pillar-0 Edge match.
- KEEP_WITH_FLAG — kept, but a clear bitrate outlier worth downscaling (includes the Household Quality Floor).
- CUT — no pillar protects it: fails the Ego edge, fails the Resonance litmus, no stature, no household claim.
- STAGNANT — the gray zone: not bad enough to cut, but merely "fine" — it neither champions the owner's edge nor passes the Resonance litmus. Queue for the owner's review instead of silently keeping it.
- EVALUATE — the facts are genuinely insufficient to decide.

Set protecting_pillar to the HIGHEST pillar that actually protects this title (HOUSEHOLD / CUSTODIAN / RESONANCE / EGO), or NONE for a CUT / STAGNANT / EVALUATE title.

Any OWNER SIGNAL lines are things the owner told me before that may bear on this title — weigh them honestly. They never force a KEEP on their own, but a GENUINELY applicable owner signal pulling toward a title you would otherwise CUT should downgrade that verdict to STAGNANT (surface it for the owner's call) rather than silently discard something they value. A signal that does not actually fit this title is ignored.

Keep each pillar analysis to ONE or TWO sentences. Fill every field."""

# Discussion-framed version of the SAME pillars — injected into the chat /
# Level-2 deletion talk (routers/chat.py) so it reasons from pillars, with
# bitrate as a downscale-only note, instead of raw taste-mismatch + "bloated
# bitrate". Same _PILLARS_BODY, so the law can never drift between the two.
PILLAR_FRAMEWORK = f"""CURATION FRAMEWORK — judge this title's fate within Curatarr's four pillars, in STRICT priority (a higher pillar overrides a lower); reason ONLY from the FACTS, never invent, and default to demanding excellence (a title EARNS its place):

{_PILLARS_BODY}

So: a household-claimed title or an objective masterwork STAYS even against the owner's taste; a quiet / meditative title stays only if it passes the Resonance litmus (Intent, Awe, Rigor); a clear bitrate outlier is a DOWNSCALE note, never a reason to delete. Argue from the specific facts of THIS title, in fresh words."""

# The structured Chain-of-Thought shape. Forcing all FOUR pillar fields BEFORE
# the verdict is what stops the model tunnel-visioning on taste and ignoring
# acclaim (the Tokyo Story "the void" bug). protecting_pillar names the highest
# pillar that keeps it — the persist-logic protects only III/II/I keeps, never a
# bare Ego(0) taste-match (the over-protection fix).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "pillar_3_household": {"type": "string"},
        "pillar_2_custodian": {"type": "string"},
        "pillar_1_resonance": {"type": "string"},
        "pillar_0_ego":       {"type": "string"},
        "bitrate_note":       {"type": "string"},
        "protecting_pillar":  {"type": "string",
                               "enum": ["HOUSEHOLD", "CUSTODIAN", "RESONANCE", "EGO", "NONE"]},
        "verdict": {"type": "string",
                    "enum": ["HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "STAGNANT", "EVALUATE"]},
    },
    "required": ["pillar_3_household", "pillar_2_custodian", "pillar_1_resonance",
                 "pillar_0_ego", "protecting_pillar", "verdict"],
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
    from sqlalchemy import func, or_
    title = (item.get("title") or "").strip()
    tmdb_id = item.get("tmdb_id")
    title_conds = []
    if title:
        title_conds.append(WatchHistoryEntry.title == title)
        title_conds.append(WatchHistoryEntry.series_title == title)
        if category == "music":
            # MusicBrainz artist names carry typographic dashes (U+2010,
            # "Mike WiLL Made‐It") while the history has ASCII "-" — the
            # exact match above sees NOTHING for such artists.
            from src.services.watch_status import _artist_variants
            title_conds.append(
                func.lower(WatchHistoryEntry.series_title).in_(_artist_variants(title)))
    if category == "music" and (item.get("artist_mbid") or item.get("mbid")):
        title_conds.append(WatchHistoryEntry.artist_mbid ==
                           (item.get("artist_mbid") or item.get("mbid")))
    if tmdb_id:
        title_conds.append(WatchHistoryEntry.tmdb_id == tmdb_id)
    if not title_conds:
        return None
    media = (WatchHistoryEntry.media_type == "music" if category == "music"
             else WatchHistoryEntry.media_type != "music")
    return [or_(*title_conds), media]


def _owner_watch(db, owner_id: int, item: dict, category: str) -> dict | None:
    """The OWNER's own watch status -> {count, completed, last, episodes}
    or None. ``episodes`` counts DISTINCT (season, episode) — 9 rows are 9
    episode plays, not 9 series rewatches."""
    from src.database.models import WatchHistoryEntry
    filters = _watch_filters(item, category)
    if not filters:
        return None
    rows = (db.query(WatchHistoryEntry.viewed_at, WatchHistoryEntry.completed,
                     WatchHistoryEntry.season, WatchHistoryEntry.episode)
              .filter(WatchHistoryEntry.user_id == owner_id, *filters).all())
    if not rows:
        return None
    return {
        "count": len(rows),
        "completed": any(bool(r.completed) for r in rows),
        "last": max((r.viewed_at for r in rows if r.viewed_at), default=None),
        "episodes": len({(r.season, r.episode) for r in rows
                         if r.episode is not None}),
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
    remux = prof.get("is_remux", False)
    gb = (prof.get("size_mb") or 0) / 1024.0
    parts = [f"{res or '?'} {codec or '?'}{' remux' if remux else ''}",
             f"{gb:.1f} GB", f"{mbpm:.0f} MB/min"]
    outlier = False
    out = size_outlier(media_type, res, codec, mbpm, is_remux=remux)
    if out and out.get("verdict") and out.get("median"):
        klass = "remux-class" if remux else "class"
        parts.append(f"{out.get('ratio')}x {klass} median ({out['verdict']})")
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
             "bitrate_outlier": False, "acclaim_present": False, "owner_signal": False}

    # ── OWNER watch ──
    try:
        ow = _owner_watch(db, user_id, item, category)
    except Exception as e:
        logger.debug("[pillars] owner-watch failed for %r: %s", title, e)
        ow = None
    if ow:
        flags["owner_watched"] = True
        when = f", last {ow['last'].strftime('%b %Y')}" if ow.get("last") else ""
        if (ow.get("episodes") or 0) >= 2:
            owner_line = (f"{ow['episodes']} episodes played"
                          + (f" ({ow['count']} plays)" if ow["count"] > ow["episodes"] else "")
                          + when)
        else:
            owner_line = (f"watched {ow['count']}x{when}, "
                          + ("completed" if ow.get("completed") else "not completed"))
    else:
        owner_line = "not watched by the owner"
    if ow and (ow.get("episodes") or 0) >= 2 and category in ("show", "anime"):
        # the SIGNALS behind the count: in order vs scattered, stop point,
        # abandon position, entry-loop rewatches, binge vs slow drip
        try:
            from src.services.watch_status import viewing_pattern
            vp = viewing_pattern(user_id, title, category=category)
            if vp:
                owner_line += f". Pattern: {vp}"
        except Exception as e:
            logger.debug("[pillars] viewing pattern failed for %r: %s", title, e)
    if category in ("show", "anime"):
        # stock truth for the judge: episodes known/aired/monitored/on disk,
        # incl. whether any monitored+aired episode is missing
        try:
            from src.services.episode_context import series_availability
            av = await series_availability(title)
            if av:
                owner_line += f". Availability: {av}"
        except Exception as e:
            logger.debug("[pillars] availability failed for %r: %s", title, e)
    if category == "music":
        # play-count DEPTH for artists — "watched 10931x" says less than
        # "10931 plays across 257 tracks, top: …" (and honest silence:
        # 1 play in 2019 is the strongest CUT signal there is)
        try:
            from src.services.watch_status import (music_listening_stats,
                                                   format_listening_line)
            ls = music_listening_stats(
                user_id, title, item.get("artist_mbid") or item.get("mbid"))
            owner_line = format_listening_line(ls)
            flags["owner_watched"] = bool(ls)
        except Exception as e:
            logger.debug("[pillars] listening stats failed for %r: %s", title, e)
        try:
            from src.services.lidarr_discography import discography_summary
            disc = await discography_summary(
                artist_mbid=item.get("artist_mbid") or item.get("mbid"),
                artist_name=title)
            if disc:
                owner_line += f" Discography {disc}."
        except Exception as e:
            logger.debug("[pillars] discography failed for %r: %s", title, e)

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
        # allow_summarizer=False: build_evidence runs inside the judge funnel,
        # which HOLDS the curator GPU gate — a summarizer distillation here
        # evicts the resident curator and every next verdict pays a 60-120s
        # reload (GPU-idle churn that also stalls chats queued on the gate).
        # Cached significance still flows in; a data-starved candidate gets the
        # raw Wikipedia-article fallback below (no LLM) instead.
        vd = await ensure_verified_data(
            title, category,
            tmdb_id=item.get("tmdb_id"), tvdb_id=item.get("tvdb_id"),
            anilist_id=item.get("anilist_id"), anidb_id=item.get("anidb_id"),
            plex_rating_key=item.get("plex_rating_key"),
            allow_summarizer=False,
        )
        verified_text = format_verified_block(vd) or ""
        # "9 episodes played" means something different for a 12- vs a
        # 100-episode series — join the two data sources for the judge.
        if (ow and (ow.get("episodes") or 0) >= 2
                and isinstance(vd, dict) and vd.get("episodes_total")):
            owner_line += f" (series total: {vd['episodes_total']} episodes)"
    except Exception as e:
        logger.debug("[pillars] verified-data failed for %r: %s", title, e)
    if verified_text:
        if any(m in verified_text for m in ("RT:", "METACRITIC", "Awards:", " wins", "Significance:")):
            flags["acclaim_present"] = True
        meta_block = verified_text
    else:
        # P5: data-starved candidate — ensure_verified_data returned nothing (no
        # cache + fast-enrich missed). Rather than let the judge guess from a thin
        # synopsis stub, fall back to the title's Wikipedia article (the same
        # entity-match-guarded source the discussion uses). Bounded: only the rare
        # thin candidate reaches here — the well-enriched majority already carry
        # cached Wikipedia significance from ensure_verified_data.
        wiki = ""
        try:
            from src.services.media_enricher import fetch_wikipedia_summary
            wiki = await fetch_wikipedia_summary(title, media_type, max_chars=2000) or ""
        except Exception as e:
            logger.debug("[pillars] wiki fallback failed for %r: %s", title, e)
        if wiki:
            meta_block = f"WIKIPEDIA (no structured enrichment on file):\n{wiki}"
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

    # ── OWNER FEEDBACK (the taste blob's explicit_feedback/disliked_titles —
    # written for years, read by NOBODY until now). A recorded "delete,
    # plot is terrible" or a standing dislike is direct owner testimony the
    # judge must weigh; standing aversions ride along on the taste line. ──
    feedback_line = ""
    try:
        import json as _json
        from src.database.models import EncryptedTasteVector
        etv = db.query(EncryptedTasteVector).filter(
            EncryptedTasteVector.user_id == user_id,
            EncryptedTasteVector.media_category == category).first()
        if etv and etv.encrypted_blob:
            blob = _json.loads(etv.encrypted_blob)
            tl = title.lower()
            fbs = [f for f in (blob.get("explicit_feedback") or [])
                   if (f.get("title") or "").lower() == tl]
            if fbs:
                last = fbs[-1]   # chronological — latest statement wins
                when = (last.get("date") or "")[:10]
                feedback_line = (
                    f"OWNER FEEDBACK on this title ({when or 'undated'}): "
                    f"{last.get('sentiment') or 'neutral'}"
                    f" — {last.get('reason') or 'no reason recorded'}")
                if float(last.get("weight") or 1.0) > 1.0:
                    feedback_line += (" (said in a Curatarr-recommendation "
                                      "follow-up — weigh this owner verdict "
                                      "heavily)")
                feedback_line += "\n"
                flags["owner_signal"] = True
            elif tl in {(t or "").lower() for t in (blob.get("disliked_titles") or [])}:
                feedback_line = ("OWNER FEEDBACK: this title is on the owner's "
                                 "recorded dislike list.\n")
                flags["owner_signal"] = True
            aversions = sorted((blob.get("theme_aversion") or {}).items(),
                               key=lambda x: -x[1])[:5]
            if aversions:
                av_str = ", ".join(k for k, _ in aversions)
                taste = (taste + " " if taste else "") + \
                    f"Standing aversions the owner has voiced: {av_str}."
    except Exception as e:
        logger.debug("[pillars] owner-feedback failed for %r: %s", title, e)

    # Item profile (title + genres + short synopsis) — the anchor for BOTH the
    # per-item owner signals (P2) and the learned-principles retrieval (P4).
    prof_parts = [title, "" if genres_str == "Unknown" else genres_str,
                  (item.get("overview") or "")[:300]]
    item_profile = " — ".join(p for p in prof_parts if p and p.strip())

    # ── OWNER SIGNALS (P2: the judge is no longer memory-blind) ──
    # What the owner has previously told me that plausibly applies to THIS title
    # (a kept franchise, a partner favourite, a values case) — the same per-item
    # considerations bridge the pitch path uses, now fed to the JUDGE so a title
    # the owner has explicitly valued can't be silently trashed on taste alone.
    owner_signals = ""
    try:
        from src.services.episodic_memory import retrieve_considerations
        cons = await retrieve_considerations(user_id, item_profile,
                                             media_category=category, top_k=3)
        # Precision guard (mirrors the visible ⭐ path in recommendations_engine):
        # retrieve_considerations is recall-leaning, and anisotropic embeddings
        # let a broad memory bleed cross-domain on pure embedding alone (a Fast &
        # Furious car-scenes memory fired on Stranger Things in testing). A false
        # signal the JUDGE reasons about is worse than a missed one — require a
        # lexical anchor or an exact-category match before feeding it in.
        cons = [c for c in cons
                if c.get("overlap") or c.get("media_category") == category]
        if cons:
            flags["owner_signal"] = True
            owner_signals = "".join(f"OWNER SIGNAL: {c['content']}\n" for c in cons)
    except Exception as e:
        logger.debug("[pillars] owner-signals failed for %r: %s", title, e)

    # ── LEARNED PRINCIPLES (P4: the judge applies rules the owner taught it) ──
    # The ACTIVE principles relevant to this title, appended to the constitution
    # (returned as law_extra). Empty until the owner promotes shadow principles,
    # so this is a no-op during the shadow rollout.
    law_extra = ""
    try:
        from src.config import settings as _settings
        if getattr(_settings, "PRINCIPLES_ENABLED", False):
            from src.services.curator_principles import (
                retrieve_principles, format_principles_block)
            law_extra = format_principles_block(
                await retrieve_principles(user_id, category=category,
                                          item_profile=item_profile, top_k=6))
    except Exception as e:
        logger.debug("[pillars] principles retrieval failed for %r: %s", title, e)

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
        + feedback_line
        + owner_signals
        + (f"TECH: {tech_line}\n" if tech_line else "TECH: no technical profile on record.\n")
    )
    return {"title": title, "facts": facts.strip(), "flags": flags,
            "law_extra": law_extra}


# ── THE JUDGE ─────────────────────────────────────────────────────────────────
# WRITTEN BUT NOT YET VALIDATED ON A WARM GPU. The schema/verdict path passed a
# smoke test (curatarr-curator, 100% valid, correct verdict); latency + the full
# matrix are pending a free GPU. The model for the verdict call (persona-baked
# curatarr-curator vs the clean base gemma4:31b) is settled by that matrix — for
# now we default to settings.CURATOR_MODEL, swappable via the `model` arg.

_JUDGE_TIMEOUT = 300.0   # s; a cold 31B load is slow — generous on purpose
_VALID_VERDICTS = {"HARD_KEEP", "KEEP_WITH_FLAG", "CUT", "STAGNANT", "EVALUATE"}


async def adjudicate(evidence_facts: str, *, model: str = None,
                     skip_priority: bool = False, law_extra: str = "") -> dict:
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
            {"role": "system",
             "content": PILLAR_CONSTITUTION + (f"\n\n{law_extra}" if law_extra else "")},
            {"role": "user", "content": "FACTS:\n" + evidence_facts},
        ],
        "format": VERDICT_SCHEMA,
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.0, "num_predict": 800,
                    "num_ctx": CURATOR_NUM_CTX, "num_gpu": 99},
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
            async with curator_priority("pillar verdict"):
                r = await _post()
        content = (r.json().get("message") or {}).get("content", "") or ""
        data = parse_llm_json(content)
        if not isinstance(data, dict) or data.get("verdict") not in _VALID_VERDICTS:
            raise ValueError(f"bad verdict payload: {content[:200]!r}")
        return data
    except Exception as e:
        logger.warning("[pillars] adjudicate failed (%s) — defaulting to EVALUATE", e)
        return {"pillar_3_household": "", "pillar_2_custodian": "", "pillar_1_resonance": "",
                "pillar_0_ego": "", "bitrate_note": "", "protecting_pillar": "NONE",
                "verdict": "EVALUATE", "_error": str(e)}


def _lean_facts(facts: str) -> str:
    """Strip the parrot/bloat sources before the prose pass: the OWNER TASTE blob
    and the OWNER SIGNAL lines (the model lifts their words verbatim → every pitch
    sounds identical) and the TECH line (a CUT shouldn't be padded with file-size).
    Keeps the title,
    watch status, and the ACCLAIM & METADATA block (themes / plot / significance)
    — the title's OWN specifics, which is exactly what we want it to argue from."""
    return "\n".join(
        ln for ln in facts.splitlines()
        if not ln.lstrip().startswith(("OWNER TASTE:", "TECH:", "OWNER SIGNAL:"))
    )


_PILLAR_FIELD = {
    "HOUSEHOLD": "pillar_3_household",
    "CUSTODIAN": "pillar_2_custodian",
    "RESONANCE": "pillar_1_resonance",
    "EGO":       "pillar_0_ego",
}


def _governing(verdict: dict) -> str:
    """The pillar finding that DROVE the verdict — the seed the monologue expands
    (so the prose stays specific to this title instead of re-deriving a generic
    taste-mismatch). Anchored on protecting_pillar for a keep; on the Ego /
    Resonance reading otherwise (why it failed to earn its place)."""
    field = _PILLAR_FIELD.get(verdict.get("protecting_pillar") or "")
    if field and verdict.get(field):
        return verdict[field]
    return (verdict.get("pillar_0_ego") or verdict.get("pillar_1_resonance")
            or verdict.get("pillar_3_household") or "")


# Per-verdict stance — frames the pitch WITHOUT making the model announce the
# verdict label ("CUT.") as the opener (the card already shows it).
_MONOLOGUE_STANCE = {
    "CUT": "This title does NOT earn its place — make the sharp case for removing it",
    "HARD_KEEP": "This title earns its place — make the sharp case for keeping it",
    "KEEP_WITH_FLAG": "This title earns its place for its stature, but its file is a "
                      "bitrate outlier — make the case for keeping it AND that it "
                      "should be downscaled to reclaim space",
    "STAGNANT": "This title is merely 'fine' — it neither sharpens the owner's edge "
                "nor earns the quiet-resonance exception; make the honest case that it "
                "is stagnant and worth a second look before it keeps its slot",
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
        # num_ctx MUST match every other curator call: this one slipped the
        # 16384 rollout (no explicit value -> baked 8192 default), so a
        # deletion run alternated judge(16k)/monologue(8k) and FULLY
        # RELOADED the 20 GB model twice per candidate — the VRAM sawtooth
        # the owner caught in Task Manager.
        "options": {"temperature": 0.7, "num_predict": 500, "num_gpu": 99,
                    "num_ctx": CURATOR_NUM_CTX},
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
            async with curator_priority("pillar monologue"):
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
    verdict = await adjudicate(ev["facts"], skip_priority=skip_priority,
                               law_extra=ev.get("law_extra", ""))
    out = {**ev, "verdict": verdict}
    if with_monologue:
        out["monologue"] = await write_monologue(
            ev["facts"], verdict, lang_directive=lang_directive,
            skip_priority=skip_priority)
    return out
