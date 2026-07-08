from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class DiscussContext(BaseModel):
    """Reference to a server-owned record the user wants to discuss.

    The backend looks up the actual record from the DB (with ownership check)
    and formats it as a RAG-style context block in the system prompt — we do
    NOT trust user-supplied title/reason text. Old-shape fields (title, pitch,
    action) are kept Optional for short-term frontend backward compat but
    ignored when ``kind`` + the matching ID are present.
    """
    kind: Optional[str] = None         # "deletion_proposal" | "proactive_message" | "principle"
    proposal_id: Optional[int] = None  # FK → DeletionProposal.id
    message_id: Optional[int] = None   # FK → ProactiveMessage.id
    principle_id: Optional[int] = None # FK → CuratorPrinciple.id (learned-principle review)

    # Pass 81d: ONE-SHOT flag set by the frontend 🔍 Reevaluate button. The
    # frontend sends a short user-visible message ("Run a Level 2 thematic
    # scan.") and toggles this flag; the chat backend then injects the
    # full Level-2 challenge framing into the discuss context block, so
    # the curator answers as if the user had typed the long prompt. The
    # frontend clears the flag after the first send, so follow-up turns
    # in the same thread don't re-inject the framing.
    reevaluate: Optional[bool] = False

    # Legacy / hint fields (still allowed)
    category: Optional[str] = None     # "movie" | "show" | "anime" | "music" — domain-gated RAG
    title: Optional[str] = None        # legacy — ignored if (kind, proposal_id) given
    pitch: Optional[str] = None        # legacy — ignored
    action: Optional[str] = None       # legacy — superseded by `kind`
    trigger_type: Optional[str] = None # legacy / informational only


class ChatMessage(BaseModel):
    message: str
    discuss_context: Optional[DiscussContext] = None

