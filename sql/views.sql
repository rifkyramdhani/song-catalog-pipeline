-- ============================================================
-- Gold-layer views: answer the two business questions directly.
-- Only 'matched' status counted by default -- ambiguous/no_match
-- rows are visible via the *_all views for analysts who want them.
-- ============================================================

-- Q1: How many ISRCs (distinct recordings) does each song have in Spotify?
CREATE OR REPLACE VIEW song_isrc_summary AS
SELECT
    s.song_id,
    s.song_title,
    s.original_artist,
    COUNT(sr.recording_id) FILTER (WHERE sr.match_status = 'matched') AS isrc_count
FROM songs s
LEFT JOIN spotify_recordings sr ON sr.song_id = s.song_id
GROUP BY s.song_id, s.song_title, s.original_artist;

-- Q2: How many videos does each song have on YouTube?
CREATE OR REPLACE VIEW song_video_summary AS
SELECT
    s.song_id,
    s.song_title,
    s.original_artist,
    COUNT(yv.video_id) FILTER (WHERE yv.match_status = 'matched') AS video_count
FROM songs s
LEFT JOIN youtube_videos yv ON yv.song_id = s.song_id
GROUP BY s.song_id, s.song_title, s.original_artist;

-- Combined dashboard view -- one row per song, both metrics.
CREATE OR REPLACE VIEW song_catalog_summary AS
SELECT
    i.song_id,
    i.song_title,
    i.original_artist,
    i.isrc_count,
    v.video_count
FROM song_isrc_summary i
JOIN song_video_summary v ON v.song_id = i.song_id;

-- Ops view: songs still needing manual match review.
CREATE OR REPLACE VIEW songs_pending_review AS
SELECT song_id, source, candidate_id, score, status, created_at
FROM match_log
WHERE status IN ('ambiguous', 'no_match')
ORDER BY score DESC;

-- Ops view: pipeline freshness / last successful run per source.
CREATE OR REPLACE VIEW pipeline_freshness AS
SELECT
    source,
    MAX(finished_at) FILTER (WHERE status = 'success') AS last_success_at,
    NOW() - MAX(finished_at) FILTER (WHERE status = 'success') AS staleness
FROM pipeline_runs
GROUP BY source;
