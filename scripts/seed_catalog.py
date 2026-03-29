import asyncio, json, os, sys
import asyncpg
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

CSV_PATH = "/app/data/spotify_features_v2.csv"
EMBED_BATCH = 100

async def main():

  # Step 0 — validate CSV
  if not Path(CSV_PATH).exists():
    print(f"ERROR: CSV not found at {CSV_PATH}")
    print("Place spotify_features_v2.csv in ./data/ on host machine")
    sys.exit(1)

  # Step 1 — connect
  db = await asyncpg.connect(os.getenv("DATABASE_URL"))
  print("Connected to Postgres")

  # Step 2 — load and clean
  print(f"Loading {CSV_PATH} ...")
  df = pd.read_csv(CSV_PATH, low_memory=False)
  print(f"Loaded {len(df):,} rows")

  # Rename columns to match our schema
  df = df.rename(columns={
    'track_id':   'spotify_id',
    'track_name': 'title',
    'album_name': 'album',
  })

  # Drop nulls on required columns
  required = ['spotify_id', 'title', 'artists', 'energy', 'valence',
              'tempo', 'danceability', 'acousticness', 'instrumentalness']
  df = df.dropna(subset=required)

  # Popularity filter — only keep recognizable songs
  df = df[df['popularity'] > 50]
  print(f"After popularity > 50 filter: {len(df):,} rows")

  # Drop duplicates by spotify_id — same track appears in
  # multiple genres in this dataset, keep highest popularity
  df = df.sort_values('popularity', ascending=False)
  df = df.drop_duplicates(subset='spotify_id', keep='first')
  print(f"After deduplication: {len(df):,} rows")

  # Step 3 — use full dataset
  sampled = df.reset_index(drop=True)
  print(f"Using all {len(sampled):,} songs")

  # Step 4 — parse and compose song_text
  sys.path.insert(0, '/app')
  from app.catalog.embedder import compose_song_text

  songs = []
  errors = 0

  for _, row in sampled.iterrows():
    try:
      # artists is a plain string in this dataset
      artist = str(row['artists']).strip()
      genre  = str(row.get('track_genre', '')).strip()

      features = {
        'energy':           float(row['energy']),
        'valence':          float(row['valence']),
        'tempo':            float(row['tempo']),
        'danceability':     float(row['danceability']),
        'acousticness':     float(row['acousticness']),
        'instrumentalness': float(row['instrumentalness']),
        'loudness':         float(row.get('loudness', 0)),
      }

      # Pass genre into compose_song_text
      song_text = compose_song_text(
        str(row['title']), artist, features, genre
      )

      songs.append({
        'spotify_id':  str(row['spotify_id']),
        'title':       str(row['title']),
        'artist':      artist,
        'album':       str(row.get('album', '')),
        'duration_ms': int(float(row.get('duration_ms', 0))),
        'features':    features,
        'song_text':   song_text,
      })
    except Exception:
      errors += 1

  print(f"Prepared {len(songs):,} songs ({errors} errors skipped)")

  # Step 5 — clear old songs and insert fresh
  print("\nClearing old catalog...")
  await db.execute("TRUNCATE songs RESTART IDENTITY CASCADE")

  print("Inserting into database...")
  inserted = 0
  skipped = 0
  BATCH = 500

  for i in range(0, len(songs), BATCH):
    batch = songs[i:i+BATCH]
    for s in batch:
      try:
        await db.execute("""
          INSERT INTO songs
            (spotify_id, title, artist, album,
             duration_ms, features, song_text)
          VALUES ($1,$2,$3,$4,$5,$6,$7)
          ON CONFLICT (spotify_id) DO UPDATE SET
            title     = EXCLUDED.title,
            artist    = EXCLUDED.artist,
            song_text = EXCLUDED.song_text,
            features  = EXCLUDED.features
        """,
          s['spotify_id'], s['title'], s['artist'],
          s['album'], s['duration_ms'],
          json.dumps(s['features']), s['song_text'],
        )
        inserted += 1
      except Exception:
        skipped += 1
    print(f"  Inserted {min(i+BATCH, len(songs)):,}/{len(songs):,}...")

  print(f"Insert done: {inserted:,} inserted, {skipped} skipped")

  # Step 6 — embed all songs
  from app.catalog.embedder import embed_texts

  print(f"\nEmbedding songs (~20-25 min for {len(songs):,} songs, costs ~$0.14)...")
  embedded = 0

  while True:
    rows = await db.fetch("""
      SELECT id, song_text FROM songs
      WHERE embedding IS NULL LIMIT 100
    """)
    if not rows:
      break

    texts = [r['song_text'] for r in rows]
    try:
      vectors = embed_texts(texts)
    except Exception as e:
      print(f"Embed error: {e} — retrying in 5s...")
      await asyncio.sleep(5)
      continue

    for row, vec in zip(rows, vectors):
      vec_str = '[' + ','.join(str(x) for x in vec) + ']'
      await db.execute(
        "UPDATE songs SET embedding = $1::vector WHERE id = $2",
        vec_str, row['id']
      )
    embedded += len(rows)
    total_db = await db.fetchval("SELECT COUNT(*) FROM songs")
    print(f"  Embedded {embedded:,}/{total_db:,}...")

  # Step 7 — summary
  total    = await db.fetchval("SELECT COUNT(*) FROM songs")
  with_emb = await db.fetchval(
    "SELECT COUNT(*) FROM songs WHERE embedding IS NOT NULL"
  )
  idx = await db.fetchval("""
    SELECT indexname FROM pg_indexes
    WHERE tablename='songs'
    AND indexname='songs_embedding_idx'
  """)

  print("\n" + "="*50)
  print("SEED COMPLETE")
  print("="*50)
  print(f"Total songs:      {total:,}")
  print(f"With embeddings:  {with_emb:,}")
  print(f"pgvector index:   {'OK' if idx else 'MISSING'}")
  print("="*50)

  await db.close()

if __name__ == "__main__":
  asyncio.run(main())