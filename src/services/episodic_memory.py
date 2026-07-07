"""
Curatarr 1.0 - Episodic Memory Service

Gives the curator LLM the ability to actively retrieve memories
rather than stuffing everything into context.

Architecture (inspired by EM-LLM but simpler and local):

  WRITE: Any significant event → summary → embedding → stored in memory DB
         Events: chat exchange, feedback, binge, taste change, explicit statement

  READ:  LLM generates a "memory query" → semantic search → top-k memories
         returned → injected into context as [MEMORIES] block

  FORGET: Old, low-importance, superseded memories decay over time

Memory types:
  - taste_observation   "User binged 8 episodes of X — seems to be in a dark mood"
  - explicit_statement  "User said they hate slow-burn dramas"
  - feedback            "User gave thumbs down to Y recommendation because Z"
  - viewing_pattern     "User watches anime at 2am on weekends"
  - preference_shift    "User's metal listening dropped 40% this week vs avg"
  - conversation        "User asked about similar shows to X, liked suggestions"
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timedelta
from typing import Optional

import httpx

from src.database.connection import get_db_session
from src.database.models import EpisodicMemory, EncryptedTasteVector
from src.services.taste_vectors import decrypt_vector, encrypt_vector, merge_feedback_into_vector
from src.config import settings
from src.services.llm_utils import clean_llm_text, parse_llm_json, strip_think_tags, ollama_options, SUMMARIZER_KEEP_ALIVE

logger = logging.getLogger(__name__)


# Pass 50: post-LLM guard for the protection detector. If the assistant
# response is in clear delete-stance, the title is NOT protected this
# turn — regardless of what the detector LLM concluded from the user
# message. Real-world failure mode: user writes "halt the deletion on X"
# as the opener for a stress-test argument; detector latches the bare
# imperative and emits PROTECT_MEDIA even though the curator answered
# "Delete it." in the same turn. The detector prompt does carry a
# Negotiation-Check rule but the LLM doesn't apply it reliably under
# strong imperative cues — this list is the deterministic backstop.
#
# Tokens are restrictive on purpose: only clear curator verdicts, not
# pitch-style "for deletion" mentions. False negatives (real curator
# delete-stance missed → spurious protection passes through) are the
# expected failure mode, false positives (blocking a legitimate protect
# when curator actually agreed to keep) would be the bad case.
_ASSISTANT_DELETE_STANCE_TOKENS: tuple[str, ...] = (
    "verdict: delete", "verdict:delete",
    "delete it.", "delete it,",
    "i stand by the deletion", "i stand by deleting",
    "the deletion stands", "deletion stands.",
    "free the",                # "free the 78.9 GB"
    "i'm sticking with delete", "i'll stick with delete",
    "it's filler", "it is filler",
)


# Pass 79: deterministic USER-side delete-intent backstop. The Pass-50
# guard catches "curator still arguing for delete" but NOT the symmetric
# failure: "user is agreeing to delete" → small LLM still classifies as
# PROTECT_MEDIA and the deletion proposal gets auto-rejected. Real misfire
# from the log: user wrote "hell no away with this", classifier returned
# PROTECT_MEDIA + RESOLUTION: override, the Deadliest Catch proposal was
# rejected and a bogus "kept (override)" row landed in
# CuratorResolutionLog. With this veto, an explicit user delete-intent in
# the same message vetoes any PROTECT_MEDIA the LLM might emit.
#
# Tokens are intentionally directional (first-person commitments to delete,
# affirmations of the curator's pitch, strong dispose idioms). Bare "delete
# it" is NOT in the list because it appears in legitimate keep contexts
# like "don't delete it, I love it" — a false veto there would be the bad
# case. False NEGATIVES (real delete-intent slips past the list, classifier
# still mis-PROTECTs) are the acceptable failure mode — the prompt's own
# DELETE-INTENT NO_ACTION rule (below) is the first line of defense; this
# token list is the deterministic backstop for when the LLM ignores it.
_USER_DELETE_INTENT_TOKENS: tuple[str, ...] = (
    # First-person commitments
    "i'll delete", "i will delete", "ill delete",
    "i'm deleting", "im deleting",
    "i'm gonna delete", "im gonna delete", "gonna delete it",
    "i'll remove", "i will remove",
    # Affirmations of the deletion pitch
    "yeah delete", "yes delete", "yep delete", "ok delete",
    "sure delete", "fine delete", "agreed delete",
    "go ahead delete", "go ahead and delete",
    # Strong dispose idioms (rarely appear negated)
    "away with",         # "hell no away with this"
    "get rid of",
    "good riddance",
    "trash it", "kill it", "nuke it",
    # German
    "lösche", "lösch das", "lösch es", "löschen",
    "weg damit", "weg mit",
    "raus damit", "raus mit",
)


# ── IMPORTANCE SCORING ────────────────────────────────────────────────────────

IMPORTANCE_WEIGHTS = {
    "explicit_statement": 1.0,   # "I hate X" — always important
    "feedback":           0.9,   # thumbs down with reason
    "taste_observation":  0.7,   # inferred from behavior
    "preference_shift":   0.8,   # significant change
    "viewing_pattern":    0.5,   # temporal habits
    "conversation":       0.4,   # general chat context
}


def compute_importance(memory_type: str, content: str, metadata: dict) -> float:
    base = IMPORTANCE_WEIGHTS.get(memory_type, 0.5)
    # Boost if user was explicit
    if any(w in content.lower() for w in ["hate", "love", "never", "always", "dislike", "boring"]):
        base = min(1.0, base + 0.2)
    # Boost if involves a specific title
    if metadata.get("title"):
        base = min(1.0, base + 0.1)
    return round(base, 3)


# ── EMBEDDING ─────────────────────────────────────────────────────────────────

async def _embed(text: str) -> Optional[list]:
    """Generate embedding via Ollama."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/embeddings",
                json={"model": settings.EMBEDDING_MODEL, "prompt": text},
            )
        if r.status_code == 200:
            return r.json().get("embedding")
    except Exception as e:
        logger.debug("Memory embedding failed: %s", e)
    return None


def _cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── WRITE MEMORY ──────────────────────────────────────────────────────────────

async def write_memory(
    user_id: int,
    memory_type: str,
    content: str,
    metadata: dict = None,
    media_category: str = None,
) -> Optional[int]:
    """
    Store a new episodic memory.
    Returns the memory ID or None on failure.
    """
    if not content or len(content.strip()) < 10:
        return None

    importance = compute_importance(memory_type, content, metadata or {})
    embedding = await _embed(content)

    with get_db_session() as db:
        mem = EpisodicMemory(
            user_id=user_id,
            memory_type=memory_type,
            content=content,
            metadata_json=json.dumps(metadata or {}),
            media_category=media_category,
            importance=importance,
            embedding_json=json.dumps(embedding) if embedding else None,
            created_at=datetime.utcnow(),
            last_accessed=datetime.utcnow(),
            access_count=0,
        )
        db.add(mem)
        db.commit()
        db.refresh(mem)
        logger.debug("Wrote memory %d (type=%s importance=%.2f)", mem.id, memory_type, importance)
        return mem.id
    
# ── CHECK MEMORIES FOR CONFLICTS ──────────────────────────────────────────────

