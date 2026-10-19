"""
Curatarr — Curator Principles: the autonomous self-learning layer.

The curator LEARNS generalizable curation principles from its DEBATES with the
owner and (once activated) injects them back into the judge — so its taste
sharpens over time instead of being brilliant-but-amnesiac (sharp per-chat,
forgetful across threads).

Design (validated PROTOTYPE-FIRST in tests/principle_extract_*.py +
tests/novelty_check_proto.py, then built here):

  CAPTURE   a thread's full dialectic (BOTH sides) → extract the PRINCIPLES the
            USER established / endorsed / conceded (never the curator's own
            un-endorsed self-justifications) → NOVELTY-CHECK each against the
            existing rule-set (DECIDE-ONLY, never rewrite → no drift):
            NEW / DUPLICATE / REFINEMENT / CONTRADICTION → store NEW/REFINEMENT/
            CONTRADICTION as 'shadow', drop (and reinforce) DUPLICATE.

  RETRIEVE  the relevant ACTIVE principles per title → injected into the judge's
            constitution (P4).

Storage: CuratorPrinciple (no decay, user-curatable — unlike EpisodicMemory).
Model: the curator itself (settings.CURATOR_MODEL) via a neutral system prompt
that overrides its baked persona (the same trick the pillar judge uses) — one
model stays resident, no eviction churn. NO fine-tuning, NO agentic tool-calling
— this is the memory pattern: background extraction + prompt injection.

Recurrence is a confidence BOOSTER, not a gate (a naive repetition gate failed
validation — the whole RESONANCE insight came from ONE deep thread). The
CONTRADICTION flag is the one human touch-point; otherwise the loop is autonomous.
"""
from __future__ import annotations

import json
import logging
import re

from src.services.llm_utils import CURATOR_NUM_CTX, curator_gpu
from datetime import datetime

import httpx

from src.config import settings
from src.database.connection import get_db_session
from src.database.runner import run_read, run_write
from src.database.models import CuratorPrinciple
from src.services.llm_utils import parse_llm_json
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)

_TIMEOUT = 300.0
_MIN_THREAD_MESSAGES = 4   # fewer than 2 exchanges is not a debate worth mining


# ── THE PROMPTS (validated verbatim) ─────────────────────────────────────────

_EXTRACT_SYS = """You extract GENERALIZABLE CURATION PRINCIPLES from a DEBATE between a USER (the OWNER of a media library — the FINAL authority on their own taste) and their AI curator about keeping / deleting / recommending titles. A principle is a lasting, title-agnostic RULE that should guide FUTURE judgments on OTHER titles.

RULES:
- Extract PRINCIPLES, never title verdicts. ("The user values slow films only when they show technical mastery" = YES. "Keep America's National Parks" = NO.)
- A principle counts ONLY if the USER established it OR endorsed / conceded to it — NEVER the curator's own un-endorsed self-justifications, and never a point the user got rhetorically cornered on but did not actually concede.
- Principles are TASTE rules (what the user values or rejects, and under which conditions) — NEVER factual/technical claims about works, production, or storage ("high bitrate is necessary to preserve animation" = NO), and never generalizations built on the user's unverified assertions about one specific work's scenes or creators.
- Generalizable, title-agnostic, ONE sentence each, ALWAYS in ENGLISH regardless of the conversation's language (the rule-set is stored in English).
- Be CONSERVATIVE: most conversations settle NO lasting principle. If nothing was genuinely established or endorsed by the user, return an empty list. Do NOT invent principles to fill space.

For each principle set basis to how it was reached: user_established, user_endorsed_curator, converged, or unresolved."""

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "principles": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "principle": {"type": "string"},
                    "basis": {"type": "string",
                              "enum": ["user_established", "user_endorsed_curator",
                                       "converged", "unresolved"]},
                },
                "required": ["principle", "basis"],
            },
        },
    },
    "required": ["principles"],
}

_NOVELTY_SYS = """You maintain a media curator's RULE-SET. Given the EXISTING knowledge (the owner's taste profile + the current principles) and a list of CANDIDATE principles freshly extracted from a conversation, classify how each candidate relates to what is ALREADY known. DO NOT rewrite the candidate — only classify it. Deciding (never rewriting) is what keeps the rule-set from drifting away from what the owner actually said.

verdict for each candidate:
- NEW           — adds knowledge not already present.
- DUPLICATE     — already covered by the taste profile or an existing principle.
- REFINEMENT    — sharpens or adds nuance to an existing point.
- CONTRADICTION — conflicts with an existing principle (flag it; the owner decides).

Set related to a few words of the existing rule / profile point it matches (or "-"), and reason to one line.
Each existing principle is listed with its ID: set related_id to the ID of the existing principle the candidate duplicates, refines or contradicts — 0 when it matches only the taste profile, a previously rejected rule, or nothing."""

