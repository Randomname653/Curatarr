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
    kind: Optional[str] = None         # "deletion_proposal" | "proactive_message" | "principle" | "watched_title"
    type: Optional[str] = None         # alias spelling for `kind` (last-played contract uses it)
    proposal_id: Optional[int] = None  # FK → DeletionProposal.id
    message_id: Optional[int] = None   # FK → ProactiveMessage.id
    principle_id: Optional[int] = None # FK → CuratorPrinciple.id (learned-principle review)

    # watched_title (last-played strip click): hints for resolving the user's
    # OWN watch-history row. All optional — the backend anchors on the row it
    # finds for THIS user and never trusts the client's copies of the facts.
    history_id: Optional[int] = None   # FK → WatchHistoryEntry.id (preferred when known)
    series_title: Optional[str] = None
    plex_rating_key: Optional[str] = None
    tmdb_id: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    viewed_at: Optional[str] = None

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