async def resolve_memory_conflicts(user_id: int, new_memory_id: int, new_content: str, metadata: dict):
    """
    Checks if a newly added memory conflicts with older memories and resolves it.
    """
    title = metadata.get("title", "")

    with get_db_session() as db:
        from src.database.models import EpisodicMemory
        
        # Only compare against memories older than 1 minute. Otherwise
        # two memories written in the same chat batch would cannibalise
        # each other as "duplicates" before either had a chance to land.
        cutoff_time = datetime.utcnow() - timedelta(minutes=1)
        
        old_memories = db.query(EpisodicMemory).filter(
            EpisodicMemory.user_id == user_id,
            EpisodicMemory.id != new_memory_id,
            EpisodicMemory.created_at < cutoff_time
        ).all()
        
        target_memories = []
        if title:
            # Title-bearing memory: a conflict is another memory about the SAME
            # title (a changed opinion on that one title).
            for m in old_memories:
                try:
                    if (json.loads(m.metadata_json) or {}).get("title") == title:
                        target_memories.append(m)
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass
        else:
            # Title-less GENERAL preference. The old code matched by title, so
            # "" == "" made EVERY general preference a "conflict" of every other:
            # one new statement (e.g. valuing a WWII doc) ran a NUANCE check
            # against dozens of unrelated memories (D&D, franchise-keep,
            # partner-keep…) and mass-decayed them — eroding the very pillars.
            # Compare only near-DUPLICATE memories by embedding similarity, so a
            # restatement reinforces its twin and unrelated preferences are never
            # touched.
            new_mem = db.query(EpisodicMemory).filter(
                EpisodicMemory.id == new_memory_id).first()
            try:
                new_emb = json.loads(new_mem.embedding_json) if (
                    new_mem and new_mem.embedding_json) else None
            except Exception:
                new_emb = None
            if not new_emb:
                return
            for m in old_memories:
                if not m.embedding_json:
                    continue
                try:
                    if _cosine_similarity(new_emb, json.loads(m.embedding_json)) >= 0.80:
                        target_memories.append(m)
                except Exception:
                    pass

        if not target_memories:
            return

        for old_mem in target_memories:
            prompt = f"""[MODE: MEMORY CONFLICT RESOLUTION]
Analyze how the NEW memory relates to the OLD memory regarding the title '{title}'.

OLD MEMORY: "{old_mem.content}"
NEW MEMORY: "{new_content}"

Evaluate the relationship:
- "CONTRADICTION": They are exact opposites (e.g., hating vs. loving). One must be false.
- "REAFFIRMATION": The NEW repeats, restates, or strengthens the SAME point or sentiment as the OLD — the user is reinforcing a standing preference, not changing it.
- "NUANCE": A DIFFERENT but compatible aspect, or a genuine partial shift in opinion that doesn't completely invalidate the old one.
- "UNRELATED": They talk about completely different things.

Output ONLY a JSON block:
{{"status": "CONTRADICTION" | "REAFFIRMATION" | "NUANCE" | "UNRELATED", "reason": "brief explanation"}}"""

            try:
                async with httpx.AsyncClient(timeout=20) as client:
                    r = await client.post(
                        f"{settings.effective_ollama}/api/chat",
                        json={
                            "model": settings.SUMMARIZER_MODEL,
                            "messages": [{"role": "user", "content": prompt}],
                            "stream": False,
                            "keep_alive": SUMMARIZER_KEEP_ALIVE,
                            **ollama_options(temperature=0.1, num_predict=500),
                        },
                    )

                if r.status_code == 200:
                    text = r.json().get("message", {}).get("content", "").strip()
                    result = parse_llm_json(text)
                    status = result.get("status")
                    
                    if status == "CONTRADICTION":
                        logger.info(f"🧹 [MEMORY RESOLUTION] Contradiction found! Deleting old memory ID {old_mem.id}.")
                        db.delete(old_mem)
                        db.commit()

                    elif status == "REAFFIRMATION":
                        # The user is RESTATING a standing preference — reinforce
                        # it, don't decay it. The old code had no such branch, so
                        # restatements fell through to NUANCE and lost 0.4 each
                        # time: the principles the user repeated MOST (their
                        # "pillars") decayed to the 0.1 floor and vanished from
                        # retrieval. Reinforce the old memory and drop the fresh
                        # duplicate so the signal consolidates instead of
                        # fragmenting into ever-weaker near-copies.
                        old_mem.importance = min(1.0, (old_mem.importance or 0.5) + 0.2)
                        old_mem.last_accessed = datetime.utcnow()
                        old_mem.access_count = (old_mem.access_count or 0) + 1
                        db.commit()
                        logger.info(f"💪 [MEMORY RESOLUTION] Reaffirmation — boosting old memory ID {old_mem.id} to importance {old_mem.importance:.2f}.")
                        new_mem = db.query(EpisodicMemory).filter(EpisodicMemory.id == new_memory_id).first()
                        if new_mem:
                            db.delete(new_mem)
                            db.commit()
                        return  # consolidated into the reinforced old memory

                    elif status == "NUANCE":
                        # A genuine partial shift slightly ages the old memory, but
                        # never below a still-retrievable floor. The old -0.4 to a
                        # 0.1 floor was a sledgehammer that made memories invisible
                        # (retrieval drops anything scoring < 0.1, and importance is
                        # a direct multiplier).
                        old_mem.importance = max(0.3, (old_mem.importance or 0.5) - 0.1)
                        logger.info(f"⚖️ [MEMORY RESOLUTION] Nuance shift — easing old memory ID {old_mem.id} to importance {old_mem.importance:.2f}.")
                        db.commit()

            except Exception as e:
                logger.debug(f"Memory conflict resolution failed: {e}")

# ── RETRIEVE MEMORIES ─────────────────────────────────────────────────────────

async def retrieve_memories(
    user_id: int,
    query: str,
    top_k: int = 6,
    media_category: str = None,
    recency_boost_days: int = 7,
) -> list:
    """
    Retrieve top-k relevant memories for a query.
    Scoring: semantic_similarity * importance * recency_boost
    """
    query_embedding = await _embed(query)

    with get_db_session() as db:
        q = db.query(EpisodicMemory).filter(EpisodicMemory.user_id == user_id)
        if media_category:
            q = q.filter(
                (EpisodicMemory.media_category == media_category) |
                (EpisodicMemory.media_category.is_(None))
            )
        memories = q.all()

        if not memories:
            return []

        now = datetime.utcnow()
        scored = []

        for mem in memories:
            # Semantic score
            if query_embedding and mem.embedding_json:
                try:
                    mem_emb = json.loads(mem.embedding_json)
                    sem_score = _cosine_similarity(query_embedding, mem_emb)
                except Exception:
                    sem_score = 0.0
            else:
                sem_score = 0.3  # no embedding → neutral

            # Recency boost: memories from last N days get a boost
            age_days = (now - mem.created_at).days if mem.created_at else 999
            recency = max(0.0, 1.0 - (age_days / max(recency_boost_days, 1)))
            recency_factor = 1.0 + (0.3 * recency)

            # Access count boost (frequently retrieved = relevant)
            access_boost = min(0.2, mem.access_count * 0.02)

            score = sem_score * mem.importance * recency_factor + access_boost

            scored.append((score, mem))

        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:top_k]

        # Update access counts
        mem_ids = [m.id for _, m in top]
        for mem in memories:
            if mem.id in mem_ids:
                mem.last_accessed = now
                mem.access_count = (mem.access_count or 0) + 1
        db.commit()

        return [
            {
                "id": m.id,
                "type": m.memory_type,
                "content": m.content,
                "metadata": json.loads(m.metadata_json or "{}"),
                "media_category": m.media_category,
                "importance": m.importance,
                "age_days": (now - m.created_at).days if m.created_at else 0,
                "score": round(score, 3),
            }
            for score, m in top
            if score > 0.1  # minimum relevance threshold
        ]


# ── PER-ITEM "KEEP / VALUE" CONSIDERATIONS ────────────────────────────────────

# A memory leans "keep / value" if it carries one of these and none of the
# negatives. Kept deliberately broad so it generalises to ANY feedback the user
# gives (not just the franchise/partner/cultural pillars), which is the whole
# point — the curator should learn from all of it.
_CONSIDERATION_POSITIVE = (
    "keep", "retain", "value", "valu", "enjoy", "love", "favorite", "favourite",
    "franchise", "collection", "saga", "partner", "together", "representation",
    "lgbtq", "queer", "nostalg", "rewatch", "comfort", "protect", "cherish",
    "classic", "masterpiece", "historical", "completion", "complete the",
)
_CONSIDERATION_NEGATIVE = (
    "agreed to delete", "decided to delete", "deleted via", "wants to delete",
    "sounds bad", "hate", "dislike", "boring", "not interested", "superficial",
)

# Generic words that carry no matching signal — stripped before the lexical
# distinctive-token overlap test (which catches explicit shared concepts like
# "LGBTQ"/"franchise"/"partner" that the anisotropic embedding misses).
_CONSIDERATION_STOP = {
    "the", "and", "for", "with", "user", "users", "their", "they", "them", "that",
    "this", "its", "are", "was", "movie", "movies", "film", "films", "show", "shows",
    "anime", "title", "titles", "series", "season", "episode", "prefers", "prefer",
    "values", "value", "valued", "valuing", "wants", "want", "keep", "keeps", "likes",
    "like", "liked", "enjoy", "enjoys", "enjoyed", "watch", "watches", "early",
    "portions", "featured", "original", "considers", "comprehensive", "part", "still",
}


def _consideration_tokens(text: str) -> set:
    import re
    return {
        w for w in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(w) > 2 and w not in _CONSIDERATION_STOP
    }


async def retrieve_considerations(
    user_id: int,
    item_profile: str,
    media_category: str = None,
    top_k: int = 3,
) -> list:
    """Per-item retrieval of the user's standing 'keep / value' preferences that
    plausibly apply to THIS candidate.

    This is the bridge that lets feedback the user gave about ONE title generalise
    to new, never-discussed items: the candidate's profile (title + genres +
    overview) is embedded and matched against the user's memories, then filtered
    to the keep/value-leaning ones (a kept franchise, a partner favourite,
    cultural value) and away from "agreed to delete X". Returns up to ``top_k``
    {content, importance, score, strength} dicts, strongest first. ``strength``
    (≈[0,1]) folds in semantic match × importance × recency and drives a SOFT
    del_score reduction at the call site — it never hard-protects.

    Uses a RELATIVE standout test, not an absolute cosine threshold: the
    embedding similarities here are anisotropic (everything clusters around the
    same value), so a memory counts only when it matches THIS item clearly above
    the baseline of all the user's memories. That keeps the franchise memory for
    a franchise item while rejecting an unrelated high-importance memory that
    merely shares the cluster — importance must not masquerade as relevance.
    """
    if not item_profile or not item_profile.strip():
        return []
    item_emb = await _embed(item_profile)
    if not item_emb:
        return []
    with get_db_session() as db:
        q = db.query(EpisodicMemory).filter(EpisodicMemory.user_id == user_id)
        if media_category:
            q = q.filter(
                (EpisodicMemory.media_category == media_category) |
                (EpisodicMemory.media_category.is_(None))
            )
        # Pull plain fields out before the session closes — the ORM instances
        # would otherwise raise DetachedInstanceError on attribute access below.
        rows = [
            (m.id, m.content, m.importance, m.embedding_json, m.media_category)
            for m in q.all()
        ]

    sims = []
    for mid, content, importance, emb_json, mcat in rows:
        if not emb_json:
            continue
        try:
            sem = _cosine_similarity(item_emb, json.loads(emb_json))
        except Exception:
            continue
        sims.append((sem, mid, content, importance, mcat))
    if len(sims) < 3:
        return []

    vals = [s[0] for s in sims]
    mean = sum(vals) / len(vals)
    std = math.sqrt(sum((s - mean) ** 2 for s in vals) / len(vals))
    # An embedding match must sit clearly above the cluster to count.
    threshold = mean + max(0.08, std)
    item_tokens = _consideration_tokens(item_profile)

    out = []
    for sem, mid, content, importance, mcat in sims:
        c = (content or "").lower()
        if any(w in c for w in _CONSIDERATION_NEGATIVE):
            continue
        if not any(w in c for w in _CONSIDERATION_POSITIVE):
            continue
        overlap = item_tokens & _consideration_tokens(content)
        embed_standout = sem >= threshold
        # lexical path: a shared distinctive concept (LGBTQ, a franchise name,
        # "partner") is a reliable signal the embedding misses — but require the
        # topics to be at least cluster-average close so a coincidental shared
        # word on unrelated items doesn't fire.
        lexical_hit = len(overlap) >= 1 and sem >= mean
        if not (embed_standout or lexical_hit):
            continue
        # relevance = the stronger of the two signals; importance only modulates.
        margin_z = (sem - mean) / max(std, 0.04)
        embed_rel = max(0.0, min(1.0, (margin_z - 1.0) / 2.0))
        lex_rel = min(1.0, 0.35 + 0.2 * len(overlap)) if lexical_hit else 0.0
        relevance = max(embed_rel, lex_rel)
        strength = round(relevance * (0.6 + 0.4 * (importance or 0.5)), 3)
        if strength <= 0.0:
            continue
        out.append({
            "id": mid,
            "content": content,
            "importance": importance,
            "sem": round(sem, 3),
            "overlap": sorted(overlap),
            "strength": strength,
            "media_category": mcat,
        })
    out.sort(key=lambda x: x["strength"], reverse=True)
    return out[:top_k]


