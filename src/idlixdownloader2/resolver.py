#!/usr/bin/env python3
"""HTTP-only resolver for IDLIX / MajorPlay playback.

The IDLIX web player resolves a stream in four HTTP calls:

1. ``GET  /api/movies/{slug}`` (or ``/api/series/{slug}/season/{s}/episode/{e}``)
2. ``GET  /api/watch/play-info/{contentType}/{contentId}``
3. ``POST /api/watch/session/claim`` with ``{"gateToken": ...}``
4. ``POST {redeemUrl}`` with ``{"claim": ..., "mode": "browser"}``

Step 2 returns a short "gate" that unlocks after ~15 seconds (the pre-roll
ad).  The normal guest flow does not require a browser or a Turnstile token.
If the API ever asks for Turnstile verification, :class:`TurnstileRequired`
is raised; the downloader is browser-free and cannot continue in that case.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import parse_qs, quote, unquote, urlparse


API_BASE = "https://z2.idlixku.com/api"
ORIGIN = "https://z2.idlixku.com"

_MOVIE_RE = r"/(?:watch/)?movies?/([^/?#]+)"
_SERIES_RE = r"/(?:watch/)?series/([^/?#]+)(?:/season/(\d+)/episode/(\d+))?"


class ResolveError(Exception):
    """Playback could not be resolved over HTTP."""


class TurnstileRequired(ResolveError):
    """The API asked for a Cloudflare Turnstile token (unsupported)."""


class ContentNotFound(ResolveError):
    """The requested title or episode is not available."""


@dataclass
class ResolvedStream:
    """Everything needed to start downloading a resolved stream."""

    title: str
    config_url: str
    subtitles: list[dict[str, Any]] = field(default_factory=list)
    expires_at: int | None = None
    content_type: str = "movie"
    content_id: str = ""
    video_id: str | None = None
    max_height: int | None = None
    quality_ladder: list[int] = field(default_factory=list)
    page_url: str = ""
    season: int | None = None
    episode: int | None = None


def _short(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate an error body."""
    return " ".join((text or "").split())[:limit]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class IdlixResolver:
    """Resolve an IDLIX page URL to a MajorPlay master playlist URL."""

    def __init__(
        self,
        scraper: Any = None,
        api_base: str = API_BASE,
        sleep: Callable[[float], None] = time.sleep,
    ):
        if scraper is None:
            import cloudscraper

            scraper = cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "desktop": True}
            )
        self.scraper = scraper
        self.api_base = api_base.rstrip("/")
        self._sleep = sleep
        self._headers = {
            "Accept": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
        }

    # ------------------------------------------------------------------
    # URL parsing
    # ------------------------------------------------------------------
    @staticmethod
    def parse_page_url(page_url: str) -> dict[str, Any]:
        """Extract content type, slug, season, and episode from an IDLIX URL."""
        parsed = urlparse(page_url)
        path = parsed.path
        query = parse_qs(parsed.query)

        series_match = re.search(_SERIES_RE, path)
        if series_match:
            season = (
                int(series_match.group(2))
                if series_match.group(2)
                else _int_or_none(query.get("season", [None])[0])
            )
            episode = (
                int(series_match.group(3))
                if series_match.group(3)
                else _int_or_none(query.get("episode", [None])[0])
            )
            return {
                "kind": "series",
                "slug": unquote(series_match.group(1)),
                "season": season,
                "episode": episode,
            }

        movie_match = re.search(_MOVIE_RE, path)
        if movie_match:
            return {
                "kind": "movie",
                "slug": unquote(movie_match.group(1)),
                "season": None,
                "episode": None,
            }

        raise ResolveError(f"Unsupported IDLIX URL: {page_url}")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def resolve(
        self,
        page_url: str,
        season: int | None = None,
        episode: int | None = None,
        timeout: int = 30,
    ) -> ResolvedStream:
        """Resolve ``page_url`` to a master playlist without a browser."""
        parsed = self.parse_page_url(page_url)

        if parsed["kind"] == "movie":
            return self._resolve_movie(parsed["slug"], page_url, timeout)

        return self._resolve_series(
            parsed["slug"],
            season if season is not None else parsed["season"],
            episode if episode is not None else parsed["episode"],
            page_url,
            timeout,
        )

    # ------------------------------------------------------------------
    # Series browsing
    # ------------------------------------------------------------------
    def search(self, query: str, timeout: int = 30) -> list[dict[str, Any]]:
        """Search movies and series by title."""
        data = self._get_json(f"/search?q={quote(query)}", timeout)
        return data.get("results") or []

    def list_seasons(
        self, page_url: str, timeout: int = 30
    ) -> tuple[str, list[dict[str, Any]]]:
        """Return ``(series_title, seasons)`` for a series page URL."""
        parsed = self.parse_page_url(page_url)
        if parsed["kind"] != "series":
            raise ResolveError(f"Not a series URL: {page_url}")
        data = self._get_json(f"/series/{quote(parsed['slug'])}", timeout)
        return data.get("title") or parsed["slug"], data.get("seasons") or []

    def list_episodes(
        self, page_url: str, season: int, timeout: int = 30
    ) -> list[dict[str, Any]]:
        """Return the episode list for one season of a series page URL."""
        parsed = self.parse_page_url(page_url)
        if parsed["kind"] != "series":
            raise ResolveError(f"Not a series URL: {page_url}")
        data = self._get_json(
            f"/series/{quote(parsed['slug'])}/season/{season}", timeout
        )
        season_data = data.get("season") or {}
        return season_data.get("episodes") or []

    # ------------------------------------------------------------------
    # Content lookup
    # ------------------------------------------------------------------
    def _resolve_movie(self, slug: str, page_url: str, timeout: int) -> ResolvedStream:
        data = self._get_json(f"/movies/{quote(slug)}", timeout)
        content_id = data.get("id")
        if not content_id:
            raise ResolveError("Movie response has no id")
        if data.get("hasVideo") is False:
            raise ContentNotFound(f"No video available for movie: {slug}")

        play = self._open_play_session("movie", content_id, timeout)
        return self._build_result(
            title=data.get("title") or slug,
            claim=play,
            page_url=page_url,
            content_type="movie",
            content_id=content_id,
            timeout=timeout,
        )

    def _resolve_series(
        self,
        slug: str,
        season: int | None,
        episode: int | None,
        page_url: str,
        timeout: int,
    ) -> ResolvedStream:
        series = self._get_json(f"/series/{quote(slug)}", timeout)
        title = series.get("title") or slug

        if season is None:
            seasons = series.get("seasons") or []
            season = _int_or_none(seasons[0].get("seasonNumber")) if seasons else 1
            season = season or 1
        if episode is None:
            episode = 1

        detail = self._get_json(
            f"/series/{quote(slug)}/season/{season}/episode/{episode}", timeout
        )
        ep = detail.get("episode") or {}
        content_id = ep.get("id")
        if not content_id:
            raise ResolveError(f"Episode not found: {slug} S{season}E{episode}")
        if ep.get("hasVideo") is False:
            raise ContentNotFound(
                f"No video available for episode: {slug} S{season}E{episode}"
            )

        play = self._open_play_session("episode", content_id, timeout)
        label = f"{title} S{season:02d}E{episode:02d}"
        if ep.get("name"):
            label = f"{label} - {ep['name']}"

        return self._build_result(
            title=label,
            claim=play,
            page_url=page_url,
            content_type="episode",
            content_id=content_id,
            timeout=timeout,
            season=season,
            episode=episode,
        )

    # ------------------------------------------------------------------
    # Playback session
    # ------------------------------------------------------------------
    def _open_play_session(
        self, content_type: str, content_id: str, timeout: int
    ) -> dict[str, Any]:
        play = self._get_json(
            f"/watch/play-info/{content_type}/{quote(content_id)}", timeout
        )
        kind = play.get("kind")

        if kind == "pentos":
            return play
        if kind == "gate":
            self._reject_turnstile(play)
            return self._claim_until_pentos(play, timeout)

        raise ResolveError(f"Unexpected play-info response: {kind!r}")

    @staticmethod
    def _reject_turnstile(payload: dict[str, Any]) -> None:
        if payload.get("verification"):
            raise TurnstileRequired(
                "Playback verification (Cloudflare Turnstile) required"
            )

    def _claim_until_pentos(
        self, gate: dict[str, Any], timeout: int
    ) -> dict[str, Any]:
        """Wait for the ad gate, then exchange the gate token for a claim."""
        current = dict(gate)
        for _ in range(12):
            self._wait_for_unlock(current)

            body: dict[str, Any] = {"gateToken": current.get("gateToken")}
            if current.get("verificationToken"):
                body["verificationToken"] = current["verificationToken"]

            response = self.scraper.post(
                f"{self.api_base}/watch/session/claim",
                json=body,
                headers=self._headers,
                timeout=timeout,
            )
            if response.status_code != 200:
                raise ResolveError(
                    f"Session claim failed: HTTP {response.status_code} "
                    f"{_short(response.text)}"
                )

            try:
                claim = response.json()
            except ValueError as exc:
                raise ResolveError("Session claim returned non-JSON") from exc

            if claim.get("kind") == "pentos":
                return claim

            self._reject_turnstile(claim)
            if claim.get("kind") not in ("gate", "pending"):
                raise ResolveError(
                    f"Unexpected session claim response: {claim.get('kind')!r}"
                )
            current = {**current, **claim}

        raise ResolveError("Playback session did not unlock after 12 attempts")

    def _wait_for_unlock(self, gate: dict[str, Any]) -> None:
        """Sleep until the server-side ad gate opens."""
        unlock_at = gate.get("unlockAt")
        server_now = gate.get("serverNow")
        if isinstance(unlock_at, (int, float)) and isinstance(server_now, (int, float)):
            wait = (unlock_at - server_now) / 1000.0
        else:
            remaining = gate.get("remainingMs")
            wait = remaining / 1000.0 if isinstance(remaining, (int, float)) else 0

        if wait > 0:
            # Cap each wait so a bogus value cannot hang the process forever.
            self._sleep(min(wait + 0.25, 16.0))

    def _redeem(self, claim: dict[str, Any], timeout: int) -> dict[str, Any]:
        """Exchange a playback claim for the actual master playlist URL."""
        redeem_url = claim.get("redeemUrl")
        if not redeem_url:
            raise ResolveError("Claim response has no redeemUrl")

        response = self.scraper.post(
            redeem_url,
            json={"claim": claim.get("claim"), "mode": "browser"},
            headers=self._headers,
            timeout=timeout,
        )
        if response.status_code != 200:
            raise ResolveError(
                f"Redeem failed: HTTP {response.status_code} {_short(response.text)}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ResolveError("Redeem returned non-JSON") from exc

        if not data.get("url"):
            raise ResolveError("Redeem response has no master playlist URL")
        return data

    def _build_result(
        self,
        title: str,
        claim: dict[str, Any],
        page_url: str,
        content_type: str,
        content_id: str,
        timeout: int,
        season: int | None = None,
        episode: int | None = None,
    ) -> ResolvedStream:
        redeem = self._redeem(claim, timeout)

        subtitles: list[dict[str, Any]] = []
        for sub in redeem.get("subtitles") or []:
            path = sub.get("path") or sub.get("url")
            if not path:
                continue
            subtitles.append(
                {
                    "url": path,
                    "name": sub.get("label") or sub.get("name") or "Subtitle",
                    "language": sub.get("lang") or sub.get("language"),
                }
            )

        quality_ladder = [
            int(item)
            for item in (claim.get("qualityLadder") or [])
            if isinstance(item, (int, float)) and int(item) > 0
        ]

        return ResolvedStream(
            title=title,
            config_url=redeem["url"],
            subtitles=subtitles,
            expires_at=redeem.get("expiresAt"),
            content_type=content_type,
            content_id=content_id,
            video_id=redeem.get("videoId") or claim.get("videoId"),
            max_height=claim.get("maxHeight"),
            quality_ladder=quality_ladder,
            page_url=page_url,
            season=season,
            episode=episode,
        )

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _get_json(self, path_or_url: str, timeout: int) -> dict[str, Any]:
        url = (
            path_or_url
            if path_or_url.startswith("http")
            else f"{self.api_base}{path_or_url}"
        )
        response = self.scraper.get(url, headers=self._headers, timeout=timeout)

        if response.status_code == 404:
            raise ContentNotFound(f"Not found: {url}")
        if response.status_code != 200:
            raise ResolveError(
                f"GET {url} failed: HTTP {response.status_code} "
                f"{_short(response.text)}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise ResolveError(f"GET {url} returned non-JSON") from exc

        if not isinstance(data, dict):
            raise ResolveError(f"GET {url} returned an unexpected payload")
        return data
