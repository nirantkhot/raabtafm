from datetime import datetime
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class VibeRequest(BaseModel):
    # min_length=1 so single emojis are valid vibes; strip guards against
    # whitespace-only strings that would produce meaningless searches.
    vibe: str = Field(..., min_length=1, max_length=200)
    display_name: str

    @field_validator("vibe")
    @classmethod
    def vibe_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("vibe must not be blank or whitespace only")
        return v.strip()


class QueueItem(BaseModel):
    # asyncpg returns UUID objects; declare them as UUID so Pydantic v2
    # accepts them directly — FastAPI serialises UUID → string in JSON output.
    id: UUID
    session_id: UUID
    spotify_id: str
    title: str
    artist: str
    position: int
    added_by: str
    vibe_query: Optional[str] = None
    similarity_score: Optional[float] = None
    added_at: datetime


class VibeResponse(BaseModel):
    tracks: list[dict]
    vibe_interpreted_as: list[str]
    confidence: float
    fallback_used: bool
