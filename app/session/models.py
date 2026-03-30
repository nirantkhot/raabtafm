from enum import Enum
from typing import Optional
from pydantic import BaseModel


class SessionStatus(str, Enum):
    PLAYING = "playing"
    PAUSED = "paused"


class PlaybackState(BaseModel):
    status: SessionStatus = SessionStatus.PAUSED
    position_ms: int = 0
    track_spotify_id: Optional[str] = None
    updated_at: str = ""
    version: int = 0


class SessionState(BaseModel):
    session_id: str
    code: str
    playback: PlaybackState
    members: dict[str, str] = {}   # {client_id: display_name}
    rtt_ms: dict[str, int] = {}


class CreateSessionRequest(BaseModel):
    display_name: str


class JoinSessionRequest(BaseModel):
    display_name: str


class PlaybackCommand(BaseModel):
    action: str  # "play" | "pause" | "seek"
    position_ms: int = 0
    track_spotify_id: Optional[str] = None


class WsMessage(BaseModel):
    event: str
    session_id: str
    payload: dict
    timestamp: str
