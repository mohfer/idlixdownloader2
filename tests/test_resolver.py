#!/usr/bin/env python3
"""Tests for the browser-free IDLIX API resolver."""

import json
import unittest
from unittest.mock import Mock

from idlixdownloader2 import MajorPlayDownloader
from idlixdownloader2.resolver import (
    ContentNotFound,
    IdlixResolver,
    ResolvedStream,
    ResolveError,
    TurnstileRequired,
)


def make_response(status=200, payload=None, text=None):
    response = Mock()
    response.status_code = status
    response.text = text if text is not None else json.dumps(payload or {})
    response.json.return_value = payload
    return response


class FakeScraper:
    """Small URL-prefix router with queued responses."""

    def __init__(self):
        self.routes = {}
        self.get_calls = []
        self.post_calls = []

    def add(self, method, prefix, *responses):
        self.routes[(method, prefix)] = list(responses)

    def _call(self, method, url, **kwargs):
        calls = self.get_calls if method == "GET" else self.post_calls
        calls.append((url, kwargs))
        matches = [
            (prefix, responses)
            for (route_method, prefix), responses in self.routes.items()
            if route_method == method and url.startswith(prefix)
        ]
        if not matches:
            raise AssertionError(f"unexpected {method} {url}")
        _, responses = max(matches, key=lambda item: len(item[0]))
        if len(responses) > 1:
            return responses.pop(0)
        return responses[0]

    def get(self, url, **kwargs):
        return self._call("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._call("POST", url, **kwargs)


MOVIE_ID = "f4f5c676-b701-496c-a6ab-a096d529de18"
CLAIM = {
    "kind": "pentos",
    "claim": "claim-token",
    "redeemUrl": "https://e2e.majorplay.net/api/play",
    "videoId": "video-id",
    "maxHeight": 1080,
    "qualityLadder": [1080, 720],
}
REDEEM = {
    "code": "ok",
    "url": "https://e2e.majorplay.net/v/z5/video-id/config-1.json?t=token&pm=browser",
    "expiresAt": 1793269232,
    "subtitles": [
        {
            "lang": "id",
            "label": "Indonesian",
            "path": "https://e2e.majorplay.net/v/z5/video-id/i18n/id/sub.vtt",
        }
    ],
    "videoId": "video-id",
}


class TestUrlParsing(unittest.TestCase):
    def test_movie_url(self):
        parsed = IdlixResolver.parse_page_url(
            "https://z2.idlixku.com/movie/toy-story-5-2026"
        )
        self.assertEqual(parsed["kind"], "movie")
        self.assertEqual(parsed["slug"], "toy-story-5-2026")
        self.assertIsNone(parsed["season"])
        self.assertIsNone(parsed["episode"])

    def test_watch_movie_url(self):
        parsed = IdlixResolver.parse_page_url(
            "https://z2.idlixku.com/watch/movie/toy-story-5-2026?play=1"
        )
        self.assertEqual(parsed["kind"], "movie")
        self.assertEqual(parsed["slug"], "toy-story-5-2026")

    def test_series_url_with_episode(self):
        parsed = IdlixResolver.parse_page_url(
            "https://z2.idlixku.com/series/oh-my-guard-2026/season/2/episode/3"
        )
        self.assertEqual(parsed["kind"], "series")
        self.assertEqual(parsed["slug"], "oh-my-guard-2026")
        self.assertEqual(parsed["season"], 2)
        self.assertEqual(parsed["episode"], 3)

    def test_series_query_episode(self):
        parsed = IdlixResolver.parse_page_url(
            "https://z2.idlixku.com/series/oh-my-guard-2026?season=1&episode=4"
        )
        self.assertEqual(parsed["season"], 1)
        self.assertEqual(parsed["episode"], 4)

    def test_unsupported_url(self):
        with self.assertRaises(ResolveError):
            IdlixResolver.parse_page_url("https://z2.idlixku.com/search?q=test")


class TestMovieResolution(unittest.TestCase):
    def test_gate_claim_redeem_flow(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            f"https://z2.idlixku.com/api/movies/toy-story-5-2026",
            make_response(
                200,
                {
                    "id": MOVIE_ID,
                    "title": "Toy Story 5",
                    "hasVideo": True,
                },
            ),
        )
        scraper.add(
            "GET",
            f"https://z2.idlixku.com/api/watch/play-info/movie/{MOVIE_ID}",
            make_response(
                200,
                {
                    "kind": "gate",
                    "gateToken": "gate-token",
                    "serverNow": 1000,
                    "unlockAt": 2000,
                },
            ),
        )
        scraper.add(
            "POST",
            "https://z2.idlixku.com/api/watch/session/claim",
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(200, REDEEM),
        )

        slept = []
        resolver = IdlixResolver(scraper=scraper, sleep=slept.append)
        result = resolver.resolve("https://z2.idlixku.com/movie/toy-story-5-2026")

        self.assertEqual(result.title, "Toy Story 5")
        self.assertEqual(result.config_url, REDEEM["url"])
        self.assertEqual(result.expires_at, REDEEM["expiresAt"])
        self.assertEqual(result.content_type, "movie")
        self.assertEqual(result.content_id, MOVIE_ID)
        self.assertEqual(result.video_id, "video-id")
        self.assertEqual(result.max_height, 1080)
        self.assertEqual(result.quality_ladder, [1080, 720])
        self.assertEqual(
            result.subtitles,
            [
                {
                    "url": "https://e2e.majorplay.net/v/z5/video-id/i18n/id/sub.vtt",
                    "name": "Indonesian",
                    "language": "id",
                }
            ],
        )
        self.assertEqual(slept, [1.25])

        claim_call = scraper.post_calls[0]
        self.assertEqual(
            claim_call[1]["json"], {"gateToken": "gate-token"}
        )
        redeem_call = scraper.post_calls[1]
        self.assertEqual(
            redeem_call[1]["json"], {"claim": "claim-token", "mode": "browser"}
        )

    def test_direct_pentos_skips_claim(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/movies/direct",
            make_response(
                200, {"id": "direct-id", "title": "Direct", "hasVideo": True}
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/movie/direct-id",
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(200, REDEEM),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        result = resolver.resolve("https://z2.idlixku.com/movie/direct")

        self.assertEqual(result.config_url, REDEEM["url"])
        self.assertEqual(len(scraper.post_calls), 1)
        self.assertTrue(scraper.post_calls[0][0].endswith("/api/play"))

    def test_pending_claim_retries(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/movies/pending",
            make_response(
                200, {"id": "pending-id", "title": "Pending", "hasVideo": True}
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/movie/pending-id",
            make_response(
                200,
                {
                    "kind": "gate",
                    "gateToken": "gate-token",
                    "serverNow": 1000,
                    "unlockAt": 1000,
                },
            ),
        )
        scraper.add(
            "POST",
            "https://z2.idlixku.com/api/watch/session/claim",
            make_response(
                200,
                {
                    "kind": "pending",
                    "serverNow": 1000,
                    "unlockAt": 1500,
                    "remainingMs": 500,
                },
            ),
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(200, REDEEM),
        )

        slept = []
        resolver = IdlixResolver(scraper=scraper, sleep=slept.append)
        result = resolver.resolve("https://z2.idlixku.com/movie/pending")

        self.assertEqual(result.config_url, REDEEM["url"])
        self.assertEqual(len(scraper.post_calls), 3)  # claim, claim, redeem
        self.assertEqual(slept, [0.75])

    def test_turnstile_required(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/movies/guarded",
            make_response(
                200, {"id": "guarded-id", "title": "Guarded", "hasVideo": True}
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/movie/guarded-id",
            make_response(
                200,
                {
                    "kind": "gate",
                    "gateToken": "gate-token",
                    "serverNow": 1000,
                    "unlockAt": 2000,
                    "verification": {"siteKey": "turnstile-site-key"},
                },
            ),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        with self.assertRaises(TurnstileRequired):
            resolver.resolve("https://z2.idlixku.com/movie/guarded")

    def test_no_video_raises(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/movies/empty",
            make_response(
                200, {"id": "empty-id", "title": "Empty", "hasVideo": False}
            ),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        with self.assertRaises(ContentNotFound):
            resolver.resolve("https://z2.idlixku.com/movie/empty")

    def test_redeem_failure(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/movies/broken",
            make_response(
                200, {"id": "broken-id", "title": "Broken", "hasVideo": True}
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/movie/broken-id",
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(410, {"error": "claim expired"}, text='{"error":"claim expired"}'),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        with self.assertRaises(ResolveError):
            resolver.resolve("https://z2.idlixku.com/movie/broken")


class TestSeriesResolution(unittest.TestCase):
    def test_series_episode_flow(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/series/test-series",
            make_response(
                200,
                {
                    "id": "series-id",
                    "title": "Test Series",
                    "seasons": [{"seasonNumber": 1}],
                },
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/series/test-series/season/1/episode/2",
            make_response(
                200,
                {
                    "episode": {
                        "id": "episode-id",
                        "name": "Pilot",
                        "hasVideo": True,
                    }
                },
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/episode/episode-id",
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(200, REDEEM),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        result = resolver.resolve(
            "https://z2.idlixku.com/series/test-series/season/1/episode/2"
        )

        self.assertEqual(result.title, "Test Series S01E02 - Pilot")
        self.assertEqual(result.content_type, "episode")
        self.assertEqual(result.content_id, "episode-id")
        self.assertEqual(result.season, 1)
        self.assertEqual(result.episode, 2)
        self.assertEqual(result.config_url, REDEEM["url"])

    def test_series_defaults_to_first_episode(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/series/test-series",
            make_response(
                200,
                {
                    "id": "series-id",
                    "title": "Test Series",
                    "seasons": [{"seasonNumber": 3}],
                },
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/series/test-series/season/3/episode/1",
            make_response(
                200,
                {"episode": {"id": "episode-id", "name": "One", "hasVideo": True}},
            ),
        )
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/watch/play-info/episode/episode-id",
            make_response(200, CLAIM),
        )
        scraper.add(
            "POST",
            "https://e2e.majorplay.net/api/play",
            make_response(200, REDEEM),
        )

        resolver = IdlixResolver(scraper=scraper, sleep=lambda _: None)
        result = resolver.resolve("https://z2.idlixku.com/series/test-series")

        self.assertEqual(result.season, 3)
        self.assertEqual(result.episode, 1)


class TestDownloaderIntegration(unittest.TestCase):
    def setUp(self):
        self.downloader = MajorPlayDownloader()

    def test_resolve_via_api_populates_downloader(self):
        self.downloader.resolver = Mock()
        self.downloader.resolver.resolve.return_value = ResolvedStream(
            title="Toy Story 5",
            config_url=REDEEM["url"],
            subtitles=[
                {
                    "url": "https://e2e.majorplay.net/v/z5/video-id/i18n/id/sub.vtt",
                    "name": "Indonesian",
                    "language": "id",
                }
            ],
            expires_at=REDEEM["expiresAt"],
        )

        result = self.downloader.resolve_via_api(
            "https://z2.idlixku.com/movie/toy-story-5-2026"
        )

        self.assertTrue(result)
        self.assertEqual(self.downloader.video_name, "Toy Story 5")
        self.assertEqual(self.downloader.config_url, REDEEM["url"])
        self.assertEqual(self.downloader.config_expires_at, REDEEM["expiresAt"])
        self.assertEqual(len(self.downloader.subtitles), 1)

    def test_resolve_via_api_turnstile_returns_false(self):
        self.downloader.resolver = Mock()
        self.downloader.resolver.resolve.side_effect = TurnstileRequired("blocked")

        result = self.downloader.resolve_via_api(
            "https://z2.idlixku.com/movie/toy-story-5-2026"
        )

        self.assertFalse(result)
        self.assertIsNone(self.downloader.config_url)

    def test_add_subtitle_deduplicates_by_base_url(self):
        self.assertTrue(
            self.downloader.add_subtitle(
                "https://e2e.majorplay.net/sub.vtt?t=old", "Indonesian", "id"
            )
        )
        self.assertFalse(
            self.downloader.add_subtitle(
                "https://e2e.majorplay.net/sub.vtt?t=new", "Indonesian", "id"
            )
        )
        self.assertEqual(len(self.downloader.subtitles), 1)


class TestSearch(unittest.TestCase):
    def test_search_returns_results_and_encodes_query(self):
        scraper = FakeScraper()
        scraper.add(
            "GET",
            "https://z2.idlixku.com/api/search",
            make_response(payload={
                "results": [
                    {
                        "id": "1",
                        "contentType": "tv_series",
                        "title": "The Pitt",
                        "slug": "the-pitt-2025",
                    }
                ]
            }),
        )
        resolver = IdlixResolver(scraper=scraper)

        results = resolver.search("the pitt")

        self.assertEqual(results[0]["slug"], "the-pitt-2025")
        self.assertIn("q=the%20pitt", scraper.get_calls[0][0])


if __name__ == "__main__":
    unittest.main()
