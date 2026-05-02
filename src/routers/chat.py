"""
Curatarr - Chat Router
Streaming Ollama with RAG, taste context, and persistent conversation memory.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncGenerator

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from src.database import get_db
from src.database.models import ChatInteraction, ConversationMessage, User
from src.routers.auth import get_current_user
from src.schemas.chat import ChatFeedback, ChatMessage, ChatResponse
from src.config import settings
from src.services.plex_sync import get_user_taste_context

logger = logging.getLogger(__name__)
router = APIRouter()

CONVERSATION_WINDOW = 20   # last N messages to include as context
MAX_TOKENS_APPROX = 6000   # rough token budget for conversation history


# ── HELPERS ───────────────────────────────────────────────────────────────────

async def _check_verification_response(user_id: int, user_message: str):
    """Check if user is responding to a pending verification question."""
    try:
        from src.services.verification_session import process_verification_response
        from src.database.connection import get_db_session
        from src.database.models import ProactiveMessage
        # Find most recent unread verification message
        with get_db_session() as db:
            pending = db.query(ProactiveMessage).filter(
                ProactiveMessage.user_id == user_id,
                ProactiveMessage.trigger_type == "verification",
                ProactiveMessage.read == False,
            ).order_by(ProactiveMessage.created_at.desc()).first()
            if pending:
                import json as _json
                question = _json.loads(pending.trigger_data or "{}")
                pending.read = True
                db.commit()
                await process_verification_response(user_id, user_message, question)
    except Exception as e:
        logger.debug("Verification response check failed: %s", e)


async def _extract_memories_bg(user_id: int, user_msg: str, assistant_msg: str):
    """Background task: extract memories from a chat exchange."""
    try:
        from src.services.episodic_memory import extract_memories_from_exchange
        await extract_memories_from_exchange(user_id, user_msg, assistant_msg)
    except Exception as e:
        logger.debug("Background memory extraction failed: %s", e)


async def _check_protection_intent_bg(user_id: int, user_msg: str, assistant_msg: str):
    """Background task: detect if the user wants to protect a title from deletion."""
    try:
        from src.services.episodic_memory import detect_and_handle_protection
        result = await detect_and_handle_protection(user_id, user_msg, assistant_msg)
        if result:
            logger.info("Protection intent handled for user %d: %s", user_id, result)
    except Exception as e:
        logger.debug("Protection intent check failed: %s", e)


async def _get_rag_context(query: str, n_results: int = 5) -> str:
    try:
        from src.vector_store.chromadb_wrapper import ChromaDBWrapper
        from src.embeddings.embedding_generator import EmbeddingGenerator
        gen = EmbeddingGenerator()
        embedding = await gen.generate_embedding(query)
        if not embedding:
            return ""
        chroma = ChromaDBWrapper()
        results = chroma.query(query_embeddings=[embedding], n_results=n_results)
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        await gen.close()
        lines = []
        for doc, meta in zip(docs, metas):
            title = meta.get("title", "Unknown")
            genres = meta.get("genres", "")
            themes = meta.get("themes", "")
            lines.append(f"- {title} ({genres}{', '+themes if themes else ''}): {doc[:200]}")
        return "\n".join(lines)
    except Exception as e:
        logger.debug("RAG failed: %s", e)
        return ""


def _load_conversation(user_id: int, db: Session) -> list:
    """Load recent conversation history for this user."""
    msgs = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.user_id == user_id)
        .order_by(ConversationMessage.created_at.desc())
        .limit(CONVERSATION_WINDOW)
        .all()
    )
    # Return in chronological order (oldest first)
    return [{"role": m.role, "content": m.content} for m in reversed(msgs)]


def _save_message(user_id: int, role: str, content: str, db: Session):
    db.add(ConversationMessage(
        user_id=user_id,
        role=role,
        content=content,
        created_at=datetime.utcnow(),
        tokens_approx=len(content) // 4,  # rough estimate
    ))
    db.commit()


# ── STREAMING CHAT ────────────────────────────────────────────────────────────

@router.post("/message")
async def send_message(
    message: ChatMessage,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Send a message — returns streaming response word by word."""
    ollama_url = settings.effective_ollama

    # 1. NEU: CONTEXT PRE-LOADING (Der Magic Trick)
    # Wenn ein Discuss-Kontext übergeben wurde, fälschen wir eine Assistant-Nachricht
    if message.discuss_context:
        title = message.discuss_context.title
        pitch = message.discuss_context.pitch
        fake_assistant_msg = f"Ich habe '{title}' zur Löschung vorgeschlagen. Mein Grund: {pitch}"
        
        # Speichere die Nachricht als Assistant in die DB
        _save_message(user.id, "assistant", fake_assistant_msg, db)

    # 2. Build context
    taste_context = await get_user_taste_context(user.id, query=message.message)
    rag_context = await _get_rag_context(message.message)
    
    # Da wir die Fake-Nachricht oben schon in die DB geschrieben haben, 
    # wird sie hier jetzt ganz normal als letzte Nachricht des Assistenten mitgeladen!
    conversation = _load_conversation(user.id, db)

    # Retrieve relevant episodic memories
    from src.services.episodic_memory import retrieve_memories, format_memories_for_context
    memories = await retrieve_memories(user.id, message.message, top_k=6)
    memory_context = format_memories_for_context(memories)

    system_prompt = f"""You are Curatarr, a personal AI media curator with deep knowledge of this user's taste.

{taste_context if taste_context else "No taste profile yet — the user should sync their Plex history first."}

{memory_context if memory_context else ""}

RELEVANT LIBRARY ITEMS:
{rag_context if rag_context else "(knowledge base not yet enriched — tell the user to run enrichment)"}

Be direct, warm, occasionally provocative. Reference specific titles when relevant.
If the knowledge base is incomplete, say so but still try to help.
Use your memories to personalise responses — you know this user."""

    # 3. Build message list with history
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(conversation)
    messages.append({"role": "user", "content": message.message})

    # 4. Save user message to history
    _save_message(user.id, "user", message.message, db)

    # 5. Stream from Ollama (ab hier bleibt alles exakt so, wie es war!)
    async def generate() -> AsyncGenerator[str, None]:
        full_response = ""
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_url}/api/chat",
                    json={
                        "model": settings.CURATOR_MODEL or settings.BASE_CURATOR_MODEL,
                        "messages": messages,
                        "stream": True,
                        "options": {"temperature": 0.7, "num_predict": 512},
                    },
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                full_response += token
                                yield f"data: {json.dumps({'token': token})}\n\n"
                            if chunk.get("done"):
                                break
                        except json.JSONDecodeError:
                            continue

        except httpx.ConnectError:
            msg = f"⚠️ Ollama not reachable at {ollama_url}. Make sure Ollama is running and '{settings.CURATOR_MODEL}' is pulled."
            full_response = msg
            yield f"data: {json.dumps({'token': msg})}\n\n"

        except Exception as e:
            msg = f"⚠️ Error: {e}"
            full_response = msg
            yield f"data: {json.dumps({'token': msg})}\n\n"

        finally:
            # Save assistant response to history
            if full_response:
                from src.database.connection import get_db_session
                with get_db_session() as db2:
                    _save_message(user.id, "assistant", full_response, db2)
                    db2.add(ChatInteraction(
                        user_id=user.id,
                        message=message.message,
                        response=full_response,
                        timestamp=datetime.utcnow(),
                    ))

                # Extract memories from this exchange in background
                asyncio.create_task(_extract_memories_bg(
                    user.id, message.message, full_response
                ))

                # Detect protection intents ("behalten", "keep", "nicht löschen", …)
                asyncio.create_task(_check_protection_intent_bg(
                    user.id, message.message, full_response
                ))

                # Check if this might be answering a verification question
                asyncio.create_task(_check_verification_response(
                    user.id, message.message
                ))

            yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")


@router.post("/feedback")
async def submit_feedback(
    feedback: ChatFeedback,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    interaction = db.query(ChatInteraction).filter(
        ChatInteraction.id == feedback.interaction_id,
        ChatInteraction.user_id == user.id,
    ).first()
    if not interaction:
        return {"status": "not_found"}
    interaction.feedback = feedback.feedback
    db.commit()
    return {"status": "ok"}


@router.get("/history")
async def get_chat_history(
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    msgs = (
        db.query(ConversationMessage)
        .filter(ConversationMessage.user_id == user.id)
        .order_by(ConversationMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        "messages": [
            {"role": m.role, "content": m.content, "created_at": m.created_at.isoformat()}
            for m in reversed(msgs)
        ]
    }


@router.delete("/history")
async def clear_chat_history(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Clear conversation memory (not ChatInteractions — those stay for feedback)."""
    db.query(ConversationMessage).filter(ConversationMessage.user_id == user.id).delete()
    db.commit()
    return {"status": "cleared"}
