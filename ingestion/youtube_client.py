"""
YouTube ingestion client.

Uses YouTube Data API v3 (API key auth, no OAuth needed for public search).
search.list costs 100 quota units/call, videos.list costs 1 unit/call --
we deliberately split search + detail fetch so we only pay the 100-unit
cost once per song, then batch-enrich with a cheap videos.list call.

Docs:
- Search: https://developers.google.com/youtube/v3/docs/search/list
- Videos: https://developers.google.com/youtube/v3/docs/videos/list
- Quota:  https://developers.google.com/youtube/v3/determine_quota_cost
"""

import logging
import os
from dataclasses import dataclass, field

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("ingestion.youtube")

SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

DEFAULT_DAILY_QUOTA = 10_000
SEARCH_COST = 100
VIDEOS_LIST_COST = 1


@dataclass
class YouTubeVideo:
    video_id: str
    channel_id: str
    channel_title: str
    video_title: str
    published_at: str
    view_count: int | None = None
    raw: dict = field(repr=False, default_factory=dict)


class YouTubeQuotaExceededError(Exception):
    pass


class YouTubeClient:
    def __init__(self, api_key: str | None = None, daily_quota: int = DEFAULT_DAILY_QUOTA):
        self.api_key = api_key or os.environ["YOUTUBE_API_KEY"]
        self.daily_quota = daily_quota
        self._quota_used = 0

    def _spend_quota(self, cost: int):
        self._quota_used += cost
        if self._quota_used > self.daily_quota:
            raise YouTubeQuotaExceededError(
                f"Would exceed daily quota ({self._quota_used}/{self.daily_quota} units). "
                "Stop or request a quota increase in Google Cloud Console."
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=20),
        retry=retry_if_exception_type(requests.exceptions.RequestException),
        reraise=True,
    )
    def search_videos(self, song_title: str, artist_name: str | None = None, max_results: int = 10) -> list[YouTubeVideo]:
        """
        Search for videos matching a song title (+ optional artist).
        Costs 100 quota units per call -- keep max_results modest.
        """
        self._spend_quota(SEARCH_COST)

        query = song_title if not artist_name else f"{artist_name} {song_title}"
        params = {
            "key": self.api_key,
            "q": query,
            "part": "snippet",
            "type": "video",
            "maxResults": max_results,
        }

        resp = requests.get(SEARCH_URL, params=params, timeout=10)
        if resp.status_code == 403 and "quotaExceeded" in resp.text:
            raise YouTubeQuotaExceededError("YouTube API daily quota exceeded (403).")
        resp.raise_for_status()

        items = resp.json().get("items", [])
        videos = []
        for item in items:
            snippet = item["snippet"]
            videos.append(
                YouTubeVideo(
                    video_id=item["id"]["videoId"],
                    channel_id=snippet["channelId"],
                    channel_title=snippet["channelTitle"],
                    video_title=snippet["title"],
                    published_at=snippet["publishedAt"],
                    raw=item,
                )
            )
        return videos

    def enrich_with_stats(self, video_ids: list[str]) -> dict[str, int]:
        """Cheap batch call (1 unit total, up to 50 IDs) to pull view counts."""
        if not video_ids:
            return {}
        self._spend_quota(VIDEOS_LIST_COST)

        params = {
            "key": self.api_key,
            "id": ",".join(video_ids[:50]),
            "part": "statistics",
        }
        resp = requests.get(VIDEOS_URL, params=params, timeout=10)
        resp.raise_for_status()

        return {
            item["id"]: int(item["statistics"].get("viewCount", 0))
            for item in resp.json().get("items", [])
        }

    @property
    def quota_used(self) -> int:
        return self._quota_used


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv()

    client = YouTubeClient()
    vids = client.search_videos("Tanda Mata", "Glenn Fredly", max_results=5)
    for v in vids:
        print(v.video_id, "|", v.channel_title, "|", v.video_title)
    print("Quota used:", client.quota_used)
