"""
End-to-end run for one batch of songs from the internal catalog:

  1. Load internal songs (sample CSV exported from the [DE TS] sheet)
  2. For each song: search Spotify (-> ISRCs) and YouTube (-> videos)
  3. Score/match every candidate back to the song (matching/matcher.py)
  4. Validate matched records (dq/validation.py)
  5. Dedupe and write good rows + rejects to Postgres
  6. Log run metadata to pipeline_runs for monitoring

Run: python main.py --input data/sample_songs.csv --limit 20
(limit exists because YouTube's daily quota is ~100 searches/day on
 the free tier -- see ingestion/youtube_client.py)
"""

import argparse
import csv
import logging
import time
from datetime import datetime

import psycopg2
from dotenv import load_dotenv
import os

from ingestion.spotify_client import SpotifyClient
from ingestion.youtube_client import YouTubeClient, YouTubeQuotaExceededError
from matching.matcher import match_song_to_candidates, MatchStatus
from dq.validation import validate_spotify_record, validate_youtube_record, dedupe_by_key

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("main")


def get_db_connection():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def load_internal_songs(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def run_spotify_stage(conn, songs: list[dict]) -> dict:
    client = SpotifyClient()
    stats = {"matched": 0, "ambiguous": 0, "no_match": 0, "rejected": 0}
    cur = conn.cursor()

    for song in songs:
        song_id, title, artist = song["CODE"], song["SONG TITLE"], song.get("ORIGINAL ARTIST") or None
        try:
            candidates = client.search_track(title, artist)
        except Exception as e:
            logger.error("Spotify search failed for %s: %s", song_id, e)
            continue

        cand_tuples = [(c.spotify_track_id, c.track_name, c.artist_name) for c in candidates]
        results = match_song_to_candidates(song_id, title, artist, cand_tuples)

        for match, cand in zip(results, candidates):
            cur.execute(
                """INSERT INTO match_log (song_id, source, candidate_id, score, status)
                   VALUES (%s, %s, %s, %s, %s)""",
                (song_id, "spotify", match.candidate_id, match.score, match.status.value),
            )
            stats[match.status.value] += 1

            if match.status != MatchStatus.MATCHED:
                continue

            report = validate_spotify_record(song_id, cand.isrc, cand.track_name, cand.artist_name)
            if not report.passed:
                stats["rejected"] += 1
                cur.execute(
                    """INSERT INTO rejected_records (source, song_id, raw_payload, errors)
                       VALUES (%s, %s, %s, %s)""",
                    ("spotify", song_id, psycopg2.extras.Json(cand.raw), report.errors),
                )
                continue

            cur.execute(
                """INSERT INTO spotify_recordings
                   (recording_id, song_id, isrc, track_name, artist_name, album_name,
                    release_date, duration_ms, match_score, match_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (isrc) DO NOTHING""",
                (cand.spotify_track_id, song_id, cand.isrc, cand.track_name, cand.artist_name,
                 cand.album_name, cand.release_date, cand.duration_ms, match.score, match.status.value),
            )

        time.sleep(0.1)  # light self-throttle, be polite to the API

    conn.commit()
    cur.close()
    return stats


def run_youtube_stage(conn, songs: list[dict]) -> dict:
    client = YouTubeClient()
    stats = {"matched": 0, "ambiguous": 0, "no_match": 0, "rejected": 0}
    cur = conn.cursor()

    for song in songs:
        song_id, title, artist = song["CODE"], song["SONG TITLE"], song.get("ORIGINAL ARTIST") or None
        try:
            candidates = client.search_videos(title, artist, max_results=10)
        except YouTubeQuotaExceededError:
            logger.warning("YouTube quota exhausted -- stopping stage early")
            break
        except Exception as e:
            logger.error("YouTube search failed for %s: %s", song_id, e)
            continue

        cand_tuples = [(v.video_id, v.video_title, v.channel_title) for v in candidates]
        results = match_song_to_candidates(song_id, title, artist, cand_tuples)

        for match, vid in zip(results, candidates):
            cur.execute(
                """INSERT INTO match_log (song_id, source, candidate_id, score, status)
                   VALUES (%s, %s, %s, %s, %s)""",
                (song_id, "youtube", match.candidate_id, match.score, match.status.value),
            )
            stats[match.status.value] += 1

            if match.status != MatchStatus.MATCHED:
                continue

            report = validate_youtube_record(song_id, vid.video_id, vid.channel_id, vid.video_title)
            if not report.passed:
                stats["rejected"] += 1
                cur.execute(
                    """INSERT INTO rejected_records (source, song_id, raw_payload, errors)
                       VALUES (%s, %s, %s, %s)""",
                    ("youtube", song_id, psycopg2.extras.Json(vid.raw), report.errors),
                )
                continue

            cur.execute(
                """INSERT INTO youtube_videos
                   (video_id, song_id, channel_id, channel_title, video_title,
                    published_at, match_score, match_status)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (video_id) DO NOTHING""",
                (vid.video_id, song_id, vid.channel_id, vid.channel_title, vid.video_title,
                 vid.published_at, match.score, match.status.value),
            )

    conn.commit()
    cur.close()
    return stats


def log_run(conn, source: str, started_at: datetime, status: str, rows_ingested: int, rows_rejected: int, error_message: str | None = None):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO pipeline_runs (source, started_at, finished_at, status, rows_ingested, rows_rejected, error_message)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (source, started_at, datetime.now(), status, rows_ingested, rows_rejected, error_message),
    )
    conn.commit()
    cur.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="CSV export of the internal song catalog sheet")
    parser.add_argument("--limit", type=int, default=20, help="Number of songs to process this run (mind API quotas)")
    args = parser.parse_args()

    load_dotenv()
    songs = load_internal_songs(args.input)[: args.limit]
    logger.info("Loaded %d songs from %s (limited to %d)", len(songs), args.input, args.limit)

    # Also insert/refresh the base `songs` rows before enrichment
    conn = get_db_connection()
    cur = conn.cursor()
    for s in songs:
        cur.execute(
            """INSERT INTO songs (song_id, song_title, original_artist)
               VALUES (%s,%s,%s)
               ON CONFLICT (song_id) DO UPDATE SET song_title = EXCLUDED.song_title""",
            (s["CODE"], s["SONG TITLE"], s.get("ORIGINAL ARTIST") or None),
        )
    conn.commit()
    cur.close()

    for source, stage_fn in [("spotify", run_spotify_stage), ("youtube", run_youtube_stage)]:
        started_at = datetime.now()
        try:
            stats = stage_fn(conn, songs)
            matched = stats["matched"]
            rejected = stats["rejected"]
            logger.info("%s stage done: %s", source, stats)
            log_run(conn, source, started_at, "success", matched, rejected)
        except Exception as e:
            logger.exception("%s stage failed", source)
            log_run(conn, source, started_at, "failed", 0, 0, str(e))

    conn.close()
    logger.info("Run complete.")


if __name__ == "__main__":
    import psycopg2.extras  # noqa: E402  (needed for Json adapter used above)
    main()
