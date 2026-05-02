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
from src.database.models import EpisodicMemory
from src.config import settings

logger = logging.getLogger(__name__)


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
                (EpisodicMemory.media_category == None)
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

async def extract_memories_from_exchange(
    user_id: int,
    user_message: str,
    assistant_response: str,
    media_category: str = None,
):
    """
    After each chat exchange, ask the small LLM to extract any
    memorable facts from the conversation.
    Runs in background — non-blocking.
    """
    logger.info(f"🔍 [MEMORY EXTRACTION] Starting analysis for user_id: {user_id}")
    logger.debug(f"🔍 [MEMORY EXTRACTION] User Message: {user_message[:100]}...")

    prompt = f"""[MODE: MEMORY EXTRACTION]
Extract memorable facts from this conversation exchange.
Output ONLY a JSON list. Each item: {{"content": "...", "type": "...", "title": "..."}}
Types: explicit_statement, feedback, preference_shift, viewing_pattern, conversation
Only include things worth remembering long-term. Empty list [] if nothing significant.

User said: {user_message[:300]}
Assistant said: {assistant_response[:300]}

JSON:"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 300},
                },
            )
        if r.status_code != 200:
            logger.warning(f"⚠️ [MEMORY EXTRACTION] API returned status code {r.status_code}")
            return

        text = r.json().get("message", {}).get("content", "").strip()
        logger.debug(f"🤖 [MEMORY EXTRACTION] LLM Raw Output: {text}")

        # Strip markdown
        if "```" in text:
            text = text.split("```")[1].lstrip("json").strip()

        try:
            facts = json.loads(text)
        except json.JSONDecodeError as je:
             logger.error(f"❌ [MEMORY EXTRACTION] JSON Parsing failed: {je}. Raw string: {text}")
             return

        if not isinstance(facts, list):
             logger.warning(f"⚠️ [MEMORY EXTRACTION] LLM output was not a list: {type(facts)}")
             return

        if not facts:
            logger.info("ℹ️ [MEMORY EXTRACTION] LLM found no significant facts to extract.")

        for fact in facts[:3]:  # max 3 memories per exchange
            if fact.get("content"):
                mem_id = await write_memory(
                    user_id=user_id,
                    memory_type=fact.get("type", "conversation"),
                    content=fact["content"],
                    metadata={"title": fact.get("title", ""), "source": "chat"},
                    media_category=media_category,
                )
                if mem_id:
                     logger.info(f"🧠 [MEMORY SAVED] ID: {mem_id} | Type: {fact.get('type')} | Content: {fact['content']}")

    except Exception as e:
        logger.error(f"💥 [MEMORY EXTRACTION ERROR]: {e}")


# ── PROTECTION INTENT (MODE 2: MEMORY EXTRACTION) ────────────────────────────

async def handle_protection_intent(user_id: int, llm_output: str) -> Optional[str]:
    """
    Parse LLM output for PROTECT_MEDIA action and write to ProtectedMedia.
    Removes any pending DeletionProposals for the same title.
    Returns a confirmation message or None if no action was found.
    """
    if "ACTION: PROTECT_MEDIA" not in llm_output:
        return None

    try:
        parts = llm_output.split("|")
        title = parts[1].replace("TITLE:", "").strip() if len(parts) > 1 else ""
        reason = parts[2].replace("REASON:", "").strip() if len(parts) > 2 else "User requested"

        if not title:
            logger.warning("⚠️ [PROTECTION] LLM triggered PROTECT_MEDIA but no title was found.")
            return None

        from src.database.models import ProtectedMedia, DeletionProposal

        with get_db_session() as db:
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

            # Remove pending deletion proposals for this title
            removed = db.query(DeletionProposal).filter(
                DeletionProposal.user_id == user_id,
                DeletionProposal.title == title,
                DeletionProposal.status == "pending",
            ).delete()

            db.commit()
            if removed > 0:
                 logger.info(f"🧹 [CLEANUP] Removed {removed} pending deletion proposals for '{title}'")

        return f"✅ '{title}' wurde dauerhaft geschützt."

    except Exception as e:
        logger.error(f"💥 [PROTECTION INTENT ERROR]: {e}")
    return None


async def detect_and_handle_protection(
    user_id: int,
    user_message: str,
    assistant_response: str,
) -> Optional[str]:
    """
    Ask the summarizer model whether the user wants to protect a title from deletion.
    If detected, delegates to handle_protection_intent and returns the confirmation.
    Runs as a background task after each chat exchange.
    """
    logger.info(f"🛡️ [PROTECTION CHECK] Scanning for protection intents...")
    
    prompt = f"""[MODE: PROTECTION INTENT DETECTION]