_NOVELTY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string",
                                "enum": ["NEW", "DUPLICATE", "REFINEMENT", "CONTRADICTION"]},
                    "related": {"type": "string"},
                    "related_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["n", "verdict"],
            },
        },
    },
    "required": ["results"],
}


# ── LLM PLUMBING ─────────────────────────────────────────────────────────────

async def _curator_json(system: str, user: str, schema: dict,
                        num_predict: int = 800) -> dict:
    """One format-forced JSON call to the curator model, persona overridden by
    the neutral ``system`` prompt. Defers to any live curator work first (this is
    background learning — it must never delay a chat turn)."""
    try:
        from src.services.llm_priority import wait_for_curator
        await wait_for_curator()
    except Exception as e:
        # Fail closed: a broken yield gate must not let background learning
        # take the GPU from a live chat. Every caller logs and skips this run.
        logger.warning("[principles] curator yield gate failed - skipping this "
                       "call: %r", e)
        raise
    payload = {
        "model": settings.CURATOR_MODEL,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "format": schema, "stream": False, "think": False, "keep_alive": "10m",
        # repeat_penalty 1.0: constrained JSON repeats its own punctuation and
        # key prefixes by construction (see pillars.adjudicate).
        "options": {"temperature": 0.1, "num_predict": num_predict,
                    "repeat_penalty": 1.0,
                    "num_ctx": CURATOR_NUM_CTX, **curator_gpu()},
    }
    async with httpx.AsyncClient(verify=tls_context(), timeout=_TIMEOUT) as client:
        r = await client.post(f"{settings.effective_ollama}/api/chat", json=payload)
    r.raise_for_status()
    return parse_llm_json((r.json().get("message") or {}).get("content", "") or "")


# ── THREAD TEXT (both sides) ─────────────────────────────────────────────────

def _thread_text(user_id: int, thread_id: str) -> tuple[str, int]:
    """The full transcript of one thread — BOTH sides, oldest first — plus the
    message count. Unlike memory extraction (user-only), principle extraction
    must see the DEBATE: what the curator argued and whether the user conceded."""
    from src.database.models import ConversationMessage
    with get_db_session() as db:
        q = db.query(ConversationMessage.role, ConversationMessage.content).filter(
            ConversationMessage.user_id == user_id)
        if thread_id == "general":
            q = q.filter((ConversationMessage.thread_id == "general")
                         | (ConversationMessage.thread_id.is_(None)))
        else:
            q = q.filter(ConversationMessage.thread_id == thread_id)
        rows = q.order_by(ConversationMessage.created_at, ConversationMessage.id).all()
    lines = [f"{'USER' if role == 'user' else 'CURATOR'}: {(content or '').strip()}"
             for role, content in rows if (content or '').strip()]
    return "\n\n".join(lines), len(lines)


# ── EXISTING CONTEXT for the novelty check ───────────────────────────────────

def _existing_context(user_id: int) -> tuple[list[tuple[int, str]], str, list[str]]:
    """The rule-set the candidate is judged against: the current principles
    as (id, text) (active + shadow, so we don't re-capture a shadow
    duplicate; the id lets the verdict name WHICH rule it relates to), the
    owner's taste-profile summary, and the REJECTED principles — the owner's
    explicit "no" is knowledge too. Without the rejected list, the next debate
    touching the same theme re-derived the same rule as NEW and rang the bell
    again (the comfort-food principle would have resurrected forever)."""
    existing: list[tuple[int, str]] = []
    rejected: list[str] = []
    with get_db_session() as db:
        rows = (db.query(CuratorPrinciple.id, CuratorPrinciple.text, CuratorPrinciple.status)
                  .filter(CuratorPrinciple.user_id == user_id,
                          CuratorPrinciple.status.in_(("active", "shadow", "rejected")))
                  .all())
        existing = [(r[0], r[1]) for r in rows if r[1] and r[2] in ("active", "shadow")]
        rejected = [r[1] for r in rows if r[1] and r[2] == "rejected"]
    taste = ""
    try:
        from src.database.models import TasteVectorEntry
        with get_db_session() as db:
            tv = (db.query(TasteVectorEntry)
                    .filter(TasteVectorEntry.user_id == user_id).first())
            if tv and tv.summary_text:
                taste = tv.summary_text[:3500]
    except Exception as e:
        logger.warning("[principles] taste fetch failed - novelty check runs "
                       "without the taste summary: %s", e)
    return existing, taste, rejected


