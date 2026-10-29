#!/usr/bin/env python3
"""
Comprehensive unit tests for idlixdownloader2
Tests core functionality with mocked dependencies
"""

import os
import tempfile
import unittest
from unittest.mock import Mock, patch
from idlixdownloader2 import MajorPlayDownloader, tui
from idlixdownloader2.downloader import main as cli_main


class TestConfigPlaylist(unittest.TestCase):
    """Test config playlist parsing"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()
        self.downloader.config_url = 'https://e2e.majorplay.net/config.json?t=token123'

    @patch('builtins.input')
    def test_parse_config_highest_resolution(self, mock_input):
        """Test selecting highest resolution (default)"""
        mock_input.return_value = ''

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
video-1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
video-720p.m3u8
#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"
'''

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_config_playlist()
            self.assertTrue(result)
            self.assertIn('1080p', self.downloader.video_playlist_url)

    @patch('builtins.input')
    def test_parse_config_selected_resolution(self, mock_input):
        """Test selecting specific resolution"""
        mock_input.return_value = '2'

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
video-1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
video-720p.m3u8
'''

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_config_playlist()
            self.assertTrue(result)
            self.assertIn('720p', self.downloader.video_playlist_url)

    @patch('builtins.input')
    def test_parse_config_invalid_choice_retries(self, mock_input):
        """Test invalid resolution choice triggers retry"""
        mock_input.side_effect = ['99', '1']

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
video-1080p.m3u8
'''

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_config_playlist()
            self.assertTrue(result)
            self.assertEqual(mock_input.call_count, 2)

    @patch('builtins.input')
    def test_parse_config_non_numeric_retries(self, mock_input):
        """Test non-numeric input triggers retry"""
        mock_input.side_effect = ['abc', '1']

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
video-1080p.m3u8
'''

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_config_playlist()
            self.assertTrue(result)
            self.assertEqual(mock_input.call_count, 2)

    def test_parse_config_no_variants(self):
        """Test error when no video variants found"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '#EXTM3U\n'

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            with self.assertRaises(Exception) as context:
                self.downloader.get_config_playlist()
            self.assertIn('No video variants', str(context.exception))

    def test_parse_config_url_not_set(self):
        """Test when config URL is not set"""
        self.downloader.config_url = None
        result = self.downloader.get_config_playlist()
        self.assertFalse(result)

    def test_resume_matches_by_bandwidth_when_resolution_unknown(self):
        """Resume must pick by bandwidth; all-unknown RESOLUTION collides otherwise"""
        self.downloader.resume_mode = True
        self.downloader.saved_resolution = 'unknown'
        self.downloader.saved_bandwidth = 5800000

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=700000
video-low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3500000
video-mid.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5800000
video-high.m3u8
'''

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_config_playlist()
            self.assertTrue(result)
            self.assertIn('video-high', self.downloader.video_playlist_url)
            self.assertEqual(self.downloader.selected_bandwidth, 5800000)

    def test_resume_legacy_unknown_without_bandwidth_prompts(self):
        """Legacy state with only resolution=unknown cannot safely auto-match"""
        self.downloader.resume_mode = True
        self.downloader.saved_resolution = 'unknown'
        self.downloader.saved_bandwidth = None

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=700000
video-low.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5800000
video-high.m3u8
'''

        with patch('builtins.input', return_value='') as mock_input:
            with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
                result = self.downloader.get_config_playlist()
                self.assertTrue(result)
                mock_input.assert_called()
                self.assertIn('video-high', self.downloader.video_playlist_url)


    def test_batch_reuses_preferred_bandwidth(self):
        """Batch downloads should not re-prompt for quality"""
        self.downloader.batch_mode = True
        self.downloader.preferred_bandwidth = 2500000

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '''#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
video-1080p.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=2500000,RESOLUTION=1280x720
video-720p.m3u8
'''

        with patch.object(
            self.downloader.scraper, 'get', return_value=mock_response
        ), patch('builtins.input') as mock_input:
            result = self.downloader.get_config_playlist()

        self.assertTrue(result)
        self.assertIn('720p', self.downloader.video_playlist_url)
        mock_input.assert_not_called()