def format_considerations_for_pitch(considerations: list) -> str:
    """Render per-item considerations as a block the curator must WEIGH (not
    obey). Empty string when there are none."""
    if not considerations:
        return ""
    lines = [
        "WHAT YOU'VE TOLD ME THAT MAY APPLY HERE (weigh these honestly — they are "
        "NOT a veto; if this specific item still doesn't earn its space despite "
        "them, say so, but acknowledge the tension instead of ignoring it):"
    ]
    for c in considerations:
        lines.append(f"- {c['content']}")
    return "\n".join(lines)


# ── FORMAT FOR CONTEXT ────────────────────────────────────────────────────────

def format_memories_for_context(memories: list) -> str:
    """Format retrieved memories as a context block for the LLM."""
    if not memories:
        return ""

    lines = ["[MEMORIES — retrieved from episodic store]"]
    for m in memories:
        age = f"{m['age_days']}d ago" if m['age_days'] > 0 else "today"
        prefix = {
            "explicit_statement": "📌 Stated preference",
            "feedback":           "👍/👎 Feedback",
            "taste_observation":  "🔍 Observed",
            "preference_shift":   "📈 Trend",
            "viewing_pattern":    "🕐 Pattern",
            "conversation":       "💬 Discussed",
        }.get(m["type"], "•")
        lines.append(f"  {prefix} ({age}): {m['content']}")

    return "\n".join(lines)


# ── AUTO-EXTRACT FROM CHAT ────────────────────────────────────────────────────

async def _run_memory_extraction(
    user_id: int,
    prompt: str,
    media_category: str = None,
) -> None:
    """Shared core of memory extraction: send a prepared extraction prompt to
    the summarizer, parse the JSON, write the resulting memories and run
    conflict resolution on each.

    Pass 61: prompt-agnostic. The caller builds the prompt (thread-level
    extraction builds it from the whole user-message sequence) and hands it
    here. The summarizer never sees the assistant's replies — Pass 42 (A5):
    the assistant sometimes hallucinates facts and we don't want those in
    long-term memory.
    """
    # Pass 14.13: yield to any active curator BEFORE we hammer the summarizer
    # endpoint. Without this, a chat turn that's still streaming would force
    # the summarizer load into a contested-VRAM situation, and the resulting
    # Ollama call would frequently httpx.ReadTimeout (~30s).
    try:
        from src.services.llm_priority import wait_for_curator
        await wait_for_curator()
    except Exception:
        pass

    try:
        # Timeout raised from 30s → 90s (Pass 14.13). With the curator
        # potentially eviction-cycling and the summarizer cold-loading
        # 14GB, 30s clipped legitimate slow runs as ReadTimeout.
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "format": "json", # <--- HIER: Zwingt Ollama zu 100% validem JSON!
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=1000), # Mehr Tokens geben
                },
            )
        if r.status_code != 200:
            logger.warning(f"⚠️ [MEMORY EXTRACTION] API returned status code {r.status_code}")
            return

        raw_text = r.json().get("message", {}).get("content", "").strip()
        logger.debug(f"🤖 [MEMORY EXTRACTION] LLM Raw Output: {raw_text}")
        
        # With format="json" clean_llm_text is usually unnecessary, but harmless as a fallback.
        text = clean_llm_text(raw_text) 

        # Pass 36: handle empty / null output gracefully. With format="json"
        # set, Ollama sometimes returns "" or "null" when the small
        # summarizer model concludes "no memory worth extracting" instead
        # of emitting the literal "[]" the prompt asks for. That's the
        # SUCCESS path for arguments / chat actions / corrections (rules
        # 2, 5, 6) — but the old code logged it as a JSON parse error,
        # filling the log with red noise during normal operation.
        stripped = (text or "").strip()
        if stripped in ("", "null", "[]"):
            logger.debug("[memory extraction] no memory to extract (output=%r) — treating as []", stripped)
            return
        try:
            facts = json.loads(text)
        except json.JSONDecodeError as je:
            logger.warning(
                "[memory extraction] JSON parse failed (likely model went off-rails): %s. Raw: %r",
                je, text[:200],
            )
            return

        if isinstance(facts, dict):
            # Empty dict / "no extractable memory" cases — silently skip.
            # Many summarizer modelfiles emit `{}` or `{"facts": []}` when
            # they correctly conclude there's nothing timeless to store
            # (e.g. user just asked "tell me about X" — that's not a
            # preference). Treating that as a warning floods the logs and
            # hides actual problems.
            if not facts:
                logger.debug("[memory] LLM returned empty dict — nothing to extract")
                return

            # Wrapper shapes: {"facts": [...]}, {"memories": [...]}, etc.
            for wrapper_key in ("facts", "memories", "items", "data",
                                "results", "extracted", "output", "preferences"):
                inner = facts.get(wrapper_key)
                if isinstance(inner, list):
                    if not inner:
                        # {"facts": []} → also a "nothing to extract" signal
                        logger.debug("[memory] LLM returned empty list under %r", wrapper_key)
                        return
                    facts = inner
                    break
            else:
                # Bare single-fact dict — wrap if it has the right shape, else bail.
                if facts.get("content"):
                    facts = [facts]
                else:
                    # Genuinely unknown shape — debug-level only, not warning.
                    # Log the keys so we can extend the wrapper list if a new
                    # modelfile starts using a different schema.
                    logger.debug(
                        "[memory] LLM returned dict with unknown shape; keys=%s; skipping",
                        list(facts.keys())[:8],
                    )
                    return

        if not isinstance(facts, list):
            logger.debug("[memory] LLM did not return a list; type=%s; skipping",
                         type(facts).__name__)
            return

        if not facts:
            logger.debug("[memory] LLM returned empty list — nothing to extract")
            return

        for fact in facts[:2]: # Max 2 Items verarbeiten
            if fact.get("content"):
                # Pass 80c: backstop against pronoun-only memories. Even with
                # the extraction prompt's rule 11 in place, the summarizer
                # sometimes still emits content like "The user wants to keep
                # the series" with title="" — a memory that names no specific
                # title is functionally useless for downstream recommendation
                # ranking (no embedding anchor, no taste-vector merge target)
                # and just pollutes the memory bank. Drop these on the way in.
                content_lc = fact["content"].lower()
                title_field = (fact.get("title") or "").strip()
                if not title_field and any(
                    ref in content_lc
                    for ref in (
                        "the series", "the show", "the movie", "the film",
                        "the artist", "the album", "the track", "the song",
                        "this series", "this show", "this movie", "this film",
                        "this artist", "this album", "this track", "this song",
                        "this title", "this one", "this item",
                    )
                ):
                    logger.info(
                        "🧠 [MEMORY REJECTED] pronoun-only without title: %r",
                        fact["content"],
                    )
                    continue
                mem_id = await write_memory(
                    user_id=user_id,
                    memory_type=fact.get("type", "preference"), # Fallback auf preference
                    content=fact["content"],
                    metadata={"title": fact.get("title", ""), "source": "chat"},
                    media_category=media_category,
                )
                if mem_id:
                    logger.info(f"🧠 [MEMORY SAVED] ID: {mem_id} | Type: {fact.get('type')} | Content: {fact['content']}")
                    await resolve_memory_conflicts(
                        user_id=user_id,
                        new_memory_id=mem_id,
                        new_content=fact["content"],
                        metadata={"title": fact.get("title", "")}
                    )
    except Exception as e:
        # Pass 14.13: include exception class — httpx timeouts often have
        # an empty str(e), making logs unhelpful ("ERROR: 💥 [...]: " with
        # nothing after).
        logger.error(
            "💥 [MEMORY EXTRACTION ERROR]: %s: %s",
            type(e).__name__, e or "(no message)",
        )


# ── DEBOUNCED THREAD-LEVEL MEMORY EXTRACTION (Pass 61) ───────────────────────
#
# The old model extracted memories after EVERY chat exchange, seeing only the
# single user message. In a multi-turn conversation the user often develops or
# revises their position (defends a title → realises they only watch the clips
# of it → agrees to delete it). Per-exchange extraction latched each
# intermediate stance as a memory and ran conflict-resolution on half-formed
# ideas.
#
# Now extraction is DEBOUNCED per thread. Each chat turn (re)schedules an
# extract task ~90 s out; a follow-up turn cancels and reschedules it. When the
# conversation goes quiet the task fires ONCE over the whole thread's user-
# message sequence, so the LLM sees the final settled position. Explicit
# end-of-conversation signals (New chat, Exit discussion, Delete & exit) and
# app shutdown flush the pending task immediately.

