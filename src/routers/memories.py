"""Curatarr 1.0 - Episodic Memories API"""

from fastapi import APIRouter, Depends, HTTPException
from src.database.connection import get_db_session
from src.database.models import EpisodicMemory, User
from src.routers.auth import get_current_user
from src.services.episodic_memory import retrieve_memories, write_memory, run_memory_decay

router = APIRouter()


@router.get("/")
async def get_memories(
    query: str = "",
    category: str = None,
    limit: int = 20,
    user: User = Depends(get_current_user),
):
    """Retrieve memories relevant to a query (or all recent if no query)."""
    if not query:
        query = "general preferences and habits"
    memories = await retrieve_memories(
        user.id, query, top_k=limit, media_category=category
    )
    return {"memories": memories, "total": len(memories)}


@router.post("/")
async def add_memory(
    content: str,
    memory_type: str = "explicit_statement",
    media_category: str = None,
    user: User = Depends(get_current_user),
):
    """Manually add a memory (e.g. from user-provided preferences)."""
    mem_id = await write_memory(
        user_id=user.id,
        memory_type=memory_type,
        content=content,
        metadata={"source": "manual"},
        media_category=media_category,
    )
    return {"id": mem_id, "ok": bool(mem_id)}


@router.delete("/{memory_id}")
async def delete_memory(
    memory_id: int,
    user: User = Depends(get_current_user),
):
    """Delete a specific episodic memory by ID."""
    with get_db_session() as db:
        mem = db.query(EpisodicMemory).filter(
            EpisodicMemory.id == memory_id,
            EpisodicMemory.user_id == user.id,
        ).first()
        if not mem:
            raise HTTPException(status_code=404, detail="Memory not found")
        db.delete(mem)
    return {"ok": True, "deleted_id": memory_id}


@router.post("/decay")
async def trigger_decay(user: User = Depends(get_current_user)):
    """Manually trigger memory decay cleanup."""
    await run_memory_decay(user.id)
    return {"ok": True}
