-- ── Core tables ───────────────────────────────────────────────────────────────
-- These have no dependency on pgvector and must always succeed.

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code VARCHAR(12) UNIQUE NOT NULL,   -- e.g. JAZZ-4521 = 9 chars; 12 gives headroom
    host_client_id TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    ended_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS session_members (
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    joined_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (session_id, client_id)
);

CREATE TABLE IF NOT EXISTS session_play_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    track_spotify_id TEXT NOT NULL,
    played_at TIMESTAMPTZ DEFAULT NOW(),
    skipped BOOLEAN DEFAULT FALSE,
    listen_duration_ms INTEGER
);

CREATE TABLE IF NOT EXISTS session_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID REFERENCES sessions(id) ON DELETE CASCADE,
    spotify_id TEXT NOT NULL,
    title TEXT NOT NULL,
    artist TEXT NOT NULL,
    position INTEGER NOT NULL,
    added_by TEXT NOT NULL,
    vibe_query TEXT,
    similarity_score FLOAT,
    added_at TIMESTAMPTZ DEFAULT NOW()
);

-- ── pgvector extension ─────────────────────────────────────────────────────────
-- Wrapped in a DO block so a missing pgvector install does not abort the migration.
-- Core session functionality works without this; vibe search will 503 until
-- pgvector is available and the catalog is seeded.

DO $$
BEGIN
    CREATE EXTENSION IF NOT EXISTS vector;
EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'pgvector extension not available (skipping): %', SQLERRM;
END
$$;

-- ── Songs catalog (vector-dependent) ──────────────────────────────────────────
-- Also wrapped: if the vector type does not exist these objects cannot be created,
-- but that is acceptable — the app degrades gracefully without catalog search.

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS songs (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        spotify_id TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        artist TEXT NOT NULL,
        album TEXT,
        duration_ms INTEGER,
        features JSONB NOT NULL,
        song_text TEXT NOT NULL,
        embedding vector(1536),
        indexed_at TIMESTAMPTZ DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS songs_embedding_idx
        ON songs USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'vector-dependent objects not created (skipping): %', SQLERRM;
END
$$;