def _build_novelty_user(candidates: list[str], existing: list, taste: str,
                        rejected: list[str] | None = None) -> str:
    """``existing`` holds (id, text) pairs — listed with their IDs so the
    verdict can name one — or plain texts (the prototype scripts)."""
    parts = []
    if taste:
        parts.append("TASTE PROFILE (existing knowledge):\n" + taste)
    parts.append("EXISTING PRINCIPLES:\n" + (
        "\n".join(f"- ID {e[0]}: {e[1]}" if isinstance(e, (tuple, list)) else f"- {e}"
                  for e in existing) if existing else "(none yet)"))
    if rejected:
        parts.append(
            "PREVIOUSLY REJECTED BY THE OWNER (they explicitly dismissed these "
            "rules — a candidate that restates one is DUPLICATE, not NEW):\n"
            + "\n".join(f"- {t}" for t in rejected))
    parts.append("CANDIDATES:\n" + "\n".join(
        f"{i}. {c}" for i, c in enumerate(candidates, 1)))
    return "\n\n".join(parts)


# ── EMBEDDING + REINFORCEMENT ────────────────────────────────────────────────

async def _embed(text: str):
    try:
        from src.services.episodic_memory import _embed as em_embed
        return await em_embed(text)
    except Exception as e:
        # em_embed already logs and returns None on an embed outage, so
        # anything landing here is a bug, not a transient.
        logger.warning("[principles] embedding crashed: %s", e, exc_info=True)
        return None


_RELATED_ID = re.compile(r"^#(\d+)\b")


def _related_ref(related_id, related: str | None, known_ids: set) -> str | None:
    """The stored ``related`` value: "#<id>: <words>" when the verdict named an
    existing principle by a VALID id (one actually listed — a model may
    invent one), else the free-text words as before."""
    words = (related or "").strip()
    words = None if words in ("", "-", "—") else words
    try:
        rid = int(related_id or 0)
    except (TypeError, ValueError):
        rid = 0
    if rid and rid in known_ids:
        return f"#{rid}: {words}" if words else f"#{rid}"
    return words


def _related_id(related: str | None) -> int | None:
    m = _RELATED_ID.match(related or "")
    return int(m.group(1)) if m else None


def supersede_contradicted(db, row) -> int | None:
    """When a CONTRADICTION principle is adopted, the rule it contradicts
    stops being active (status 'superseded', excluded from retrieval). Before,
    both stayed active and the judge was told to "apply" two opposite rules.
    Works only when the capture resolved the related rule's id; free-text
    ``related`` is left alone (it cannot be resolved safely). Returns the
    superseded id. The caller commits."""
    if (row.novelty or "") != "contradiction":
        return None
    rid = _related_id(row.related)
    if not rid or rid == row.id:
        return None
    old = (db.query(CuratorPrinciple)
             .filter(CuratorPrinciple.id == rid,
                     CuratorPrinciple.user_id == row.user_id,
                     CuratorPrinciple.status.in_(("active", "shadow")))
             .first())
    if old is None:
        return None
    old.status = "superseded"
    old.related = f"superseded by #{row.id}"
    logger.info("🧭 [PRINCIPLE] #%d supersedes contradicted #%d", row.id, old.id)
    return old.id


