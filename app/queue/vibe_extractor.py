"""
NL vibe → Spotify audio feature ranges via Claude Haiku.
"""

import json
import structlog
from anthropic import AsyncAnthropic

from app.config import settings
from app.core.logging import E

logger = structlog.get_logger(__name__)

_client: AsyncAnthropic | None = None


def _get_client() -> AsyncAnthropic:
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


SYSTEM_PROMPT = """
You are a music expert. Given a vibe or mood description, extract
target Spotify audio feature ranges. Return ONLY valid JSON.
No prose. No markdown. No backticks. No explanation.
Return this exact schema:
{
"energy": {"min": 0.0, "max": 1.0},
"valence": {"min": 0.0, "max": 1.0},
"tempo": {"min": 60, "max": 200},
"danceability": {"min": 0.0, "max": 1.0},
"acousticness": {"min": 0.0, "max": 1.0},
"mood_keywords": ["2 to 4 descriptive words"],
"confidence": 0.8
}
Ranges should reflect the vibe well but not be too narrow.
Prefer ranges of 0.3-0.5 width on energy/valence for best results.
confidence: 0.1 (very vague) to 1.0 (very specific)
Examples:
"late night drive" ->
energy 0.2-0.55, valence 0.1-0.45, tempo 65-105,
keywords: ["nocturnal","melancholic","atmospheric","reflective"], confidence: 0.75
"hype gym workout" ->
energy 0.75-1.0, valence 0.45-1.0, tempo 120-185,
danceability 0.6-1.0,
keywords: ["intense","powerful","pumping","aggressive"], confidence: 0.85
"sunday morning coffee" ->
energy 0.05-0.4, valence 0.45-0.8, tempo 60-105,
acousticness 0.4-1.0,
keywords: ["cozy","warm","gentle","slow"], confidence: 0.8
"sad and slow" ->
energy 0.05-0.4, valence 0.0-0.35, tempo 55-95,
keywords: ["melancholic","somber","slow","emotional"], confidence: 0.7
"feel good summer" ->
energy 0.55-1.0, valence 0.65-1.0, tempo 100-155,
danceability 0.55-1.0,
keywords: ["upbeat","happy","energetic","summer"], confidence: 0.8
"focus and study" ->
energy 0.1-0.5, valence 0.3-0.65, tempo 70-120,
acousticness 0.2-0.8, instrumentalness 0.3-1.0,
keywords: ["focused","calm","steady","minimal"], confidence: 0.75
"""

_FALLBACK_TEMPLATE = {
    "energy":       {"min": 0.0, "max": 1.0},
    "valence":      {"min": 0.0, "max": 1.0},
    "tempo":        {"min": 55,  "max": 200},
    "danceability": {"min": 0.0, "max": 1.0},
    "acousticness": {"min": 0.0, "max": 1.0},
    "confidence":   0.1,
}


def _fallback(user_input: str) -> dict:
    return {**_FALLBACK_TEMPLATE, "mood_keywords": [user_input]}


async def extract_vibe_features(user_input: str) -> dict:
    """
    Call Claude Haiku to extract audio feature ranges from a vibe description.
    Returns wide-open fallback on any error.
    """
    client = _get_client()
    raw_response = None
    try:
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_input}],
        )
        raw_response = message.content[0].text
        # Strip markdown code fences the model sometimes adds despite instructions
        # e.g.  ```json\n{...}\n```  or  ```\n{...}\n```
        cleaned = raw_response.strip()
        if cleaned.startswith("```"):
            # Drop the opening fence line and any closing fence
            cleaned = cleaned.split("\n", 1)[-1]          # remove ```json line
            cleaned = cleaned.rsplit("```", 1)[0].strip() # remove trailing ```
        features = json.loads(cleaned)
        logger.debug(
            E.VIBE_EXTRACTED,
            vibe=user_input,
            confidence=features.get("confidence"),
            keywords=features.get("mood_keywords"),
        )
        return features
    except Exception as exc:
        logger.error(
            E.VIBE_EXTRACTION_ERROR,
            vibe=user_input,
            error=str(exc),
            raw_response=raw_response,
        )
        return _fallback(user_input)
