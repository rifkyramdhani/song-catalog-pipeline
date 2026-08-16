"""
Matches Spotify/YouTube search results back to our internal song catalog
(CODE, ORIGINAL_ARTIST, SONG_TITLE).

Why this exists: the internal sheet has ~30%+ blank ORIGINAL_ARTIST, and
API search is title-based, so a naive exact-match will silently attach
the wrong recording to the wrong song (e.g. two different songs both
titled "Rindu"). We score every candidate and only auto-accept high
confidence matches; everything else goes to manual review.
"""

import logging
from dataclasses import dataclass
from enum import Enum

from rapidfuzz import fuzz

logger = logging.getLogger("matching.matcher")

# Tune these after looking at real match-score distribution from a sample run.
AUTO_ACCEPT_THRESHOLD = 90
REVIEW_THRESHOLD = 70


class MatchStatus(str, Enum):
    MATCHED = "matched"          # high confidence, auto-accepted
    AMBIGUOUS = "ambiguous"      # multiple close candidates or mid confidence
    NO_MATCH = "no_match"        # nothing above review threshold


@dataclass
class MatchResult:
    song_code: str
    candidate_id: str            # spotify_track_id or youtube video_id
    score: float
    status: MatchStatus
    matched_title: str
    matched_artist: str | None


def score_candidate(song_title: str, artist_name: str | None, cand_title: str, cand_artist: str | None) -> float:
    """
    Weighted similarity score (0-100).
    Title carries most weight since artist is frequently missing internally.
    """
    title_score = fuzz.token_sort_ratio(song_title.lower(), cand_title.lower())

    if artist_name and cand_artist:
        artist_score = fuzz.token_sort_ratio(artist_name.lower(), cand_artist.lower())
        return round(0.7 * title_score + 0.3 * artist_score, 2)

    # No internal artist to compare against -- title match only, but cap
    # the score so it can't auto-accept purely on a generic title.
    return round(min(title_score, 92), 2)


def match_song_to_candidates(
    song_code: str,
    song_title: str,
    artist_name: str | None,
    candidates: list[tuple[str, str, str | None]],  # (candidate_id, title, artist)
) -> list[MatchResult]:
    """
    Score every API candidate against one internal song row.
    Returns results sorted best-first; caller decides how many to keep
    (Spotify: keep all matched -> multiple ISRCs is expected/one-to-many;
     YouTube: keep all matched -> multiple videos is expected too).
    """
    scored = []
    for cand_id, cand_title, cand_artist in candidates:
        score = score_candidate(song_title, artist_name, cand_title, cand_artist)

        if score >= AUTO_ACCEPT_THRESHOLD:
            status = MatchStatus.MATCHED
        elif score >= REVIEW_THRESHOLD:
            status = MatchStatus.AMBIGUOUS
        else:
            status = MatchStatus.NO_MATCH

        scored.append(
            MatchResult(
                song_code=song_code,
                candidate_id=cand_id,
                score=score,
                status=status,
                matched_title=cand_title,
                matched_artist=cand_artist,
            )
        )

    scored.sort(key=lambda r: r.score, reverse=True)

    if scored and scored[0].status == MatchStatus.MATCHED:
        # if #2 candidate is nearly as good as #1, flag both ambiguous --
        # avoids silently picking the wrong one of two near-identical titles
        if len(scored) > 1 and (scored[0].score - scored[1].score) < 5 and scored[1].score >= REVIEW_THRESHOLD:
            scored[0].status = MatchStatus.AMBIGUOUS
            scored[1].status = MatchStatus.AMBIGUOUS
            logger.info("Close-score tie for song %s, flagging both for review", song_code)

    return scored
