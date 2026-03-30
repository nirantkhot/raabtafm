from fastapi import APIRouter

router = APIRouter()

DEMO_TRACKS = [
    {"spotify_id": "4cOdK2wGLETKBW3PvgPWqT", "title": "Bohemian Rhapsody", "artist": "Queen"},
    {"spotify_id": "3z8h0TU7ReDPLIFD9xzfLP", "title": "Blinding Lights", "artist": "The Weeknd"},
    {"spotify_id": "0VjIjW4GlUZAMYd2vXMi3b", "title": "Watermelon Sugar", "artist": "Harry Styles"},
    {"spotify_id": "7qiZfU4dY1lWllzX7mPBI3", "title": "Shape of You", "artist": "Ed Sheeran"},
    {"spotify_id": "6DCZcSspjsKoFjzjrWoCdn", "title": "God's Plan", "artist": "Drake"},
    {"spotify_id": "2takcwOaAZWiXQijPHIx7B", "title": "Levitating", "artist": "Dua Lipa"},
    {"spotify_id": "1Cv1YLb4q0RzL6pybtaMLo", "title": "Peaches", "artist": "Justin Bieber"},
    {"spotify_id": "5QO79kh1waicV47BqGRL3g", "title": "Save Your Tears", "artist": "The Weeknd"},
    {"spotify_id": "7lPN2DXiMsVn7XUKtOW1CS", "title": "drivers license", "artist": "Olivia Rodrigo"},
    {"spotify_id": "3KkXRkHbMCARz0aVfEt68P", "title": "Butter", "artist": "BTS"},
]


@router.get("/tracks/demo")
async def get_demo_tracks():
    return DEMO_TRACKS