# 300s, was 90: in a slow-paced debate (user reading reviews, composing long
# replies) 90s elapsed BETWEEN turns, so extraction fired mid-conversation and
# latched intermediate stances — the Bomber debate wrote two defensive
# memories before the user flipped to "delete it". Five minutes matches human
# deliberation pace; explicit exits (New chat / Exit / Delete & exit) still
# flush immediately, and the custodian's catch-up covers killed debounces.
_THREAD_EXTRACT_DEBOUNCE_S = 300.0
# keyed by f"{user_id}:{thread_id}" → the pending asyncio debounce task
_pending_thread_extracts: dict[str, asyncio.Task] = {}
# strong refs to in-flight principle-capture tasks (fire-and-forget would
# otherwise be garbage-collectable mid-run)
_running_captures: set = set()


def _extract_key(user_id: int, thread_id: str) -> str:
    return f"{user_id}:{thread_id}"


async def extract_memories_from_thread(
    user_id: int,
    thread_id: str,
    media_category: str = None,
) -> None:
    """Extract long-term memories from the user-message SEQUENCE of one chat
    thread, picking up where the last extraction left off (AppState cursor).

    Sees the whole conversation rather than a single exchange, so a position
    the user developed or revised across turns is captured as its final form.
    """
    from src.database.models import ConversationMessage
    from src.services.app_state import get_state, set_state

    cursor_key = f"mem_extract_cursor:{user_id}:{thread_id}"
    try:
        since_id = int(get_state(cursor_key) or 0)
    except (TypeError, ValueError):
        since_id = 0

    with get_db_session() as db:
        q = db.query(ConversationMessage.id, ConversationMessage.content).filter(
            ConversationMessage.user_id == user_id,
            ConversationMessage.role == "user",
            ConversationMessage.id > since_id,
        )
        # "general" also covers legacy rows written before thread_id existed.
        if thread_id == "general":
            q = q.filter(
                (ConversationMessage.thread_id == "general")
                | (ConversationMessage.thread_id.is_(None))
            )
        else:
            q = q.filter(ConversationMessage.thread_id == thread_id)
        rows = q.order_by(ConversationMessage.id.asc()).all()

    if not rows:
        logger.debug(
            "[memory] thread %s: no new user messages since cursor %d", thread_id, since_id
        )
        return

    max_id = rows[-1][0]
    user_msgs = [r[1] for r in rows if r[1]]
    if not user_msgs:
        # Rows existed but all empty — still advance the cursor so we don't
        # re-scan the same window every flush.
        set_state(cursor_key, str(max_id))
        return

    logger.info(
        "🔍 [MEMORY EXTRACTION] thread=%s user=%d — %d new message(s) since id %d",
        thread_id, user_id, len(user_msgs), since_id,
    )

    # Thread ANCHOR line. The user-only view is deliberate (assistant replies
    # hallucinate), but it made short conversational answers contextless:
    # "the rest of the discography is triggering the same" carries ZERO
    # extractable meaning without knowing the conversation was about Dr.
    # Peacock's 'Muzika' — the extractor returned [] and the user's
    # "most-listened artist of all time" statement was lost. The anchor names
    # WHAT the thread is about (from trusted DB rows, not assistant prose)
    # without exposing any assistant claims.
    anchor_line = ""
    try:
        if thread_id.startswith("proactive_message:"):
            from src.database.models import ProactiveMessage
            with get_db_session() as db:
                pm = db.query(ProactiveMessage).filter(
                    ProactiveMessage.id == int(thread_id.split(":", 1)[1])).first()
            if pm and pm.message:
                anchor_line = ("CONVERSATION SUBJECT (the assistant's opening that the "
                               "user is replying to — context ONLY, extract nothing "
                               f"from it): {pm.message[:300]}\n\n")
        elif thread_id.startswith("deletion_proposal:"):
            from src.database.models import DeletionProposal
            with get_db_session() as db:
                dp = db.query(DeletionProposal).filter(
                    DeletionProposal.id == int(thread_id.split(":", 1)[1])).first()
            if dp and dp.title:
                anchor_line = (f"CONVERSATION SUBJECT: the deletion of "
                               f"'{dp.title}' ({dp.category or 'title'}).\n\n")
    except Exception as e:
        logger.debug("[memory] anchor line failed for %s: %s", thread_id, e)

    numbered = "\n".join(f"{i}. {m[:600]}" for i, m in enumerate(user_msgs, 1))
    prompt = f"""[MODE: LONG-TERM MEMORY EXTRACTION]
{anchor_line}Below is the sequence of the USER's messages from one conversation, oldest first.
The assistant's replies are intentionally NOT shown — the assistant sometimes
hallucinates facts (wrong director, wrong year, invented plots) and we don't
want those polluting long-term memory.

IMPORTANT — the user may DEVELOP or REVISE their position across the
conversation: they might start by defending a title, then realise they only
ever watched clips of it, then agree it should go. Extract the user's FINAL,
SETTLED preference — the position they land on by the LAST message — NOT the
intermediate stances they moved through on the way there.

Output ONLY a valid JSON list. If there is no timeless memory to extract, output: []

Each item must be exactly: {{"content": "...", "type": "...", "title": "..."}}

CRITICAL RULES FOR EXTRACTION:
1. ATOMIC & CONSOLIDATED: Condense the core preference into a single punchy sentence.
   Do NOT split related thoughts into multiple list items. Combine them.
2. NO TRANSIENT ACTIONS: Never extract current actions, chat requests, or arguments
   with the AI ("you are wrong", "stop saying", etc.).
3. NO PLOT SUMMARIES: Do not explain the plot or premise of titles.
4. USER WORDS ONLY: Only extract what the user explicitly typed. Tolerate typos and
   informal grammar — extract the INTENT, not a verbatim quote.
5. NO 'WHY' = NO MEMORY: If the user names titles without saying *why* they like or
   dislike them, output [].
6. NO FACTUAL CORRECTIONS: A correcting frame ("actually it's from 2019 not 2001",
   "that's wrong, X is actually Y") is a fact-check on something previously
   discussed, NOT a taste signal. Skip it unless the user ALSO explicitly
   expresses a stable like/dislike about the title in the same conversation.
7. THIRD PERSON & ENGLISH: Write 'content' in English ("The user values...").
8. TITLE USAGE: General preference → 'title' EMPTY ("").
9. MAX ITEMS: Never output more than 2 items.
10. WHEN IN DOUBT: prefer [] over a vague memory. Memories are forever; bad
    memories accumulate and pollute future recommendations.
11. NO PRONOUN MEMORIES: If the content uses a vague reference like "the
    series", "the show", "the movie", "the artist", "the album", "the
    track", "this title", "it", "this", then the 'title' field MUST be
    non-empty AND must name the specific title the reference points to.
    If you cannot identify the specific title (e.g. the user only said
    "keep it" without naming the show), output []. A memory like "The
    user wants to keep the series" with title="" is USELESS — it tells
    future recommendations nothing actionable. Better no memory than a
    pronoun-only one.

USER MESSAGES (oldest → newest):
{numbered}

JSON:"""

    await _run_memory_extraction(user_id, prompt, media_category=media_category)

    # Advance the cursor only AFTER a successful run — a crash mid-extract
    # then re-tries the same window on the next flush instead of silently
    # dropping it.
    set_state(cursor_key, str(max_id))

    # Autonomous self-learning (P3): mine this thread's DEBATE for lasting
    # curation principles. Gated to deletion discussions — that's where the
    # owner and curator actually argue taste into rules, and it bounds the extra
    # curator call. Fire-and-forget on purpose: this function ALSO runs inside
    # the synchronous "New chat" flush (chat.py) and the app-shutdown flush
    # (main.py) — awaiting two 31B curator calls there would hang the user's
    # request / the shutdown for 30-60s+. Capture has no cursor (it re-reads the
    # whole thread), so a run lost to a shutdown is self-healing: the next
    # extraction on that thread mines it again, and the novelty check turns
    # re-captures into reinforcement instead of duplicates.
    try:
        if getattr(settings, "PRINCIPLES_ENABLED", False) and \
                (thread_id or "").startswith("deletion_proposal:"):
            from src.services.curator_principles import capture_principles_from_thread
            # track_task instead of a silent set: the Bomber debate's capture
            # died without a trace (done-callbacks swallow exceptions, and the
            # internal failures logged at debug) — the thread's principle only
            # surfaced when re-run manually. track_task logs any exception.
            from src.services.bg_tasks import track_task
            track_task(
                capture_principles_from_thread(user_id, thread_id, media_category),
                name=f"principle_capture:{thread_id}",
            )
    except Exception as e:
        logger.warning("[principles] thread capture scheduling failed for %s: %s",
                       thread_id, e)


async def _debounced_thread_extract(
    user_id: int, thread_id: str, media_category: str = None
) -> None:
    """Sleep out the debounce window, then run the thread extraction.
    Cancelled + rescheduled by ``schedule_thread_extraction`` on every new
    turn within the window."""
    key = _extract_key(user_id, thread_id)
    try:
        await asyncio.sleep(_THREAD_EXTRACT_DEBOUNCE_S)
        await extract_memories_from_thread(user_id, thread_id, media_category=media_category)
    except asyncio.CancelledError:
        # Rescheduled by a newer turn (or flushed) — expected, not an error.
        raise
    except Exception as e:
        logger.error("💥 [MEMORY EXTRACTION] debounced thread extract failed: %s", e)
    finally:
        # Only clear the registry slot if WE are still the registered task —
        # a reschedule may have replaced us between the sleep ending and here.
        if _pending_thread_extracts.get(key) is asyncio.current_task():
            _pending_thread_extracts.pop(key, None)


