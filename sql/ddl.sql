-- ============================================================
-- Song Catalog Enrichment Warehouse - DDL
-- Portable ANSI SQL (tested against Postgres; Snowflake/BigQuery
-- need minor type swaps noted inline).
-- ============================================================

-- Internal song catalog (source of truth: [DE TS] Song Catalog Data)
CREATE TABLE songs (
    song_id         VARCHAR(20) PRIMARY KEY,   -- = internal CODE
    song_title      TEXT NOT NULL,
    original_artist TEXT,                       -- nullable: ~30% blank in source
    created_at      TIMESTAMP DEFAULT NOW(),
    updated_at      TIMESTAMP DEFAULT NOW()
);

-- One row per Spotify recording (= one ISRC). A song has MANY recordings.
CREATE TABLE spotify_recordings (
    recording_id     VARCHAR(30) PRIMARY KEY,   -- spotify_track_id
    song_id          VARCHAR(20) NOT NULL REFERENCES songs(song_id),
    isrc             VARCHAR(15) NOT NULL,
    track_name       TEXT NOT NULL,
    artist_name      TEXT NOT NULL,
    album_name       TEXT,
    release_date     DATE,
    duration_ms      INTEGER,
    match_score      NUMERIC(5,2) NOT NULL,      -- from matcher.py
    match_status     VARCHAR(15) NOT NULL,       -- matched / ambiguous / no_match
    ingested_at      TIMESTAMP DEFAULT NOW(),
    UNIQUE (isrc)                                -- one ISRC should map to one recording row
);

CREATE INDEX idx_spotify_song_id ON spotify_recordings(song_id);

-- One row per matched YouTube video. A song has MANY videos.
CREATE TABLE youtube_videos (
    video_id        VARCHAR(20) PRIMARY KEY,     -- YouTube video ID (11 chars)
    song_id         VARCHAR(20) NOT NULL REFERENCES songs(song_id),
    channel_id      VARCHAR(30) NOT NULL,
    channel_title   TEXT,
    video_title     TEXT NOT NULL,
    published_at    TIMESTAMP,
    view_count      BIGINT,
    match_score      NUMERIC(5,2) NOT NULL,
    match_status     VARCHAR(15) NOT NULL,
    ingested_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX idx_youtube_song_id ON youtube_videos(song_id);

-- Records that failed validation or matching -- quarantine, not deleted.
CREATE TABLE rejected_records (
    rejection_id    SERIAL PRIMARY KEY,
    source          VARCHAR(10) NOT NULL,        -- 'spotify' / 'youtube'
    song_id         VARCHAR(20),
    raw_payload     JSONB NOT NULL,               -- use VARIANT on Snowflake, JSON on BigQuery
    errors          TEXT[] NOT NULL,              -- use ARRAY<STRING> on BigQuery
    rejected_at     TIMESTAMP DEFAULT NOW()
);

-- Audit trail of every match attempt (including ambiguous/no_match) --
-- needed for the "detect data corruption/anomalies" requirement and
-- for tuning match thresholds later.
CREATE TABLE match_log (
    attempt_id      SERIAL PRIMARY KEY,
    song_id         VARCHAR(20) NOT NULL,
    source          VARCHAR(10) NOT NULL,
    candidate_id    VARCHAR(30) NOT NULL,
    score           NUMERIC(5,2) NOT NULL,
    status          VARCHAR(15) NOT NULL,
    created_at      TIMESTAMP DEFAULT NOW()
);

-- Pipeline run metadata -- powers monitoring/freshness checks.
CREATE TABLE pipeline_runs (
    run_id          SERIAL PRIMARY KEY,
    source          VARCHAR(10) NOT NULL,
    started_at      TIMESTAMP NOT NULL,
    finished_at     TIMESTAMP,
    status          VARCHAR(15) NOT NULL,         -- running / success / failed
    rows_ingested   INTEGER,
    rows_rejected   INTEGER,
    error_message   TEXT
);