Identify if the user wants to PROTECT a specific media title from deletion.
Look for keywords like: behalten, keep, nicht löschen, für mitbewohner, schützen, protect, don't delete, never delete, bitte behalten.

If a protection intent is found, output EXACTLY this format (one line):
ACTION: PROTECT_MEDIA | TITLE: [exact title] | REASON: [brief reason]

If no protection intent is present, output: NO_ACTION

User said: {user_message[:400]}
Assistant said: {assistant_response[:200]}

Output:"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 80},
                },
            )
        if r.status_code != 200:
             logger.warning(f"⚠️ [PROTECTION CHECK] API returned status code {r.status_code}")
             return None

        llm_output = r.json().get("message", {}).get("content", "").strip()
        logger.debug(f"🤖 [PROTECTION CHECK] LLM Raw Output: {llm_output}")
        
        return await handle_protection_intent(user_id, llm_output)

    except Exception as e:
        logger.error(f"💥 [PROTECTION CHECK ERROR]: {e}")
    return None

async def detect_and_handle_protection(
    user_id: int,
    user_message: str,
    assistant_response: str,
) -> Optional[str]:
    """
    Ask the summarizer model whether the user wants to protect a title from deletion.
    If detected, delegates to handle_protection_intent and returns the confirmation.
    Runs as a background task after each chat exchange.
    """
    prompt = f"""[MODE: PROTECTION INTENT DETECTION]
Identify if the user wants to PROTECT a specific media title from deletion.
Look for keywords like: behalten, keep, nicht löschen, für mitbewohner, schützen, protect, don't delete, never delete, bitte behalten.

If a protection intent is found, output EXACTLY this format (one line):
ACTION: PROTECT_MEDIA | TITLE: [exact title] | REASON: [brief reason]

If no protection intent is present, output: NO_ACTION

User said: {user_message[:400]}
Assistant said: {assistant_response[:200]}

Output:"""

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "options": {"temperature": 0.1, "num_predict": 80},
                },
            )
        if r.status_code != 200:
            return None

        llm_output = r.json().get("message", {}).get("content", "").strip()
        return await handle_protection_intent(user_id, llm_output)

    except Exception as e:
        logger.debug("Protection intent detection failed: %s", e)
    return None


# ── MEMORY DECAY / CLEANUP ────────────────────────────────────────────────────

async def run_memory_decay(user_id: int, keep_top: int = 500):
    """
    Remove low-importance, old, never-accessed memories.
    Keeps the memory store lean and relevant.
    Called after each sync.
    """
    with get_db_session() as db:
        total = db.query(EpisodicMemory).filter(EpisodicMemory.user_id == user_id).count()
        if total <= keep_top:
            return

        cutoff = datetime.utcnow() - timedelta(days=90)
        # Delete old low-importance memories that were never accessed again
        deleted = db.query(EpisodicMemory).filter(
            EpisodicMemory.user_id == user_id,
            EpisodicMemory.importance < 0.5,
            EpisodicMemory.access_count == 0,
            EpisodicMemory.created_at < cutoff,
        ).delete()
        db.commit()
        if deleted:
            logger.info("Memory decay: removed %d stale memories for user %d", deleted, user_id)