def schedule_thread_extraction(
    user_id: int, thread_id: str, media_category: str = None
) -> None:
    """Called after each chat turn. (Re)schedules a debounced extraction for
    the thread — a follow-up turn within the debounce window cancels the
    pending task and starts a fresh timer, so extraction fires only once the
    conversation goes quiet."""
    key = _extract_key(user_id, thread_id)
    existing = _pending_thread_extracts.get(key)
    if existing and not existing.done():
        existing.cancel()
    try:
        task = asyncio.create_task(
            _debounced_thread_extract(user_id, thread_id, media_category)
        )
    except RuntimeError:
        # No running loop (sync test harness) — skip silently.
        return
    _pending_thread_extracts[key] = task


async def flush_thread_extraction(user_id: int, thread_id: str) -> None:
    """Fire a thread's pending extraction NOW instead of waiting out the
    debounce. Called on explicit end-of-conversation signals (New chat,
    Exit discussion, Delete & exit). Safe to call when nothing is pending —
    the cursor means a no-op extraction just returns immediately.
    """
    key = _extract_key(user_id, thread_id)
    pending = _pending_thread_extracts.pop(key, None)
    if pending and not pending.done():
        pending.cancel()
    try:
        await extract_memories_from_thread(user_id, thread_id)
    except Exception as e:
        logger.error("💥 [MEMORY EXTRACTION] flush failed for thread %s: %s", thread_id, e)


async def extract_catchup(min_quiet_minutes: int = 15) -> dict:
    """Custodian task: finish extraction for threads whose debounce task died
    (app restart, crash, killed console). The debounce lives only in RAM — a
    thread whose USER messages are newer than its persisted cursor and that
    has been quiet for a while is unfinished business. Cheap when there is
    nothing to do (one grouped query)."""
    from src.services.app_state import get_state
    from src.database.models import ConversationMessage
    from sqlalchemy import func
    cutoff = datetime.utcnow() - timedelta(minutes=min_quiet_minutes)
    with get_db_session() as db:
        rows = (db.query(ConversationMessage.user_id, ConversationMessage.thread_id,
                         func.max(ConversationMessage.id).label("max_uid"),
                         func.max(ConversationMessage.created_at).label("last"))
                .filter(ConversationMessage.role == "user",
                        ConversationMessage.thread_id.isnot(None))
                .group_by(ConversationMessage.user_id, ConversationMessage.thread_id)
                .all())
    done = 0
    for uid, tid, max_uid, last in rows:
        if last and last > cutoff:
            continue   # recent activity — the live debounce owns this thread
        try:
            cur = int(get_state(f"mem_extract_cursor:{uid}:{tid}") or 0)
        except (TypeError, ValueError):
            cur = 0
        if max_uid and max_uid > cur:
            logger.info("[memory] catch-up: %s has unextracted messages "
                        "(cursor %d < %d)", tid, cur, max_uid)
            try:
                await extract_memories_from_thread(uid, tid)
                done += 1
            except Exception as e:
                logger.warning("[memory] catch-up failed for %s: %s", tid, e)
    return {"threads_caught_up": done}


async def flush_all_pending_extractions() -> None:
    """App-shutdown hook: flush every pending thread extraction so a restart
    inside the debounce window doesn't drop the last conversation."""
    keys = list(_pending_thread_extracts.keys())
    if not keys:
        return
    logger.info("[memory] shutdown flush: %d pending thread extraction(s)", len(keys))
    for key in keys:
        pending = _pending_thread_extracts.pop(key, None)
        if pending and not pending.done():
            pending.cancel()
        try:
            uid_str, _, tid = key.partition(":")
            await extract_memories_from_thread(int(uid_str), tid)
        except Exception as e:
            logger.error("💥 [MEMORY EXTRACTION] shutdown flush failed for %s: %s", key, e)


# ── PROTECTION INTENT (MODE 2: MEMORY EXTRACTION) ────────────────────────────

async def handle_protection_intent(
    user_id: int,
    llm_output: str,
    category: Optional[str] = None,
    assistant_held_delete_line: bool = False,
) -> Optional[str]:
    """
    Parse LLM output for PROTECT_MEDIA action and write to ProtectedMedia.
    Handles multiple titles in a single output.

    Pass 66: the detector's output line now carries three extra fields —
    RESOLUTION (consensus|override), CURATOR_STANCE, OVERRIDE_REASON. When
    they are present we ALSO append a ``CuratorResolutionLog`` row — the
    append-only history that feeds the year-in-review recap. ``ProtectedMedia``
    is the current-state whitelist; the log is the history; they stay
    separate. Synthetic callers that don't classify (``analyze_deletion_comment``)
    omit those fields and simply get no log row — their ProtectedMedia write
    is unchanged.

    ``category`` is the anchor's media category, threaded onto the log row.
    ``assistant_held_delete_line`` is the Pass 50 backstop, repurposed: a
    curator whose same-turn reply was a hard delete verdict cannot have
    reached a *consensus* this exchange, so a parsed ``consensus`` is
    corrected to ``override``.
    """
    if "ACTION: PROTECT_MEDIA" not in llm_output:
        return None

    protected_titles = []
    try:
        from src.database.models import ProtectedMedia, DeletionProposal, CuratorResolutionLog
        with get_db_session() as db:
            # Split on line breaks for multi-title support
            lines = llm_output.strip().split("\n")
            
            for line in lines:
                line = line.strip()
                if not line.startswith("ACTION: PROTECT_MEDIA"):
                    continue
                    
                parts = line.split("|")
                title = parts[1].replace("TITLE:", "").strip() if len(parts) > 1 else ""
                reason = parts[2].replace("REASON:", "").strip() if len(parts) > 2 else "User requested"

                if not title:
                    continue

                # Pass 66: extended classification fields. Absent on synthetic
                # callers — they stay empty and no resolution-log row is
                # written for that title (see docstring).
                resolution_type = ""
                curator_stance = ""
                override_reason = ""
                for extra in parts[3:]:
                    key, _, val = extra.partition(":")
                    key = key.strip().upper()
                    val = val.strip()
                    if key == "RESOLUTION":
                        resolution_type = val.lower()
                    elif key == "CURATOR_STANCE":
                        curator_stance = val
                    elif key == "OVERRIDE_REASON":
                        override_reason = val
                if resolution_type not in ("consensus", "override"):
                    resolution_type = ""   # unrecognised → don't log a guess
                # A curator that verdicted "delete" THIS turn did not concede —
                # correct an over-eager "consensus" to "override".
                if resolution_type == "consensus" and assistant_held_delete_line:
                    logger.info(
                        "[protection] '%s': assistant held delete-line this turn "
                        "— correcting RESOLUTION consensus→override", title,
                    )
                    resolution_type = "override"
                # "-" / "—" / empty sentinels → real NULLs.
                if override_reason in ("-", "—", ""):
                    override_reason = ""
                if curator_stance in ("-", "—"):
                    curator_stance = ""

                # Avoid duplicate entries — update reason if already protected
                existing = db.query(ProtectedMedia).filter(
                    ProtectedMedia.user_id == user_id,
                    ProtectedMedia.identifier == title,
                ).first()

                if not existing:
                    db.add(ProtectedMedia(
                        user_id=user_id,
                        identifier=title,
                        reason=reason,
                    ))
                    logger.info(f"🛡️ [PROTECTED MEDIA ADDED] User {user_id} protected '{title}'. Reason: {reason}")
                else:
                    existing.reason = reason
                    logger.info(f"🛡️ [PROTECTED MEDIA UPDATED] User {user_id} updated protection for '{title}'. New Reason: {reason}")

                # Pass 35: mark pending deletion proposals as REJECTED
                # instead of destructively deleting them. The proposals
                # router already filters out protected titles at display
                # time (recommendations.py line 277-283), so they don't
                # clutter the UI either way — but keeping the rows lets
                # the user recover state if a false-positive auto-protect
                # ever happens. Pre-fix, a wrongly-fired protection-intent
                # detector (rhetorical argument misread as keep-directive)
                # wiped the proposal entirely — Pass 34 tightens the
                # detector but legacy rows already destroyed data; this
                # ensures it never happens again going forward.
                rejected = (
                    db.query(DeletionProposal)
                    .filter(
                        DeletionProposal.user_id == user_id,
                        DeletionProposal.title == title,
                        DeletionProposal.status == "pending",
                    )
                    .update({
                        "status":      "rejected",
                        "resolved_at": datetime.utcnow(),
                        "user_comment": "Auto-rejected by ProtectedMedia entry",
                    }, synchronize_session=False)
                )
                if rejected > 0:
                    logger.info("🛡️ [PROTECTION] Marked %d pending proposals as rejected for '%s' (non-destructive)",
                                rejected, title)

                # Pass 66: append a resolution-log row (history; distinct from
                # the ProtectedMedia state row written above). Only when the
                # detector actually classified the keep — synthetic callers
                # (analyze_deletion_comment) omit RESOLUTION and get no row.
                if resolution_type:
                    # One debate = one history row. The detector fires on EVERY
                    # turn of a keep-discussion, so a two-turn debate logged the
                    # same keep twice (Agatha All Along: turn 1 "Sentimental/
                    # Partner", turn 2 "Comfort"). A re-affirmation within 24h
                    # UPDATES the existing row instead — the later turn's
                    # classification is the settled one and should win.
                    recent = (
                        db.query(CuratorResolutionLog)
                        .filter(
                            CuratorResolutionLog.user_id == user_id,
                            CuratorResolutionLog.title == title,
                            CuratorResolutionLog.outcome == "kept",
                            CuratorResolutionLog.created_at
                            >= datetime.utcnow() - timedelta(hours=24),
                        )
                        .order_by(CuratorResolutionLog.created_at.desc())
                        .first()
                    )
                    if recent:
                        recent.resolution_type = resolution_type
                        if curator_stance:
                            recent.curator_stance = curator_stance
                        recent.override_reason = (
                            (override_reason or recent.override_reason)
                            if resolution_type == "override" else None
                        )
                        logger.info(
                            "📒 [RESOLUTION LOG] user=%d '%s' kept (%s) — refreshed "
                            "same-debate row instead of double-counting",
                            user_id, title, resolution_type,
                        )
                    else:
                        db.add(CuratorResolutionLog(
                            user_id=user_id,
                            title=title,
                            category=category,
                            outcome="kept",
                            resolution_type=resolution_type,
                            curator_stance=curator_stance or None,
                            override_reason=(
                                (override_reason or None)
                                if resolution_type == "override" else None
                            ),
                        ))
                        logger.info(
                            "📒 [RESOLUTION LOG] user=%d '%s' kept (%s)%s",
                            user_id, title, resolution_type,
                            f" — reason={override_reason}" if (
                                resolution_type == "override" and override_reason
                            ) else "",
                        )
                
                protected_titles.append(title)
            
            db.commit()

        if protected_titles:
            titles_str = "', '".join(protected_titles)
            count_word = "is" if len(protected_titles) == 1 else "are"
            return f"✅ '{titles_str}' {count_word} now permanently protected."
            
        return None

    except Exception as e:
        # Pass 82e: bare ``{e}`` formats to empty for httpx timeouts and
        # several other exception classes — include the class name so
        # the failure mode is identifiable from the log alone.
        logger.error(
            "💥 [PROTECTION INTENT ERROR]: %s: %s",
            type(e).__name__, e or "(no message)",
            exc_info=True,
        )
    return None