def _reinforce_duplicate(user_id: int, related: str, related_id: int | None = None) -> None:
    """Recurrence = a confidence BOOSTER, not a gate: when the owner restates a
    principle we already hold, bump its counter instead of storing a near-copy.
    A resolved ``related_id`` names the rule exactly; otherwise the free-text
    ``related`` words are matched as before."""
    if related_id:
        try:
            with get_db_session() as db:
                row = (db.query(CuratorPrinciple)
                         .filter(CuratorPrinciple.id == related_id,
                                 CuratorPrinciple.user_id == user_id).first())
                if row is not None:
                    row.times_reinforced = (row.times_reinforced or 0) + 1
                    db.commit()
                    logger.info("[principles] reinforced #%d (×%d): %s", row.id,
                                row.times_reinforced, (row.text or "")[:60])
                    return
        except Exception as e:
            logger.warning("[principles] reinforce by id failed: %s", e, exc_info=True)
    if not related or related.strip() in ("-", "—", ""):
        return
    key = related.strip().lower()[:40]
    try:
        with get_db_session() as db:
            # Rejected rows included: a duplicate of a rule the owner dismissed
            # is DROPPED either way, but bumping its counter records that the
            # theme keeps resurfacing — if it climbs, that's a signal to bring
            # it up with the owner again rather than silently suppress forever.
            rows = (db.query(CuratorPrinciple)
                      .filter(CuratorPrinciple.user_id == user_id,
                              CuratorPrinciple.status.in_(
                                  ("active", "shadow", "rejected"))).all())
            for row in rows:
                if key and key in (row.text or "").lower():
                    row.times_reinforced = (row.times_reinforced or 0) + 1
                    db.commit()
                    logger.info("[principles] %s #%d (×%d): %s",
                                "recurrence on REJECTED" if row.status == "rejected"
                                else "reinforced",
                                row.id, row.times_reinforced, (row.text or "")[:60])
                    return
    except Exception as e:
        logger.warning("[principles] reinforce failed: %s", e, exc_info=True)


# ── CAPTURE (P3) ─────────────────────────────────────────────────────────────

async def capture_principles_from_thread(user_id: int, thread_id: str,
                                         category: str | None = None) -> list[dict]:
    """Mine one finished debate for lasting principles and store the novel ones
    as SHADOW. Returns the stored principles (for logging / surfacing). Safe to
    call on any thread — a thin / non-principled one just returns []."""
    if not getattr(settings, "PRINCIPLES_ENABLED", False):
        return []
    convo, n = await run_read(_thread_text, user_id, thread_id)
    if n < _MIN_THREAD_MESSAGES:
        return []

    # A long debate overflowed the window, and Ollama truncated from the front
    # — the extraction rules went first. Keep the newest turns instead: what
    # a debate ends on is what the owner endorsed.
    from src.services.prompt_budget import budget_for, keep_tail
    convo = keep_tail(convo, budget_for(CURATOR_NUM_CTX, 700, _EXTRACT_SYS) - 8)

    # 1 — extract (both sides, user-endorsed only)
    try:
        ex = await _curator_json(_EXTRACT_SYS, "DEBATE:\n" + convo, _EXTRACT_SCHEMA, 700)
    except Exception as e:
        logger.warning("[principles] extract failed for %s: %s", thread_id, e)
        return []
    candidates = [c for c in (ex.get("principles") or [])
                  if isinstance(c, dict) and (c.get("principle") or "").strip()
                  and c.get("basis") != "unresolved"]
    if not candidates:
        logger.debug("[principles] thread %s: nothing endorsed to learn", thread_id)
        return []

    # 2 — novelty-check against the existing rule-set (decide-only)
    existing, taste, rejected = await run_read(_existing_context, user_id)
    known_ids = {pid for pid, _ in existing}
    cand_texts = [c["principle"].strip() for c in candidates]
    try:
        nv = await _curator_json(
            _NOVELTY_SYS, _build_novelty_user(cand_texts, existing, taste, rejected),
            _NOVELTY_SCHEMA, 900)
        by_n = {int(r["n"]): r for r in (nv.get("results") or [])
                if isinstance(r, dict) and r.get("n") is not None}
    except Exception as e:
        logger.warning("[principles] novelty check failed for %s: %s", thread_id, e)
        by_n = {}
    # Fail CLOSED. An empty verdict map used to turn every candidate into NEW:
    # duplicates and contradictions of standing rules were stored and rang the
    # bell (or went live under PRINCIPLE_AUTO_ACTIVATE). Capture has no
    # cursor, so the next extraction on this thread mines it again.
    if not by_n:
        logger.warning("[principles] thread %s: no novelty verdicts — capture "
                       "skipped this run (%d candidate(s))", thread_id,
                       len(candidates))
        return []

    # 3 — store per verdict (DUPLICATE dropped+reinforced; the rest → shadow)
    stored = []
    for i, c in enumerate(candidates, 1):
        r = by_n.get(i, {})
        verdict = (r.get("verdict") or "NEW").upper()   # unclassified → treat as NEW
        related = _related_ref(r.get("related_id"), r.get("related"), known_ids)
        if verdict == "DUPLICATE":
            await run_write(_reinforce_duplicate, user_id, r.get("related") or "",
                            _related_id(related))
            continue
        if verdict not in ("NEW", "REFINEMENT", "CONTRADICTION"):
            verdict = "NEW"
        text = c["principle"].strip()
        emb = await _embed(text)
        # Shadow by default; once the owner trusts the loop (PRINCIPLE_AUTO_ACTIVATE)
        # NEW/REFINEMENT go straight to active — a CONTRADICTION always waits.
        status = "shadow"
        if verdict != "CONTRADICTION" and getattr(settings, "PRINCIPLE_AUTO_ACTIVATE", False):
            status = "active"

        def _store():
            with get_db_session() as db:
                row = CuratorPrinciple(
                    user_id=user_id, text=text, basis=c.get("basis"),
                    category=category, status=status, novelty=verdict.lower(),
                    related=related, origin_thread_id=thread_id,
                    origin_summary=(r.get("reason") or None),
                    embedding_json=json.dumps(emb) if emb else None,
                    created_at=datetime.utcnow(),
                    activated_at=datetime.utcnow() if status == "active" else None,
                )
                db.add(row)
                db.commit()
                db.refresh(row)
                return row.id

        try:
            row_id = await run_write(_store)
            stored.append({"id": row_id, "text": text, "novelty": verdict.lower(),
                           "basis": c.get("basis"), "status": status})
            logger.info("🧭 [PRINCIPLE %s] %s #%d: %s",
                        verdict, status, row_id, text[:80])
        except Exception as e:
            logger.warning("[principles] store failed: %s", e)
    return stored


