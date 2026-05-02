from pydantic import BaseModel
from typing import Optional, List, Dict, Any

# NEU: Das Modell für den übergebenen Diskussions-Kontext
class DiscussContext(BaseModel):
    title: str
    pitch: str
    action: str = "deletion" # Falls wir später auch Empfehlungen diskutieren wollen

class ChatMessage(BaseModel):
    message: str
    media_context: Optional[List[str]] = None
    discuss_context: Optional[DiscussContext] = None  # NEU: Das optionale Feld

class ChatResponse(BaseModel):
    response: str
    media_references: Optional[List[Dict[str, Any]]] = None
    user_taste_update: Optional[Dict[str, float]] = None

class ChatFeedback(BaseModel):
    interaction_id: int
    feedback: int  # -1, 0, 1
    reason: Optional[str] = None