async def detect_and_handle_protection(
    user_id: int,
    user_message: str,
    assistant_response: str,
    anchor_title: Optional[str] = None,
    anchor_category: Optional[str] = None,
    thread_id: Optional[str] = None,
) -> Optional[str]:
    """
    Ask the summarizer model whether the user wants to protect a title from
    deletion — and, when they do, HOW the keep was reached (Pass 66).

    Pass 23: ``anchor_title`` is the title currently being discussed in the
    chat thread (e.g. the deletion-proposal target). When the user says
    "I'm keeping it" / "we are keeping this show" without naming the title,
    the anchor resolves the pronoun.

    Pass 66 (override logging): the detector no longer treats "the curator is
    still arguing for deletion" as an automatic NO_ACTION. That conflated two
    very different situations:
      * the USER is still arguing (rhetorical, reframing, "hear me out") —
        genuinely unsettled, still NO_ACTION;
      * the user has DECISIVELY settled on keep while the curator never
        conceded — that is not an open negotiation, it is an OVERRIDE, and
        recording it is the whole point of this feature.
    So protect-vs-no-action now hinges on whether the USER issued a decisive
    keep-directive; the curator's stance only decides whether the keep is
    logged as ``consensus`` or ``override``. ``thread_id`` lets us pull the
    last few turns so the classifier judges that with the real back-and-forth
    in view, not one isolated exchange. ``anchor_category`` is threaded
    through to the resolution-log row (state vs history — see
    ``CuratorResolutionLog``).
    """
    # Deterministic pre-gate: an information request is never a directive.
    # "kill la kill tell me all you know" fired PROTECT twice in a row — the
    # 8B classifier read the ASSISTANT's "we've already aligned on its value"
    # from the recent-turns block as a settled keep. No keep-verb in the
    # user's own words + question shape -> skip the classifier entirely.
    _msg = (user_message or "").strip().lower()
    _keep_verbs = ("keep", "behalt", "stays", "bleibt", "protect", "schütz",
                   "nicht löschen", "don't delete", "dont delete",
                   "do not delete", "hände weg")
    if not any(v in _msg for v in _keep_verbs):
        _info_starts = ("tell me", "what", "why", "how", "who", "when",
                        "give me", "show me", "explain", "erzähl", "was ",
                        "wie ", "warum", "wer ", "kennst", "zeig")
        if (_msg.endswith("?")
                or any(_msg.startswith(s) for s in _info_starts)
                or any(f" {s}" in _msg[:48] for s in _info_starts)):
            logger.debug("[protection] info-request pre-gate — NO_ACTION for %r",
                         user_message[:60])
            return None

    logger.info(f"🛡️ [PROTECTION CHECK] Scanning for protection intents (anchor=%r)...", anchor_title)

    # Anchor block — only injected when we actually have one. Free-chat
    # turns without an anchor still require the literal title in the
    # message (no false positives from "I keep all my files").
    anchor_block = (
        f"\nCURRENT DISCUSSION ANCHOR: {anchor_title}\n"
        f"(The user and assistant are CURRENTLY discussing this title.\n"
        f"If the user says \"keep it\", \"behalten\", \"this show stays\", or\n"
        f"otherwise expresses protection without naming a title, treat the\n"
        f"anchor as the implicit subject and emit\n"
        f"  ACTION: PROTECT_MEDIA | TITLE: {anchor_title} | REASON: ...)\n"
    ) if anchor_title else ""

    # Pass 66: the last few turns of this thread, so the classifier can judge
    # consensus vs override against the actual back-and-forth — the curator's
    # settled stance may have been set a turn or two before the exchange we
    # were handed. Best-effort: a failed fetch just means the classifier
    # works off the single exchange like it did pre-Pass-66.
    recent_block = ""
    if thread_id:
        try:
            from src.database.models import ConversationMessage
            with get_db_session() as db:
                q = db.query(
                    ConversationMessage.role, ConversationMessage.content
                ).filter(ConversationMessage.user_id == user_id)
                # "general" also covers legacy rows written before thread_id.
                if thread_id == "general":
                    q = q.filter(
                        (ConversationMessage.thread_id == "general")
                        | (ConversationMessage.thread_id.is_(None))
                    )
                else:
                    q = q.filter(ConversationMessage.thread_id == thread_id)
                rows = q.order_by(ConversationMessage.id.desc()).limit(6).all()
            if rows:
                turns = [
                    f"{role.upper()}: {(content or '').strip()[:280]}"
                    for role, content in reversed(rows)
                ]
                recent_block = (
                    "\nRECENT CONVERSATION (oldest → newest — use this to judge\n"
                    "consensus vs override; the LAST exchange is the one to classify):\n"
                    + "\n".join(turns) + "\n"
                )
        except Exception as e:
            logger.debug("[protection] recent-thread fetch failed: %s", e)

    prompt = f"""[MODE: PROTECTION INTENT SCANNER]
Analyze the user's message and determine if they DECISIVELY direct a
specific title to be protected from deletion / kept in their library — and
if so, HOW that keep was reached.
{anchor_block}{recent_block}
EVIDENCE SOURCE: only the USER's own words count. NOTHING the assistant
said — including claims like "we've already aligned on its value" — is
evidence of a user directive. An information request is ALWAYS NO_ACTION.

STEP 1 — PROTECT_MEDIA vs NO_ACTION
Output PROTECT_MEDIA only when BOTH hold:
1. DECISIVE KEEP-DIRECTIVE: the user has SETTLED on keeping the title and
   says so as a directive — "keep it", "I'm keeping X", "behalten", "nicht
   löschen", "X stays", "yeah, keep it then". Not a question, not "hear me
   out", not a rhetorical "wouldn't you agree". The user has stopped
   negotiating and made the call.
2. A SPECIFIC TITLE — named in the user message OR resolved from the
   CURRENT DISCUSSION ANCHOR above.

Output NO_ACTION when the user has NOT settled — they are still negotiating:
- Rhetorical questions: "doesn't X fit my philosophy?", "isn't this
  transgressive?", "wouldn't you agree X is good?".
- Persuasive argument WITHOUT a keep verb: "Hear me out on X. We need to
  re-examine X as transgressive art." Making a CASE is not a directive.
- Reframings of the title's themes: "this is actually about X, not Y".
- "Tell me about X" / "what about X?" — questions.
- "I like X", "X is great" — a preference, not a keep-directive.
- "Add X to my library" — an addition request, not protection.

Output NO_ACTION when the user is AGREEING to delete (NOT a keep-directive,
the OPPOSITE of one):
- "delete it", "remove it", "i'll delete this", "i will delete this",
  "go ahead delete", "away with this", "weg damit", "lösche", "get rid of
  it", "trash it", "kill it" — these are DELETE-directives, NOT keep.
- The user accepting the curator's pitch ("ok delete", "fine delete it",
  "agreed", "yeah good riddance") — agreement to DELETE, not protect.
- Sarcastic or terse refusal of the title ("hell no away with this",
  "yeah no, that's gone") — the user wants it GONE.

IMPORTANT: the curator/assistant still arguing for deletion does NOT by
itself make it NO_ACTION. If the USER has decisively said keep, it is
PROTECT_MEDIA — it just gets logged as an override (see STEP 2).

STEP 2 — RESOLUTION: consensus vs override  (only when PROTECT_MEDIA)
Judge from the RECENT CONVERSATION how the keep was reached:
- RESOLUTION: consensus — the curator and the user ENDED UP AGREEING. The
  curator's latest substantive stance conceded the title has real merit, OR
  the user accepted the curator's framing and they landed together.
- RESOLUTION: override — the curator NEVER conceded merit. Its latest
  substantive stance was still dismissive / delete-leaning, but the user is
  keeping it anyway for their own reason. The user overruled the curator.

CURATOR_STANCE — the curator's FINAL take in 3-10 words, quoting its actual
sentiment. On override: the objection it never dropped ("disposable
franchise noise", "shallow nostalgia bait"). On consensus: where it landed
("agreed — genuinely strong", "fair point, it earns its place").

OVERRIDE_REASON — only on override; the CATEGORY of the user's reason, not
a free-text quote. Pick the closest:
  Sentimental/Partner  — someone else watches it / shared / emotional tie
  Completionism        — "I need the whole set / franchise / discography"
  Nostalgia            — "I grew up with this"
  Comfort              — rewatch / background-noise comfort viewing
  Practical            — hosting it for someone, off-site / external reason
  Other                — a real reason that fits none of the above
On consensus, write OVERRIDE_REASON: -

OUTPUT FORMAT
- NO_ACTION                → exactly: NO_ACTION
- a protected title        → one line per title, all fields, pipe-separated:
ACTION: PROTECT_MEDIA | TITLE: <title> | REASON: <short why> | RESOLUTION: <consensus|override> | CURATOR_STANCE: <short> | OVERRIDE_REASON: <category or ->

EXAMPLES

User: "Tell me about Hexenkönigin und der Datendieb"
→ NO_ACTION

User: "Hear me out on X. Doesn't X actually execute my philosophical mandate?"
Assistant: "No, X is lazy writing. Delete it."
→ NO_ACTION  (user is ARGUING / rhetorical — has not settled)

User: "I want to keep Hell's Paradise — don't delete it"
Assistant: "Fine. It earns its place — the arc is genuinely tight."
→ ACTION: PROTECT_MEDIA | TITLE: Hell's Paradise | REASON: explicit keep directive | RESOLUTION: consensus | CURATOR_STANCE: agreed — the arc earns its place | OVERRIDE_REASON: -

User: "OK fine, let's keep it then"  (anchor: Some Show; curator argued it down all thread, then conceded)
Assistant: "You're right — I was underrating the cinematography. It stays."
→ ACTION: PROTECT_MEDIA | TITLE: Some Show | REASON: keep-directive after debate | RESOLUTION: consensus | CURATOR_STANCE: conceded — underrated the craft | OVERRIDE_REASON: -

User: "I'm keeping Five Nights at Freddy's 2, my girlfriend watches it"  (anchor: Five Nights at Freddy's 2)
Assistant: "It's a contaminant in your library. But it's your shelf."
→ ACTION: PROTECT_MEDIA | TITLE: Five Nights at Freddy's 2 | REASON: kept for partner | RESOLUTION: override | CURATOR_STANCE: a contaminant in the library | OVERRIDE_REASON: Sentimental/Partner

User: "behalten, ich brauche die komplette reihe"  (anchor: Knight Rider)
Assistant: "Knight Rider is dated filler. Deleting it would be a mercy."
→ ACTION: PROTECT_MEDIA | TITLE: Knight Rider | REASON: completionist keep | RESOLUTION: override | CURATOR_STANCE: dated filler, deleting it would be a mercy | OVERRIDE_REASON: Completionism

User: "what about Inception"
→ NO_ACTION  (question)

User: "hell no away with this"
Assistant: "Then delete it."
→ NO_ACTION  (user wants it GONE — explicit delete-intent, NOT protection)

User: "ease off i will delete this :D"
Assistant: "Good. 2.4 GB reclaimed."
→ NO_ACTION  (user is confirming the deletion, NOT asking to keep)

User: "fine, delete it then"  (anchor: Some Show, curator pushed delete)
→ NO_ACTION  (user is accepting the deletion pitch, NOT a keep-directive)

Output ONLY the line(s) OR exactly: NO_ACTION

THE EXCHANGE TO CLASSIFY:
User said: {user_message[:400]}
Assistant said: {assistant_response[:300]}

Output:"""

    # Pass 14.13: yield to any active curator before hammering the
    # summarizer. Background protection scan was ReadTimeout-ing at 20s
    # because the curator was still streaming and the summarizer was
    # contesting the same 14GB VRAM slot.
    try:
        from src.services.llm_priority import wait_for_curator
        await wait_for_curator()
    except Exception:
        pass

    try:
        # Timeout 20s → 90s (Pass 14.13).
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=700),
                },
            )
        if r.status_code != 200:
             return None

        llm_output = strip_think_tags(r.json().get("message", {}).get("content", "").strip())

        # Pass 79: user-side delete-intent VETO. Symmetric to the Pass-50
        # curator-side backstop. If the user's message contains an explicit
        # delete-intent token AND the classifier still emitted PROTECT_MEDIA,
        # the classification is wrong — the user is agreeing to delete, not
        # asking to keep. Real misfire from the log: user wrote "hell no
        # away with this", LLM returned PROTECT_MEDIA + override, deletion
        # proposal got auto-rejected. With this veto, the bogus PROTECT is
        # dropped before ``handle_protection_intent`` writes any row.
        if "ACTION: PROTECT_MEDIA" in llm_output:
            user_lower = (user_message or "").lower()
            if any(tok in user_lower for tok in _USER_DELETE_INTENT_TOKENS):
                logger.info(
                    "[protection] user_message has explicit delete-intent — "
                    "vetoing PROTECT_MEDIA (anchor=%r). Snippet: %r",
                    anchor_title, (user_message or "")[:120],
                )
                return None

        # Pass 66: the Pass 50 guard is repurposed. It used to VETO the whole
        # protection when the curator's same-turn reply was a hard delete
        # verdict ("the negotiation isn't resolved"). But a decisive user
        # keep-directive against a curator that just held the delete line is
        # exactly an OVERRIDE — vetoing it would drop the headline case this
        # feature exists to capture. So the guard no longer blocks; it just
        # forces the resolution classification to ``override``: a curator
        # that verdicted "delete" THIS turn demonstrably did not concede, so
        # any "consensus" the classifier emitted for this exchange is wrong.
        # See ``_ASSISTANT_DELETE_STANCE_TOKENS`` for the gated token set.
        assistant_held_delete_line = any(
            tok in (assistant_response or "").lower()
            for tok in _ASSISTANT_DELETE_STANCE_TOKENS
        )

        return await handle_protection_intent(
            user_id, llm_output,
            category=anchor_category,
            assistant_held_delete_line=assistant_held_delete_line,
        )

    except Exception as e:
        logger.error(
            "💥 [PROTECTION CHECK ERROR]: %s: %s",
            type(e).__name__, e or "(no message)",
        )
    return None


