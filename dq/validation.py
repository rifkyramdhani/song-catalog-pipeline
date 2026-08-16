"""
Data quality checks applied to enriched records before they're loaded
into the warehouse. Anything failing a hard check gets quarantined
instead of silently dropped or silently loaded.
"""

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("dq.validation")

# ISRC format: CC-XXX-YY-NNNNN (country, registrant, year, designation)
# stored/transmitted without dashes: CCXXXYYNNNNN (12 chars)
ISRC_PATTERN = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}\d{2}\d{5}$")


@dataclass
class ValidationReport:
    record_id: str
    passed: bool
    errors: list[str] = field(default_factory=list)


def validate_spotify_record(song_code: str, isrc: str | None, track_name: str, artist_name: str) -> ValidationReport:
    errors = []

    if not isrc:
        errors.append("missing_isrc")
    elif not ISRC_PATTERN.match(isrc.replace("-", "")):
        errors.append(f"malformed_isrc:{isrc}")

    if not track_name or not track_name.strip():
        errors.append("missing_track_name")

    if not artist_name or not artist_name.strip():
        errors.append("missing_artist_name")

    return ValidationReport(record_id=f"{song_code}:{isrc}", passed=not errors, errors=errors)


def validate_youtube_record(song_code: str, video_id: str, channel_id: str, video_title: str) -> ValidationReport:
    errors = []

    if not video_id or len(video_id) != 11:
        errors.append(f"malformed_video_id:{video_id}")

    if not channel_id:
        errors.append("missing_channel_id")

    if not video_title or not video_title.strip():
        errors.append("missing_video_title")

    return ValidationReport(record_id=video_id, passed=not errors, errors=errors)


def dedupe_by_key(records: list[dict], key_fields: list[str]) -> tuple[list[dict], list[dict]]:
    """
    Generic dedup: keeps first occurrence per composite key, returns
    (kept, dropped_duplicates) so drops can be logged, not silently lost.
    """
    seen = set()
    kept, dropped = [], []

    for rec in records:
        key = tuple(rec.get(f) for f in key_fields)
        if key in seen:
            dropped.append(rec)
        else:
            seen.add(key)
            kept.append(rec)

    if dropped:
        logger.warning("Dropped %d duplicate records on key %s", len(dropped), key_fields)

    return kept, dropped


def check_row_count_anomaly(current_count: int, previous_count: int, threshold_pct: float = 30.0) -> bool:
    """
    Basic anomaly guard for the orchestrator: if today's ingested row
    count drops more than threshold_pct vs the last successful run,
    treat as a likely upstream/API problem and flag for alert rather
    than silently loading a partial dataset.
    Returns True if anomalous (should alert).
    """
    if previous_count == 0:
        return False
    drop_pct = (previous_count - current_count) / previous_count * 100
    return drop_pct > threshold_pct