# ── PRINCIPLE REVIEW (bell → chat → settled decision applied) ────────────────

_REVIEW_SYS = """You read ONE exchange from a review conversation in which the OWNER of a media library and their curator debate whether a LEARNED RULE should be adopted into the curation rule-set. Decide what the OWNER settled in THIS exchange.

decision:
- adopt         — the owner clearly accepts the rule with its current wording.
- adopt_revised — the owner accepts the rule but the exchange settled on DIFFERENT wording (their own, or the curator's refinement they explicitly endorsed). Put that final wording in revised_text: ONE sentence, faithful to what was actually agreed — never invent.
- reject        — the owner clearly dismisses the rule.
- none          — still discussing, undecided, or off-topic.

Be conservative: questions, musings and partial agreement are none. revised_text stays "" unless decision is adopt_revised — and it is ALWAYS written in ENGLISH (translate faithfully if the conversation was held in another language; the rule-set is stored in English)."""

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string",
                     "enum": ["adopt", "adopt_revised", "reject", "none"]},
        "revised_text": {"type": "string"},
    },
    "required": ["decision", "revised_text"],
}


async def detect_and_apply_principle_verdict(user_id: int, principle_id: int,
                                             user_message: str,
                                             assistant_response: str) -> None:
    """Post-turn hook for principle-review threads: classify whether the OWNER
    settled the principle's fate this exchange and apply it — adopt → active,
    reject → rejected, adopt_revised → the user-sanctioned wording replaces the
    text (this is the ONE place a rewrite is allowed: the owner endorsed it
    explicitly, which is the opposite of autonomous drift) and it activates.
    Best-effort: any failure leaves the principle in shadow for the panel."""
    def _rule_text():
        with get_db_session() as db:
            row = (db.query(CuratorPrinciple)
                   .filter(CuratorPrinciple.id == principle_id,
                           CuratorPrinciple.user_id == user_id).first())
            return (row.text,) if row else None

    try:
        found = await run_read(_rule_text)
        if not found:
            return
        rule_text = found[0]
        convo = (f"RULE UNDER REVIEW:\n\"{rule_text}\"\n\n"
                 f"OWNER: {(user_message or '').strip()[:1200]}\n\n"
                 f"CURATOR: {(assistant_response or '').strip()[:1500]}")
        v = await _curator_json(_REVIEW_SYS, convo, _REVIEW_SCHEMA, 300)
        decision = (v.get("decision") or "none").lower()
        revised = (v.get("revised_text") or "").strip()
        if decision == "none":
            return
        refine = decision == "adopt_revised" and len(revised) >= 15
        # The embedding is fetched before the write: the write below runs on
        # the writer thread and cannot await.
        emb = None
        if refine:
            try:
                emb = await _embed(revised)
            except (TypeError, ValueError):
                pass

        def _apply():
            with get_db_session() as db:
                row = (db.query(CuratorPrinciple)
                       .filter(CuratorPrinciple.id == principle_id,
                               CuratorPrinciple.user_id == user_id).first())
                if not row:
                    return
                if decision == "reject":
                    row.status = "rejected"
                    logger.info("🧭 [PRINCIPLE REVIEW] #%d rejected by the owner in chat",
                                row.id)
                elif decision in ("adopt", "adopt_revised"):
                    if refine:
                        logger.info("🧭 [PRINCIPLE REVIEW] #%d wording refined in review: %s",
                                    row.id, revised[:90])
                        row.text = revised
                        row.origin_summary = ((row.origin_summary or "")
                                              + " | wording refined in owner review").strip(" |")
                        if emb:
                            row.embedding_json = json.dumps(emb)
                    row.status = "active"
                    row.activated_at = datetime.utcnow()
                    supersede_contradicted(db, row)
                    logger.info("🧭 [PRINCIPLE REVIEW] #%d ADOPTED (%s) — now active",
                                row.id, decision)
                db.commit()

        await run_write(_apply)
    except Exception as e:
        logger.warning("[principles] review verdict failed for #%s: %s",
                       principle_id, e)