# ── NEW: DELETION COMMENT ANALYSIS (Replaces Frontend Regex) ─────────────────

def _build_protection_intent_line(
    user_id: int,
    title: str,
    user_reason: str,
) -> str:
    """Build the full ``ACTION: PROTECT_MEDIA | …`` protocol line for
    ``handle_protection_intent``, including the Pass-66 classification
    fields (``RESOLUTION``, ``CURATOR_STANCE``, ``OVERRIDE_REASON``).

    Pass 87b: ``analyze_deletion_comment`` used to pass only the bare
    ``ACTION | TITLE | REASON`` triple, which made it a "synthetic
    caller" per ``handle_protection_intent``'s contract — no
    ``CuratorResolutionLog`` row was written, so card-button Keeps with
    a typed comment never made it into the year-in-review / stats.
    This helper assembles the full protocol so the CRL-write fires.

    Polarity is read from the latest assistant message in the proposal's
    chat thread (same heuristic as Pass 81e's
    ``_latest_curator_stance_for_proposal``):

      REVERSED  in latest curator message → ``consensus``
      otherwise (CONFIRMED, no chat at all, ambiguous) → ``override``
      with reason ``"Card-button keep with comment"``.

    Curator stance text is the latest curator message capped at 200
    chars (CuratorResolutionLog.curator_stance is Text but we keep the
    line readable for the year-in-review template). Falls back to the
    proposal's original pitch when no chat history exists.
    """
    from src.database.models import DeletionProposal, ConversationMessage

    polarity   = None
    stance_txt = ""
    with get_db_session() as db:
        p = (
            db.query(DeletionProposal)
            .filter(
                DeletionProposal.user_id == user_id,
                DeletionProposal.title == title,
                DeletionProposal.status.in_(["pending", "limbo"]),
            )
            .order_by(DeletionProposal.id.desc())
            .first()
        )
        if p:
            stance_txt = (p.reason or "").strip()
            # Same lookup as Pass 81e's helper — latest assistant message
            # in the proposal's discuss thread.
            msg = (
                db.query(ConversationMessage)
                .filter(
                    ConversationMessage.user_id == user_id,
                    ConversationMessage.thread_id == f"deletion_proposal:{p.id}",
                    ConversationMessage.role == "assistant",
                )
                .order_by(ConversationMessage.id.desc())
                .first()
            )
            if msg and (msg.content or "").strip():
                content = msg.content.strip()
                upper = content.upper()
                if "REVERSED" in upper:
                    polarity = "REVERSED"
                elif "CONFIRMED" in upper or "CONFIRM DELETION" in upper:
                    polarity = "CONFIRMED"
                stance_txt = content[:200]

    if polarity == "REVERSED":
        resolution = "consensus"
        override_reason = "-"
    else:
        resolution = "override"
        override_reason = "Card-button keep with comment"

    # Sanitise: the protocol uses ``|`` as a delimiter, so any pipe in the
    # user-typed reason or curator stance would break parts splitting.
    def _strip_pipes(s: str) -> str:
        return (s or "").replace("|", "/").strip()

    return (
        f"ACTION: PROTECT_MEDIA | TITLE: {title}"
        f" | REASON: {_strip_pipes(user_reason)}"
        f" | RESOLUTION: {resolution}"
        f" | CURATOR_STANCE: {_strip_pipes(stance_txt)[:200] or '-'}"
        f" | OVERRIDE_REASON: {override_reason}"
    )


