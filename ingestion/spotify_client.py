"""
Spotify ingestion client.

Uses the Client Credentials Flow (app-only auth, no user login needed)
to search for tracks matching our internal catalog (song_title + artist)
and pull back ISRC + metadata for every matching recording.

Docs:
- Auth:   https://developer.spotify.com/documentation/web-api/tutorials/client-credentials-flow
- Search: https://developer.spotify.com/documentation/web-api/reference/search
- Track:  https://developer.spotify.com/documentation/web-api/reference/get-track
"""

import base64
import logging
import os
import time
from dataclasses import dataclass, field

import requests
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

logger = logging.getLogger("ingestion.spotify")

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"


@dataclass
class SpotifyRecording:
    """One recording (= one ISRC) returned by Spotify for a searched song."""
    spotify_track_id: str
    isrc: str | None
    track_name: str
    artist_name: str
    album_name: str
    release_date: str | None
    duration_ms: int
    raw: dict = field(repr=False, default_factory=dict)


class SpotifyRateLimitError(Exception):
    """Raised on HTTP 429; caller should back off using Retry-After."""
    pass


class SpotifyClient:
    def __init__(self, client_id: str | None = None, client_secret: str | None = None):
        self.client_id = client_id or os.environ["SPOTIFY_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["SPOTIFY_CLIENT_SECRET"]
        self._token = None
        self._token_expires_at = 0

    # ---- auth -----------------------------------------------------------
    def _get_token(self) -> str:
        """Fetch (and cache) an app-only access token via Client Credentials Flow."""
        if self._token and time.time() < self._token_expires_at - 30:
            return self._token

        auth_header = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        resp = requests.post(
            TOKEN_URL,
            headers={"Authorization": f"Basic {auth_header}"},
            data={"grant_type": "client_credentials"},
            timeout=10,
        )
        resp.raise_for_status()
        payload = resp.json()

        self._token = payload["access_token"]
        self._token_expires_at = time.time() + payload["expires_in"]
        logger.info("Fetched new Spotify token, expires in %ss", payload["expires_in"])
        return self._token

    # ---- search -----------------------------------------------------------
    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type(SpotifyRateLimitError),
        reraise=True,
    )
    def search_track(self, song_title: str, artist_name: str | None = None, limit: int = 10) -> list[SpotifyRecording]:
        """
        Search Spotify for a track by title (+ optional artist).
        Returns every matching recording (each = a distinct ISRC candidate).
        """
        query = song_title if not artist_name else f"{song_title} {artist_name}"

        headers = {"Authorization": f"Bearer {self._get_token()}"}
        params = {"q": query, "type": "track", "limit": limit}

        resp = requests.get(SEARCH_URL, headers=headers, params=params, timeout=10)

        if resp.status_code == 429:
            retry_after = int(resp.headers.get("Retry-After", 5))
            logger.warning("Spotify rate limited, retry after %ss", retry_after)
            time.sleep(retry_after)
            raise SpotifyRateLimitError()
        if resp.status_code == 403:
            logger.error("403 response body: %s", resp.text)
        resp.raise_for_status()
        items = resp.json().get("tracks", {}).get("items", [])

        results = []
        for item in items:
            results.append(
                SpotifyRecording(
                    spotify_track_id=item["id"],
                    isrc=item.get("external_ids", {}).get("isrc"),
                    track_name=item["name"],
                    artist_name=", ".join(a["name"] for a in item["artists"]),
                    album_name=item["album"]["name"],
                    release_date=item["album"].get("release_date"),
                    duration_ms=item["duration_ms"],
                    raw=item,
                )
            )
        return results

    def get_track_by_id(self, spotify_track_id: str) -> SpotifyRecording | None:
        """Fetch a single track directly, used when re-validating a stored ID."""
        headers = {"Authorization": f"Bearer {self._get_token()}"}
        resp = requests.get(
            f"https://api.spotify.com/v1/tracks/{spotify_track_id}",
            headers=headers,
            timeout=10,
        )
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        item = resp.json()
        return SpotifyRecording(
            spotify_track_id=item["id"],
            isrc=item.get("external_ids", {}).get("isrc"),
            track_name=item["name"],
            artist_name=", ".join(a["name"] for a in item["artists"]),
            album_name=item["album"]["name"],
            release_date=item["album"].get("release_date"),
            duration_ms=item["duration_ms"],
            raw=item,
        )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    from dotenv import load_dotenv
    load_dotenv()

    client = SpotifyClient()
    recs = client.search_track("Tanda Mata", "Glenn Fredly")
    for r in recs:
        print(r.isrc, "|", r.artist_name, "-", r.track_name, "|", r.album_name)
