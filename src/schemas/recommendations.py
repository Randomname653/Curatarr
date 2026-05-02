from pydantic import BaseModel
from typing import Optional, List, Dict, Any


class RecommendationRequest(BaseModel):
    user_id: int
    limit: int = 10
    exclude_watched: bool = True
    include_genres: Optional[List[str]] = None
    exclude_genres: Optional[List[str]] = None


class RecommendationResponse(BaseModel):
    recommendations: List[Dict[str, Any]]
    taste_vector_summary: Dict[str, float]
    reasoning: Optional[str] = None


class DeletionCandidate(BaseModel):
    media_id: str
    title: str
    service: str
    reason: str
    confidence: float
    storage_freed_mb: float