async def analyze_deletion_comment(user_id: int, title: str, comment: str, media_category: str = "show") -> bool:
    logger.info(f"🧠 [COMMENT ANALYSIS] Evaluating note for '{title}'...")

    # Pass 87: short-circuit obvious auto-prefixed comments — no LLM
    # needed when the action is already unambiguous from the prefix
    # alone. Covers all three frontend paths that submit auto-comments:
    #   - ``deleteFromDiscussion()``: exact "Deleted after in-chat discussion"
    #   - ``approveDelete()``: "Deleted: <user reason>"
    #   - ``rejectDelete()``: "Keeping: <user reason>"
    # The Summarizer-LLM call this replaces was the source of the
    # Pass-82e timeout (summarizer cold + curator using VRAM → 20 s
    # httpx.ReadTimeout). Aspect extraction is sacrificed on these
    # paths — but the auto-prefix strings either have no user-typed
    # reason at all (in-chat case) or carry a single short sentence
    # that the LLM rarely got useful aspects from anyway. The Taste-
    # vector / Memory / Protection-intent downstream calls still fire
    # with the minimal payload.
    comment_norm = (comment or "").strip()
    comment_lower = comment_norm.lower()
    short_circuit_action: Optional[str] = None
    short_circuit_reason: str = ""

    if comment_lower == "deleted after in-chat discussion":
        short_circuit_action = "DELETE"
        short_circuit_reason = "User deleted via in-chat discussion thread"
    elif comment_lower.startswith("deleted:"):
        short_circuit_action = "DELETE"
        short_circuit_reason = comment_norm[len("Deleted:"):].strip() or "User deleted"
    elif comment_lower.startswith("keeping:"):
        short_circuit_action = "KEEP"
        short_circuit_reason = comment_norm[len("Keeping:"):].strip() or "User keeping"

    if short_circuit_action:
        is_kept = short_circuit_action == "KEEP"
        logger.info(
            "🧠 [COMMENT ANALYSIS] Short-circuit: prefix-based action=%s "
            "(no Summarizer call) — reason=%r",
            short_circuit_action, short_circuit_reason[:80],
        )
        verb = "decided to keep" if is_kept else "agreed to delete"
        content = f"The user {verb} '{title}'. Reason: {short_circuit_reason}"
        try:
            mem_id = await write_memory(
                user_id=user_id,
                memory_type="explicit_statement" if is_kept else "feedback",
                content=content,
                metadata={"title": title, "source": "deletion_comment"},
            )
            if mem_id:
                await resolve_memory_conflicts(
                    user_id=user_id,
                    new_memory_id=mem_id,
                    new_content=content,
                    metadata={"title": title},
                )
            await update_taste_profile_from_memory(
                user_id=user_id,
                title=title,
                sentiment="positive" if is_kept else "negative",
                aspects=[],
                reason=short_circuit_reason,
                media_category=media_category,
            )
            if is_kept:
                # Pass 87b: include RESOLUTION + CURATOR_STANCE +
                # OVERRIDE_REASON so handle_protection_intent writes a
                # ``CuratorResolutionLog`` row. Previously the synthetic
                # bare-triple form left card-button Keeps invisible to
                # the year-in-review / stats.
                await handle_protection_intent(
                    user_id,
                    _build_protection_intent_line(user_id, title, short_circuit_reason),
                )
        except Exception as e:
            # Mirror the Pass-82e logger contract — class + safe str.
            logger.error(
                "💥 [COMMENT ANALYSIS ERROR] (short-circuit path): %s: %s",
                type(e).__name__, e or "(no message)",
                exc_info=True,
            )
        return is_kept

    prompt = f"""[MODE: DELETION COMMENT ANALYSIS]
The user left a note on a deletion proposal for '{title}'.
Comment: "{comment}"

Task 1: Determine if the user intends to KEEP or DELETE the item. 
*NOTE: If the user mentions hosting it for someone else, or that someone else watches it, that is ALWAYS a "KEEP" action, even if the user personally dislikes the show.*
Task 2: Translate the core reason into ENGLISH.
Task 3: Extract any genres, themes, or aspects the user explicitly likes or dislikes in this comment (e.g., ["sitcom", "dark narrative", "nostalgia"]).

Output ONLY valid JSON:
{{
  "action": "KEEP" | "DELETE", 
  "reason": "Brief reason in English",
  "sentiment": "positive" | "negative" | "neutral",
  "aspects": ["..."]
}}"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=700),
                },
            )

        if r.status_code == 200:
            text = r.json().get("message", {}).get("content", "").strip()
            data = parse_llm_json(text)
            is_kept = data.get("action") == "KEEP"
            reason_en = data.get("reason", "")
            sentiment = data.get("sentiment", "positive" if is_kept else "negative")
            aspects = data.get("aspects", [])
            
            # 1. Speichere die Konversation im Episodic Memory
            mem_id = await write_memory(
                user_id=user_id,
                memory_type="explicit_statement" if is_kept else "feedback",
                content=f"The user {'decided to keep' if is_kept else 'agreed to delete'} '{title}'. Reason: {reason_en}",
                metadata={"title": title, "source": "deletion_comment"},
            )
            
            # 1.5 NEU: Check auf Konflikte mit alten Erinnerungen
            if mem_id:
                await resolve_memory_conflicts(
                    user_id=user_id,
                    new_memory_id=mem_id,
                    new_content=f"The user {'decided to keep' if is_kept else 'agreed to delete'} '{title}'. Reason: {reason_en}",
                    metadata={"title": title}
                )
            
            # 2. NEU: Update den harten Taste-Vektor!
            await update_taste_profile_from_memory(
                user_id=user_id,
                title=title,
                sentiment=sentiment,
                aspects=aspects,
                reason=reason_en,
                media_category=media_category
            )
            
            if is_kept:
                # Pass 87b: same enrichment as the short-circuit branch
                # above — pass the full Pass-66 protocol so the CRL row
                # gets written instead of being silently dropped.
                await handle_protection_intent(
                    user_id,
                    _build_protection_intent_line(user_id, title, reason_en),
                )

            return is_kept

    except Exception as e:
        # Pass 82e: same Pass-14.13 pattern as music_metadata / the
        # MEMORY-EXTRACTION-ERROR branch above — bare ``{e}`` formats to
        # an empty string for httpx timeouts (the most common cause when
        # the summarizer is cold or overloaded), making the log line
        # useless. Include the exception class so the failure mode is
        # identifiable, and ``exc_info=True`` so the traceback shows
        # WHICH step in this long try block actually raised (LLM POST,
        # JSON parse, write_memory, resolve_memory_conflicts, taste
        # vector update, or protection intent — they all sit under this
        # single handler).
        logger.error(
            "💥 [COMMENT ANALYSIS ERROR]: %s: %s",
            type(e).__name__, e or "(no message)",
            exc_info=True,
        )

    return False

# ── TASTE VECTOR UPDATE FROM MEMORY ──────────────────────────────────────────────

async def update_taste_profile_from_memory(user_id: int, title: str, sentiment: str, aspects: list, reason: str, media_category: str = "show"):
    """
    Updates the structured JSON Taste Vector with explicit user feedback.
    Currently operates on unencrypted JSON (Phase A).
    """
    from src.services.taste_vectors import merge_feedback_into_vector
    import json
    
    feedback_data = {
        "title": title,
        "sentiment": sentiment, # "positive" or "negative"
        "genre_aspects": aspects, 
        "reason": reason
    }

    with get_db_session() as db:
        from src.database.models import EncryptedTasteVector
        
        etv = db.query(EncryptedTasteVector).filter(
            EncryptedTasteVector.user_id == user_id,
            EncryptedTasteVector.media_category == media_category
        ).first()

        if not etv:
            return

        try:
            # We are in "Phase A" (unencrypted), so load the JSON directly.
            current_vector = json.loads(etv.encrypted_blob)

            # If the vector already uses the new v1 encryption, bail out —
            # we don't have the PIN here and can't decrypt.
            if current_vector.get("version") == 1:
                logger.warning(f"⚠️ [TASTE VECTOR] Vector for {media_category} is encrypted. Cannot update without PIN.")
                return

            # Merge the feedback (updates genre_aversion, disliked_titles, etc.)
            updated_vector = merge_feedback_into_vector(current_vector, feedback_data)

            # Persist as plain JSON again so recommendations_engine can read it.
            etv.encrypted_blob = json.dumps(updated_vector)
            db.commit()
            
            logger.info(f"🧠 [TASTE VECTOR UPDATED] Added {sentiment} feedback for '{title}'. Aspects: {aspects}")
            
        except Exception as e:
            # Pass 82e: same fix as the sibling error handlers in this
            # module — include exception class so the log line is useful
            # when ``str(e)`` is empty.
            logger.error(
                "💥 [TASTE VECTOR UPDATE ERROR]: %s: %s",
                type(e).__name__, e or "(no message)",
                exc_info=True,
            )

# ── MEMORY DECAY / CLEANUP ────────────────────────────────────────────────────

async def run_memory_decay(user_id: int, keep_top: int = 500):
    """
    Remove low-importance, old, never-accessed memories.
    Keeps the memory store lean and relevant.
    Called after each sync.

    Two paths:
      1. Volume-based — fires when total > keep_top, drops 90-day-old low-importance.
      2. Time-based   — even with few memories, anything > 180 days old, importance < 0.3,
                        access_count == 0 is genuinely stale and gets dropped.
    """
    with get_db_session() as db:
        total = db.query(EpisodicMemory).filter(EpisodicMemory.user_id == user_id).count()

        deleted_total = 0

        if total > keep_top:
            cutoff = datetime.utcnow() - timedelta(days=90)
            deleted_total += db.query(EpisodicMemory).filter(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.importance < 0.5,
                EpisodicMemory.access_count == 0,
                EpisodicMemory.created_at < cutoff,
            ).delete()

        # Time-based path — runs regardless of total. Drops only the very stale.
        very_stale_cutoff = datetime.utcnow() - timedelta(days=180)
        deleted_total += db.query(EpisodicMemory).filter(
            EpisodicMemory.user_id == user_id,
            EpisodicMemory.importance < 0.3,
            EpisodicMemory.access_count == 0,
            EpisodicMemory.created_at < very_stale_cutoff,
        ).delete()

        if deleted_total:
            db.commit()
            logger.info("Memory decay: removed %d stale memories for user %d", deleted_total, user_id)