class TestVideoInfo(unittest.TestCase):
    """Test video info extraction"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()

    def test_extract_title(self):
        """Test title extraction from meta tag"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '<div data-postid="12345">Content</div><meta itemprop="name" content="Test Movie">'

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_video_info('https://z2.idlixku.com/movie/test')
            self.assertTrue(result)
            self.assertEqual(self.downloader.video_name, 'Test Movie')

    def test_fallback_to_url_slug(self):
        """Test fallback to URL slug when title not found"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = '<html><body>No meta tag</body></html>'

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            result = self.downloader.get_video_info('https://z2.idlixku.com/movie/toy-story-5')
            self.assertTrue(result)
            self.assertEqual(self.downloader.video_name, 'toy-story-5')

    def test_http_error(self):
        """Test HTTP error handling"""
        mock_response = Mock()
        mock_response.status_code = 404

        with patch.object(self.downloader.scraper, 'get', return_value=mock_response):
            with self.assertRaises(Exception) as context:
                self.downloader.get_video_info('https://z2.idlixku.com/movie/test')
            self.assertIn('Failed to fetch page', str(context.exception))


class TestHttpGetRetry(unittest.TestCase):
    """Test the retry wrapper used for playlist/init/segment requests"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()

    def test_retries_after_connection_reset(self):
        """A transient reset should be retried instead of aborting"""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.text = "ok"

        with patch.object(
            self.downloader.scraper,
            'get',
            side_effect=[ConnectionResetError(10054, 'reset'), mock_response],
        ), patch('idlixdownloader2.downloader.time.sleep'):
            response = self.downloader._http_get('https://cdn.example/playlist.json')

        self.assertEqual(response.status_code, 200)

    def test_retries_transient_server_error(self):
        """5xx responses should be retried"""
        error_response = Mock()
        error_response.status_code = 503
        ok_response = Mock()
        ok_response.status_code = 200

        with patch.object(
            self.downloader.scraper,
            'get',
            side_effect=[error_response, ok_response],
        ), patch('idlixdownloader2.downloader.time.sleep'):
            response = self.downloader._http_get('https://cdn.example/playlist.json')

        self.assertEqual(response.status_code, 200)

    def test_raises_after_exhausting_retries(self):
        """Persistent failures should surface after the retry budget"""
        with patch.object(
            self.downloader.scraper,
            'get',
            side_effect=ConnectionResetError(10054, 'reset'),
        ) as mock_get, patch('idlixdownloader2.downloader.time.sleep'):
            with self.assertRaises(ConnectionResetError):
                self.downloader._http_get('https://cdn.example/playlist.json', retries=3)

        self.assertEqual(mock_get.call_count, 3)


class TestSegmentUrls(unittest.TestCase):
    """Test JWT propagation to playlist, init, and segment URLs"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()

    def test_absolute_init_url_gets_jwt_token(self):
        """Absolute EXT-X-MAP URLs must carry the playlist token"""
        playlist_url = (
            "https://e2e.majorplay.net/v/z5/id/p/vid/data-1.json"
            "?t=TOKEN&pm=browser"
        )
        playlist_text = "\n".join([
            "#EXTM3U",
            '#EXT-X-MAP:URI="https://g5.cdn.example/v/z5/id/p/vid/cdn/cover.jpg"',
            "#EXTINF:6.0,",
            "https://g5.cdn.example/v/z5/id/p/vid/cdn/chunk-1.js",
        ])
        requested = []

        def fake_get(url, **kwargs):
            requested.append(url)
            response = Mock()
            response.status_code = 200
            response.content = b"data"
            response.text = playlist_text if "data-1.json" in url else ""
            return response

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(self.downloader, "_http_get", side_effect=fake_get):
                self.downloader.download_segments(playlist_url, f"{tmp}/video")

        init_requests = [u for u in requested if "cover.jpg" in u]
        self.assertEqual(len(init_requests), 1)
        self.assertIn("t=TOKEN", init_requests[0])


class TestMetadataSystem(unittest.TestCase):
    """Test metadata save/load functionality"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()
        self.downloader.video_name = "Test Movie"

    def test_save_metadata_creates_directory(self):
        """Test that save_metadata creates output directory if not exists"""
        import tempfile
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            # Override output path temporarily
            original_cwd = os.getcwd()
            os.chdir(tmpdir)

            try:
                self.downloader.save_download_metadata("Test Movie", "1920x1080", None)
                self.assertTrue(os.path.exists(os.path.join(tmpdir, "output/Test Movie")))
                self.assertTrue(os.path.exists(os.path.join(tmpdir, "output/Test Movie/download_state.json")))
            finally:
                os.chdir(original_cwd)

    def test_load_metadata_not_exists(self):
        """Test loading metadata when file doesn't exist"""
        result = self.downloader.load_download_metadata("NonexistentMovie")
        self.assertIsNone(result)

    def test_save_and_load_metadata(self):
        """Test saving and loading metadata roundtrip"""
        import tempfile
        import shutil

        with tempfile.TemporaryDirectory() as tmpdir:
            original_cwd = os.getcwd()
            os.chdir(tmpdir)

            try:
                subtitle_info = {
                    'url': 'https://example.com/subtitle.vtt',
                    'name': 'English',
                    'language': 'en'
                }

                self.downloader.save_download_metadata(
                    "Test Movie", "1920x1080", subtitle_info, bandwidth=5000000
                )
                loaded = self.downloader.load_download_metadata("Test Movie")

                self.assertIsNotNone(loaded)
                self.assertEqual(loaded['video_name'], "Test Movie")
                self.assertEqual(loaded['resolution'], "1920x1080")
                self.assertEqual(loaded['bandwidth'], 5000000)
                self.assertEqual(loaded['subtitle']['language'], 'en')
            finally:
                os.chdir(original_cwd)


