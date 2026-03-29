import time
from openai import OpenAI

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI()
    return _client


def compose_song_text(title: str, artist: str, features: dict, genre: str = "") -> str:
    energy = features["energy"]
    valence = features["valence"]
    tempo = features["tempo"]
    acousticness = features["acousticness"]
    instrumentalness = features["instrumentalness"]

    if energy > 0.75:
        energy_word = "very high energy, intense"
    elif energy > 0.5:
        energy_word = "mid-high energy, active"
    elif energy > 0.25:
        energy_word = "low energy, calm"
    else:
        energy_word = "very low energy, ambient"

    if valence > 0.65:
        mood_word = "happy, upbeat, positive"
    elif valence > 0.45:
        mood_word = "neutral mood"
    elif valence > 0.2:
        mood_word = "melancholic, somber"
    else:
        mood_word = "very sad, dark, heavy"

    if tempo > 160:
        tempo_word = "very fast tempo"
    elif tempo > 120:
        tempo_word = "upbeat tempo"
    elif tempo > 90:
        tempo_word = "moderate tempo"
    else:
        tempo_word = "slow tempo"

    acoustic_word = "acoustic, organic" if acousticness > 0.5 else "electronic, produced"
    vocal_word = "instrumental" if instrumentalness > 0.5 else "vocal"
    genre_part = f"Genre: {genre}. " if genre and genre != "nan" else ""

    return (
        f"{title} by {artist}. {genre_part}{energy_word}, {mood_word}. "
        f"{tempo_word} at {tempo:.0f} BPM. {acoustic_word}. {vocal_word}."
    )


def embed_texts(texts: list[str]) -> list[list[float]]:
    client = _get_client()
    results: list[list[float]] = []
    batch_size = 100
    batches = [texts[i:i + batch_size] for i in range(0, len(texts), batch_size)]
    total = len(batches)

    for i, batch in enumerate(batches):
        print(f"Embedding batch {i + 1}/{total}...")
        try:
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=batch,
            )
        except Exception:
            time.sleep(2)
            response = client.embeddings.create(
                model="text-embedding-3-small",
                input=batch,
            )
        results.extend([item.embedding for item in response.data])

    return results