# ── RETRIEVE (for P4 injection) ──────────────────────────────────────────────

async def retrieve_principles(user_id: int, category: str | None = None,
                              item_profile: str | None = None,
                              top_k: int = 6) -> list[str]:
    """The ACTIVE principles to inject into the judge for THIS title. Global
    (category NULL) + same-category principles apply; when an item_profile is
    given we rank by embedding similarity and keep the top_k, else return all
    active (there are few). Shadow principles are never injected — that is the
    whole point of the shadow rollout."""
    def _load():
        with get_db_session() as db:
            q = db.query(CuratorPrinciple).filter(
                CuratorPrinciple.user_id == user_id,
                CuratorPrinciple.status == "active")
            if category:
                q = q.filter((CuratorPrinciple.category == category)
                             | (CuratorPrinciple.category.is_(None)))
            return [(p.text, p.embedding_json) for p in q.all() if p.text]

    rows = await run_read(_load)
    if not rows:
        return []
    if not item_profile or len(rows) <= top_k:
        return [t for t, _ in rows]
    # rank by relevance to this title
    try:
        from src.services.episodic_memory import _cosine_similarity
        qe = await _embed(item_profile)
        if qe:
            scored = []
            for text, emb_json in rows:
                try:
                    sem = _cosine_similarity(qe, json.loads(emb_json)) if emb_json else 0.0
                except (ValueError, TypeError):
                    sem = 0.0
                if sem is None:
                    sem = 0.0   # dimension mismatch ranks like "no embedding"
                scored.append((sem, text))
            scored.sort(key=lambda x: x[0], reverse=True)
            return [t for _, t in scored[:top_k]]
    except Exception as e:
        from src.services.enrichment_state import log_topup_failure
        log_topup_failure(logger, "[principles] retrieve ranking", category, e)
    return [t for t, _ in rows[:top_k]]


def format_principles_block(principles: list[str]) -> str:
    """Render active principles for injection into the judge's constitution."""
    if not principles:
        return ""
    lines = ["LEARNED PRINCIPLES (rules the owner established in past debates — "
             "apply them as part of your judgment):"]
    lines += [f"- {p}" for p in principles]
    return "\n".join(lines)


# ── CONDENSE (rule-set hygiene) ───────────────────────────────────────────────
# As debates accumulate, near-duplicate principles pile up and crowd unique
# rules out of the judge's top_k. Prototype-validated (tests/condense_proto.py):
# embedding-threshold clustering FAILED calibration (one-sentence rules sit too
# close in nomic space — the top cosine pair was semantically different while a
# real duplicate scored lower), so the merge decision is the curator's: ONE
# format-forced call per category group over the full listing. Verified
# conservative on the real rule-set (0 merges on a clean set) and precise on
# planted duplicates (grouped exactly the clones, nothing else).