class TestLanguageParsing(unittest.TestCase):
    """Test language extraction from subtitle URLs"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()

    def test_parse_language_indonesian(self):
        """Test parsing Indonesian language code from URL"""
        url = "https://e2e.majorplay.net/v/z5/abc/i18n/id/subtitle.vtt?t=token"
        result = self.downloader.get_language_from_subtitle_url(url)
        self.assertEqual(result, "Indonesian")

    def test_parse_language_english(self):
        """Test parsing English language code from URL"""
        url = "https://e2e.majorplay.net/v/z5/abc/i18n/en/subtitle.vtt?t=token"
        result = self.downloader.get_language_from_subtitle_url(url)
        self.assertEqual(result, "English")

    def test_parse_language_unknown_code(self):
        """Test parsing unknown language code returns uppercase code"""
        url = "https://e2e.majorplay.net/v/z5/abc/i18n/xx/subtitle.vtt?t=token"
        result = self.downloader.get_language_from_subtitle_url(url)
        self.assertEqual(result, "XX")

    def test_parse_language_no_pattern(self):
        """Test URL without language pattern returns None"""
        url = "https://example.com/subtitle.vtt"
        result = self.downloader.get_language_from_subtitle_url(url)
        self.assertIsNone(result)

    def test_format_playlist_name_and_language(self):
        label = self.downloader.format_subtitle_label({
            'name': 'Bahasa Indonesia',
            'language': 'id',
            'url': 'https://e2e.majorplay.net/subs.vtt',
        })
        self.assertEqual(label, 'Bahasa Indonesia (Indonesian)')

    def test_format_hash_with_i18n_url(self):
        label = self.downloader.format_subtitle_label({
            'name': 'd0690e6eec95a54d285545da878c41af',
            'language': None,
            'url': 'https://e2e.majorplay.net/v/x/i18n/en/subtitle.vtt?t=tok',
        })
        self.assertEqual(label, 'English Subtitle')

    def test_format_hash_without_language(self):
        label = self.downloader.format_subtitle_label({
            'name': 'd0690e6eec95a54d285545da878c41af',
            'language': None,
            'url': 'https://e2e.majorplay.net/subs-legacy/d0690e6eec95a54d285545da878c41af.vtt',
        })
        self.assertEqual(label, 'Subtitle')

    def test_format_hash_without_language_indexed(self):
        label = self.downloader.format_subtitle_label({
            'name': 'd0690e6eec95a54d285545da878c41af',
            'language': None,
            'url': 'https://e2e.majorplay.net/subs-legacy/hash.vtt',
        }, index=2)
        self.assertEqual(label, 'Subtitle 2')

    def test_format_none_subtitle(self):
        self.assertEqual(self.downloader.format_subtitle_label(None), 'none')

    def test_normalize_language_aliases(self):
        """Codes, names, and regional variants map to canonical tags"""
        for value in ('en', 'eng', 'English', 'en-US', 'English (US)'):
            self.assertEqual(
                self.downloader.normalize_language(value), ('eng', 'English')
            )
        for value in ('id', 'ind', 'in', 'Indonesian', 'Bahasa Indonesia'):
            self.assertEqual(
                self.downloader.normalize_language(value), ('ind', 'Indonesian')
            )
        self.assertEqual(self.downloader.normalize_language('xx'), (None, None))

    def test_resolve_language_prefers_i18n_url(self):
        """The canonical URL path wins over a conflicting API language"""
        code, name = self.downloader.resolve_subtitle_language({
            'name': 'English',
            'language': 'id',
            'url': 'https://e2e.majorplay.net/v/x/i18n/en/subtitle.vtt',
        })
        self.assertEqual((code, name), ('eng', 'English'))

    def test_resolve_language_falls_back_to_api(self):
        """API metadata is used when the URL has no /i18n/ path"""
        code, name = self.downloader.resolve_subtitle_language({
            'name': 'Bahasa Indonesia',
            'language': 'id',
            'url': 'https://e2e.majorplay.net/subs.vtt',
        })
        self.assertEqual((code, name), ('ind', 'Indonesian'))

    def test_build_mux_command_uses_detected_language(self):
        """The MKV subtitle track must not be hardcoded to Indonesian"""
        cmd = self.downloader.build_mux_command(
            'video.mp4',
            'output.mkv',
            audio_complete='audio.mp4',
            subtitle_file='subtitle.vtt',
            subtitle_language='eng',
            subtitle_language_name='English',
        )
        self.assertIn('language=eng', cmd)
        self.assertIn('title=English', cmd)
        self.assertNotIn('language=ind', cmd)

    def test_build_mux_command_unknown_language(self):
        """Unknown languages are tagged und instead of guessed"""
        cmd = self.downloader.build_mux_command(
            'video.mp4',
            'output.mkv',
            subtitle_file='subtitle.vtt',
        )
        self.assertIn('language=und', cmd)


class TestSeriesSelection(unittest.TestCase):
    """Test interactive season/episode selection for parent series URLs"""

    def setUp(self):
        self.downloader = MajorPlayDownloader()
        self.url = "https://z2.idlixku.com/series/the-pitt-2025"
        self.seasons = [
            {'seasonNumber': 1, 'episodeCount': 15, 'isPublished': True},
            {'seasonNumber': 2, 'episodeCount': 15, 'isPublished': True},
        ]
        self.episodes = [
            {'episodeNumber': 1, 'name': '7:00 A.M.', 'hasVideo': True, 'runtime': 53},
            {'episodeNumber': 2, 'name': '8:00 A.M.', 'hasVideo': True, 'runtime': 51},
        ]

    def test_builds_concrete_episode_url(self):
        with patch.object(
            self.downloader.resolver, 'list_seasons',
            return_value=('The Pitt', self.seasons),
        ), patch.object(
            self.downloader.resolver, 'list_episodes',
            return_value=self.episodes,
        ), patch('idlixdownloader2.tui.checkbox', side_effect=[[1], [0]]):
            targets = self.downloader.prepare_series_targets(self.url)

        self.assertEqual(
            targets,
            [(f"{self.url}/season/2/episode/1", 2, 1)],
        )

    def test_multi_select_episodes_and_seasons(self):
        with patch.object(
            self.downloader.resolver, 'list_seasons',
            return_value=('The Pitt', self.seasons),
        ), patch.object(
            self.downloader.resolver, 'list_episodes',
            return_value=self.episodes,
        ), patch(
            'idlixdownloader2.tui.checkbox',
            side_effect=[[0, 1], [0, 1], [0]],
        ):
            targets = self.downloader.prepare_series_targets(self.url)

        self.assertEqual(
            targets,
            [
                (f"{self.url}/season/1/episode/1", 1, 1),
                (f"{self.url}/season/1/episode/2", 1, 2),
                (f"{self.url}/season/2/episode/1", 2, 1),
            ],
        )

    def test_back_from_episode_menu_returns_to_seasons(self):
        with patch.object(
            self.downloader.resolver, 'list_seasons',
            return_value=('The Pitt', self.seasons),
        ), patch.object(
            self.downloader.resolver, 'list_episodes',
            return_value=self.episodes,
        ), patch(
            'idlixdownloader2.tui.checkbox',
            side_effect=[[0], None, [0], [0]],
        ) as menu:
            targets = self.downloader.prepare_series_targets(self.url)

        self.assertEqual(menu.call_count, 4)
        self.assertEqual(
            targets,
            [(f"{self.url}/season/1/episode/1", 1, 1)],
        )

    def test_specific_episode_url_is_unchanged(self):
        url = f"{self.url}/season/1/episode/2"
        self.assertEqual(
            self.downloader.prepare_series_targets(url),
            [(url, 1, 2)],
        )

    def test_explicit_season_episode_overrides_url(self):
        url = f"{self.url}/season/1/episode/1"
        targets = self.downloader.prepare_series_targets(
            url, season=2, episode=3
        )
        self.assertEqual(
            targets,
            [(f"{self.url}/season/2/episode/3", 2, 3)],
        )

    def test_movie_url_is_unchanged(self):
        url = "https://z2.idlixku.com/movie/toy-story-5-2026"
        self.assertEqual(
            self.downloader.prepare_series_targets(url),
            [(url, None, None)],
        )

    def test_cancel_returns_none(self):
        with patch.object(
            self.downloader.resolver, 'list_seasons',
            return_value=('The Pitt', self.seasons),
        ), patch('idlixdownloader2.tui.checkbox', return_value=None):
            targets = self.downloader.prepare_series_targets(self.url)

        self.assertIsNone(targets)


class TestTuiFallback(unittest.TestCase):
    """TUI must still work when stdin is piped (tests, scripts)"""

    def test_numbered_select(self):
        with patch('builtins.input', return_value='2'):
            self.assertEqual(tui.select('Title', ['a', 'b'], 'sub'), 1)

    def test_numbered_select_default(self):
        with patch('builtins.input', return_value=''):
            self.assertEqual(tui.select('Title', ['a', 'b'], default=1), 1)

    def test_text_prompt_default(self):
        with patch('builtins.input', return_value=''):
            self.assertEqual(tui.prompt_text('Title', default='x'), 'x')

    def test_confirm_accepts_yn_when_piped(self):
        with patch('builtins.input', return_value='n'):
            self.assertFalse(tui.confirm('Include?', default=True))
        with patch('builtins.input', return_value=''):
            self.assertTrue(tui.confirm('Include?', default=True))

    def test_cli_opens_tui_when_url_omitted(self):
        with patch('idlixdownloader2.tui.is_interactive', return_value=True), \
                patch(
                    'idlixdownloader2.tui.run_app', return_value=0
                ) as run_app:
            self.assertEqual(cli_main([]), 0)

        run_app.assert_called_once()
        self.assertIsNone(run_app.call_args[0][0])

    def test_cli_passes_url_to_tui(self):
        url = 'https://z2.idlixku.com/movie/x'
        with patch('idlixdownloader2.tui.is_interactive', return_value=True), \
                patch(
                    'idlixdownloader2.tui.run_app', return_value=0
                ) as run_app:
            self.assertEqual(cli_main([url]), 0)

        self.assertEqual(run_app.call_args[0][0], url)


class TestTuiPageUrl(unittest.TestCase):
    def test_series_result(self):
        self.assertEqual(
            tui.DownloaderApp._page_url(
                {"slug": "the-pitt-2025", "contentType": "tv_series"}
            ),
            "https://z2.idlixku.com/series/the-pitt-2025",
        )

    def test_movie_result(self):
        self.assertEqual(
            tui.DownloaderApp._page_url(
                {"slug": "toy-story-5-2026", "contentType": "movie"}
            ),
            "https://z2.idlixku.com/movie/toy-story-5-2026",
        )

    def test_missing_slug(self):
        self.assertIsNone(tui.DownloaderApp._page_url({"contentType": "movie"}))


if __name__ == '__main__':
    unittest.main()