_CONDENSE_SYS = """You maintain a media curator's RULE-SET: the owner's established curation principles. Some principles have accumulated near-duplicates. Identify groups of principles that state the SAME underlying requirement, and write ONE consolidated principle per group.

RULES:
- Be CONSERVATIVE. Merge ONLY principles whose meaning is the same requirement or the same requirement plus a sharpening of it. Most principles stand alone; an EMPTY merges list is the expected common outcome.
- Never merge a KEEP-condition rule with a DELETE-condition rule, even on the same topic.
- The consolidated principle must preserve EVERY nuance that could change a future verdict — one sentence, title-agnostic, ENGLISH, no examples. If the nuances cannot fit one sentence, do not merge.
- Use the exact IDs shown. A group needs at least 2 ids."""

_CONDENSE_SCHEMA = {
    "type": "object",
    "properties": {
        "merges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "integer"}},
                    "merged_principle": {"type": "string"},
                },
                "required": ["ids", "merged_principle"],
            },
        },
    },
    "required": ["merges"],
}


async def condense_principles(user_id: int, dry_run: bool = False) -> dict:
    """Consolidate near-duplicate ACTIVE principles, one curator call per
    category group. Sources become status='merged' (audit trail, never hard
    deleted, related records the successor); the consolidated rule is written
    active with basis='condensed' and the summed reinforcement count."""
    def _load():
        with get_db_session() as db:
            return [{"id": p.id, "text": p.text, "category": p.category,
                     "basis": p.basis, "times_reinforced": p.times_reinforced or 0}
                    for p in db.query(CuratorPrinciple).filter(
                        CuratorPrinciple.user_id == user_id,
                        CuratorPrinciple.status == "active",
                    ).order_by(CuratorPrinciple.id)]

    rows = await run_read(_load)

    groups: dict = {}
    for r in rows:
        groups.setdefault(r["category"], []).append(r)

    report = {"active_before": len(rows), "groups_reviewed": 0,
              "merged": [], "dry_run": dry_run}
    claimed: set = set()   # a principle may join at most one merge group

    for cat, members in groups.items():
        if len(members) < 2:
            continue
        report["groups_reviewed"] += 1
        listing = "\n".join(f"ID {m['id']}: {m['text']}" for m in members)
        try:
            res = await _curator_json(
                _CONDENSE_SYS,
                f"ACTIVE PRINCIPLES:\n{listing}\n\n"
                f"Propose merge groups (empty list if none).",
                _CONDENSE_SCHEMA, num_predict=600)
        except Exception as e:
            logger.warning("[principles] condense call failed for %r: %s", cat, e)
            continue
        valid = {m["id"]: m for m in members}
        for mg in (res.get("merges") or []):
            ids = [i for i in dict.fromkeys(mg.get("ids") or [])
                   if i in valid and i not in claimed]
            text = (mg.get("merged_principle") or "").strip()
            if len(ids) < 2 or not text:
                continue
            claimed.update(ids)
            entry = {"category": cat, "sources": [
                {"id": i, "text": valid[i]["text"], "basis": valid[i]["basis"]}
                for i in sorted(ids)], "text": text}
            if not dry_run:
                emb = await _embed(text)

                def _merge():
                    with get_db_session() as db:
                        new = CuratorPrinciple(
                            user_id=user_id, text=text, category=cat,
                            basis="condensed", status="active",
                            novelty="NEW",
                            times_reinforced=sum(valid[i]["times_reinforced"] for i in ids),
                            origin_summary="condensed from #" + ",#".join(str(i) for i in sorted(ids)),
                            embedding_json=json.dumps(emb) if emb else None,
                            created_at=datetime.utcnow(),
                            activated_at=datetime.utcnow(),
                        )
                        db.add(new)
                        db.flush()
                        for i in ids:
                            src = db.get(CuratorPrinciple, i)
                            if src:
                                src.status = "merged"
                                src.related = f"merged into #{new.id}"
                        db.commit()
                        return new.id

                entry["new_id"] = await run_write(_merge)
            report["merged"].append(entry)

    report["active_after"] = (report["active_before"]
                              - sum(len(m["sources"]) for m in report["merged"])
                              + len(report["merged"]))
    logger.info("[principles] condense: %d group(s) reviewed, %d merge(s), "
                "active %d -> %d%s", report["groups_reviewed"],
                len(report["merged"]), report["active_before"],
                report["active_after"], " (dry run)" if dry_run else "")
    return report
