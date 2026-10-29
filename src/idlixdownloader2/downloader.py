#!/usr/bin/env python3
"""
MajorPlay Video Downloader
Downloads videos from IDLIX
"""

import os
import sys
import re
import time
import json
import argparse
import subprocess
import cloudscraper
from urllib.parse import quote, urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from . import tui
from .resolver import IdlixResolver, TurnstileRequired

# IDLIX currently ships English and Indonesian subtitle tracks only.
# The URL path (/i18n/<code>/) is the most reliable source; API labels come next.
LANGUAGE_MAP = {
    'en': ('eng', 'English'),
    'eng': ('eng', 'English'),
    'english': ('eng', 'English'),
    'id': ('ind', 'Indonesian'),
    'ind': ('ind', 'Indonesian'),
    'in': ('ind', 'Indonesian'),
    'indonesian': ('ind', 'Indonesian'),
    'bahasa indonesia': ('ind', 'Indonesian'),
}

class MajorPlayDownloader:
    def __init__(self):
        self.scraper = cloudscraper.create_scraper(
            browser={'browser': 'chrome', 'platform': 'windows', 'desktop': True}
        )
        self.resolver = IdlixResolver(self.scraper)
        self.base_url = "https://z2.idlixku.com/"
        self.video_name = None
        self.config_url = None
        self.config_expires_at = None
        self.subtitles = []  # List of subtitle URLs
        self.batch_mode = False
        self.preferred_bandwidth = None
        self.preferred_subtitle_language = None
        self.ui = tui.ConsoleUI()

    def _log(self, message):
        self.ui.log(message)

    @staticmethod
    def sanitize_filename(name):
        """Remove characters that are unsafe in a filename."""
        for char in ['/', '\\', ':', '*', '?', '"', '<', '>', '|']:
            name = name.replace(char, '-')
        return name.strip()

    def _http_get(self, url, timeout=30, retries=3, headers=None):
        """GET with retries for transient CDN resets and 5xx responses."""
        request_headers = {
            'Referer': 'https://z2.idlixku.com/',
            'Origin': 'https://z2.idlixku.com',
            'Accept': '*/*',
        }
        if headers:
            request_headers.update(headers)

        last_error = None
        for attempt in range(retries):
            try:
                response = self.scraper.get(
                    url, timeout=timeout, headers=request_headers
                )
                if (
                    response.status_code in (429, 500, 502, 503, 504)
                    and attempt < retries - 1
                ):
                    time.sleep(1 * (attempt + 1))
                    continue
                return response
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(1 * (attempt + 1))
                    continue
                raise
        raise last_error

    def add_subtitle(self, url, name=None, language=None):
        """Add a subtitle, deduplicating by URL without the JWT query."""
        if not url:
            return False
        base = url.split('?', 1)[0]
        if any(s.get('url', '').split('?', 1)[0] == base for s in self.subtitles):
            return False
        if not name:
            filename = base.rsplit('/', 1)[-1]
            name = filename.rsplit('.', 1)[0] if '.' in filename else filename
        self.subtitles.append({'url': url, 'name': name, 'language': language})
        return True

    def get_video_info(self, url):
        """Extract video metadata from page"""
        self._log(f"[1/6] Fetching video page...")

        response = self._http_get(url)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch page: {response.status_code}")

        # Extract title
        match = re.search(r'<meta itemprop="name" content="([^"]+)"', response.text)
        if match:
            self.video_name = self.sanitize_filename(match.group(1))
        else:
            # Fallback: use URL slug
            self.video_name = url.rstrip('/').split('/')[-1].split('?', 1)[0]

        self._log(f"   Video: {self.video_name}")
        return True

    def get_config_playlist(self):
        """Get config (master playlist)"""
        self._log(f"[3/6] Fetching MajorPlay config...")

        # For now, we need to provide the config URL
        # In a full implementation, this would be extracted from player JavaScript
        if not self.config_url:
            self._log("\n   Config URL required. Example:")
            self._log("   https://e2e.majorplay.net/v/z5/VIDEO_ID/config-XXXXX.json?t=JWT_TOKEN")
            return False

        response = self._http_get(self.config_url)
        if response.status_code != 200:
            raise Exception(f"Failed to get config: {response.status_code}")

        # Extract JWT token early for subtitle/playlist URLs
        jwt_token = ''
        if '?' in self.config_url:
            jwt_token = '?' + self.config_url.split('?', 1)[1]

        # Parse M3U8 master playlist
        master_playlist = response.text

        # Extract variant playlists
        variants = []
        for line in master_playlist.split('\n'):
            if line.startswith('#EXT-X-STREAM-INF'):
                # Extract resolution and bandwidth
                res_match = re.search(r'RESOLUTION=(\d+x\d+)', line)
                bw_match = re.search(r'BANDWIDTH=(\d+)', line)
                resolution = res_match.group(1) if res_match else 'unknown'
                bandwidth = int(bw_match.group(1)) if bw_match else 0
                variants.append({
                    'resolution': resolution,
                    'bandwidth': bandwidth,
                })
            elif line and not line.startswith('#') and variants:
                # This is the playlist URL for the last variant
                variants[-1]['url'] = line

        # Extract audio
        audio_url = None
        for line in master_playlist.split('\n'):
            if 'TYPE=AUDIO' in line:
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    audio_url = match.group(1)

        # Extract subtitles from master playlist with language info
        playlist_subs_found = 0
        for line in master_playlist.split('\n'):
            if 'TYPE=SUBTITLES' in line:
                # Parse subtitle metadata: NAME, LANGUAGE, URI
                name_match = re.search(r'NAME="([^"]+)"', line)
                lang_match = re.search(r'LANGUAGE="([^"]+)"', line)
                uri_match = re.search(r'URI="([^"]+)"', line)

                if uri_match:
                    # Build subtitle URL with JWT token
                    sub_url = uri_match.group(1)
                    if not sub_url.startswith('http'):
                        base = self.config_url.rsplit('?', 1)[0].rsplit('/', 1)[0]
                        sub_url = urljoin(base + '/', sub_url) + jwt_token

                    # Deduplicate by base URL so API and playlist subtitles
                    # do not show up twice with different tokens.
                    if self.add_subtitle(
                        sub_url,
                        name_match.group(1) if name_match else None,
                        lang_match.group(1) if lang_match else None,
                    ):
                        playlist_subs_found += 1

        if playlist_subs_found > 0:
            self._log(f"   [OK] {playlist_subs_found} subtitle(s) in master playlist")

        if not variants:
            raise Exception("No video variants found in config")

        def variant_label(v):
            # RESOLUTION often missing on MajorPlay; bandwidth is the stable id
            if v['resolution'] != 'unknown':
                return f"{v['resolution']} ({v['bandwidth']//1000} kbps)"
            return f"{v['bandwidth']//1000} kbps"

        # Resume: match by bandwidth (unique). Resolution alone collides as "unknown".
        selected = None
        if getattr(self, 'resume_mode', False):
            saved_bw = getattr(self, 'saved_bandwidth', None)
            if saved_bw is not None:
                for v in variants:
                    if v['bandwidth'] == saved_bw:
                        selected = v
                        self._log(f"\n   Using saved quality: {variant_label(selected)}")
                        break
            # Legacy state without bandwidth: only safe if resolution is unique & real
            if selected is None:
                saved_res = getattr(self, 'saved_resolution', None)
                if saved_res and saved_res != 'unknown':
                    matches = [v for v in variants if v['resolution'] == saved_res]
                    if len(matches) == 1:
                        selected = matches[0]
                        self._log(f"\n   Using saved quality: {variant_label(selected)}")
            if selected is None:
                self._log(f"\n   [!] Saved quality not available, prompting for selection")

        # Batch downloads reuse the first episode's quality when possible.
        if (
            selected is None
            and getattr(self, 'preferred_bandwidth', None) is not None
        ):
            for v in variants:
                if v['bandwidth'] == self.preferred_bandwidth:
                    selected = v
                    self._log(f"\n   Using previous quality: {variant_label(selected)}")
                    break
            if selected is None:
                selected = variants[
                    variants.index(max(variants, key=lambda x: x['bandwidth']))
                ]
                self._log(
                    f"\n   Previous quality unavailable, "
                    f"using {variant_label(selected)}"
                )

        # Prompt user to select resolution if not resuming
        if selected is None:
            highest_idx = variants.index(max(variants, key=lambda x: x['bandwidth']))
            index = self.ui.select_quality(
                [variant_label(v) for v in variants],
                default=highest_idx,
            )
            if index is None:
                self._log("   Dibatalkan.")
                return False
            selected = variants[index]
            self._log(f"   Selected: {variant_label(selected)}")

        # Store for metadata / resume (bandwidth is the match key)
        self.selected_resolution = selected['resolution']
        self.selected_bandwidth = selected['bandwidth']
        if self.batch_mode:
            self.preferred_bandwidth = self.selected_bandwidth

        # Construct base without query
        base = self.config_url.rsplit('?', 1)[0].rsplit('/', 1)[0]

        # Build URLs with JWT token (already extracted earlier)
        self.video_playlist_url = urljoin(base + '/', selected['url']) + jwt_token
        self.audio_playlist_url = urljoin(base + '/', audio_url) + jwt_token if audio_url else None

        return True

    def download_segments(self, playlist_url, output_prefix, label='Video'):
        """Download all segments from fMP4 playlist with init segment (parallel)"""
        # Fetch playlist
        response = self._http_get(playlist_url)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch playlist: {response.status_code}")

        # Extract JWT token from playlist URL to pass to segments
        jwt_token = ''
        if '?' in playlist_url:
            jwt_token = '?' + playlist_url.split('?', 1)[1]

        # Parse init segment (EXT-X-MAP) and segment URLs
        init_url = None
        segments = []
        for line in response.text.split('\n'):
            line = line.strip()
            if line.startswith('#EXT-X-MAP:'):
                # Extract init segment URL
                match = re.search(r'URI="([^"]+)"', line)
                if match:
                    init_url = match.group(1)
                    # Resolve relative URL and preserve JWT token
                    if not init_url.startswith('http'):
                        base = playlist_url.rsplit('/', 1)[0].rsplit('?', 1)[0]
                        init_url = urljoin(base + '/', init_url) + jwt_token
                    elif '?' not in init_url:
                        # Absolute CDN URL: the init segment is authenticated too
                        init_url = init_url + jwt_token
            elif line and not line.startswith('#'):
                # Resolve segment URL and append JWT token
                seg_url = line
                if not seg_url.startswith('http'):
                    # Relative URL - resolve against base and append JWT token
                    base = playlist_url.rsplit('/', 1)[0].rsplit('?', 1)[0]
                    seg_url = urljoin(base + '/', seg_url) + jwt_token
                else:
                    # Absolute URL - append JWT token if not already present
                    if '?' not in seg_url:
                        seg_url = seg_url + jwt_token
                segments.append(seg_url)

        if not segments:
            raise Exception("No segments found in playlist")

        self._log(f"   Found {len(segments)} segments")

        # Download init segment (required for fMP4)
        init_data = None
        if init_url:
            self._log(f"   Downloading init segment...")
            init_response = self._http_get(init_url)
            if init_response.status_code == 200:
                init_data = init_response.content
            else:
                raise Exception(
                    f"Failed to download init segment: HTTP {init_response.status_code}"
                )

        # Parallel download function with retry
        def download_segment(i, seg_url, retries=3):
            seg_file = f"{output_prefix}_seg{i:04d}.m4s"

            # Continue download: skip if segment already exists with content
            if os.path.exists(seg_file) and os.path.getsize(seg_file) > 0:
                return seg_file

            for attempt in range(retries):
                try:
                    seg_response = self._http_get(seg_url, timeout=30, retries=1)
                    if seg_response.status_code != 200:
                        # Debug logging for 401 errors
                        if seg_response.status_code == 401:
                            self._log(f"\n   [DEBUG] Segment {i} got 401 Unauthorized")
                            self._log(f"   [DEBUG] URL: {seg_url[:100]}{'...' if len(seg_url) > 100 else ''}")
                            self._log(f"   [DEBUG] Has query params: {'?' in seg_url}")
                        if attempt == retries - 1:
                            raise Exception(f"Failed segment {i}: {seg_response.status_code}")
                        time.sleep(1 * (attempt + 1))
                        continue

                    # Download fragment as-is (don't prepend init here)
                    with open(seg_file, 'wb') as f:
                        f.write(seg_response.content)
                    return seg_file

                except Exception as e:
                    if attempt == retries - 1:
                        raise Exception(f"Segment {i} failed after {retries} retries: {e}")
                    time.sleep(2 * (attempt + 1))

            raise Exception(f"Segment {i} failed")

        # Download segments in parallel (reduced workers to avoid rate-limit)
        segment_files = [None] * len(segments)
        completed = 0
        progress_lock = Lock()

        progress = self.ui.start_progress(label, len(segments))

        # Manually manage executor for proper Ctrl+C handling
        executor = ThreadPoolExecutor(max_workers=5)
        try:
            futures = {executor.submit(download_segment, i, url): i
                      for i, url in enumerate(segments, 1)}

            for future in as_completed(futures):
                i = futures[future]
                try:
                    segment_files[i-1] = future.result()
                    with progress_lock:
                        completed += 1
                        progress.advance()
                except Exception as e:
                    self._log(f"\n   [X] Error downloading segment {i}: {e}")
                    raise
        except KeyboardInterrupt:
            self._log(f"\n\n   [!] Download cancelled by user (Ctrl+C)")
            self._log(
                f"   Progress: {completed}/{len(segments)} segments completed"
            )
            self._log(
                "   Continue download will skip completed segments on next run"
            )
            # Don't wait for workers - cancel immediately
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            # Normal shutdown for non-interrupted case
            if not executor._shutdown:
                executor.shutdown(wait=True)
            progress.close()

        self._log(f"   [OK] {completed}/{len(segments)} {label.lower()} segments")
        return {'segments': segment_files, 'init_data': init_data}

    @staticmethod
    def build_mux_command(video_complete, output_file, audio_complete=None,
                          subtitle_file=None, subtitle_language=None,
                          subtitle_language_name=None):
        """Build the FFmpeg command, tagging subtitles with their real language."""
        cmd = ['ffmpeg', '-i', video_complete]

        if audio_complete:
            cmd.extend(['-i', audio_complete])

        if subtitle_file:
            cmd.extend(['-i', subtitle_file])
            cmd.extend(['-c:v', 'copy', '-c:a', 'copy', '-c:s', 'srt'])
            cmd.extend([
                '-metadata:s:s:0',
                f'language={subtitle_language or "und"}',
            ])
            if subtitle_language_name:
                cmd.extend([
                    '-metadata:s:s:0',
                    f'title={subtitle_language_name}',
                ])
            output_file = output_file.replace('.mp4', '.mkv')
        else:
            cmd.extend(['-c', 'copy'])

        cmd.append(output_file)
        return cmd

    def download_with_ffmpeg(self, output_file, subtitle_url=None, segments_dir=None,
                             subtitle_language=None, subtitle_language_name=None):
        """Download segments then binary concat fMP4 fragments"""
        self._log(f"[5/6] Downloading video...")

        if not self.video_playlist_url:
            raise Exception("No video playlist URL")

        # Use segments_dir for intermediate files if provided, otherwise use output_file directory
        if segments_dir:
            base_name = os.path.splitext(os.path.basename(output_file))[0]
            segments_prefix = f"{segments_dir}/{base_name}"
        else:
            segments_prefix = output_file

        try:
            # Download video segments
            self._log(f"   Downloading video segments...")
            video_data = self.download_segments(
                self.video_playlist_url, f"{segments_prefix}_video", label="Video"
            )
            video_segments = video_data['segments']
            video_init = video_data['init_data']

            # Download audio segments if separate
            audio_segments = None
            audio_init = None
            if self.audio_playlist_url:
                self._log(f"   Downloading audio segments...")
                audio_data = self.download_segments(
                    self.audio_playlist_url, f"{segments_prefix}_audio", label="Audio"
                )
                audio_segments = audio_data['segments']
                audio_init = audio_data['init_data']

            # Download subtitle (keep in movie directory, not segments)
            subtitle_file = None
            if subtitle_url:
                self._log(f"   Downloading subtitle...")
                # Strip extension from output_file to avoid .mkv.vtt
                subtitle_file = f"{os.path.splitext(output_file)[0]}.vtt"
                sub_response = self._http_get(subtitle_url)
                if sub_response.status_code == 200:
                    with open(subtitle_file, 'wb') as f:
                        f.write(sub_response.content)

            # Binary concat: init + frag1 + frag2 + ... = complete.mp4
            self._log(f"   Concatenating fMP4 fragments...")
            video_complete = f"{segments_prefix}_video_complete.mp4"
            with open(video_complete, 'wb') as out:
                if video_init:
                    out.write(video_init)
                for seg_file in video_segments:
                    with open(seg_file, 'rb') as seg:
                        out.write(seg.read())

            # Binary concat audio if separate
            audio_complete = None
            if audio_segments:
                audio_complete = f"{segments_prefix}_audio_complete.mp4"
                with open(audio_complete, 'wb') as out:
                    if audio_init:
                        out.write(audio_init)
                    for seg_file in audio_segments:
                        with open(seg_file, 'rb') as seg:
                            out.write(seg.read())

            # Mux video + audio + subtitle with FFmpeg
            self._log(f"   Muxing with FFmpeg...")
            cmd = self.build_mux_command(
                video_complete,
                output_file,
                audio_complete=audio_complete,
                subtitle_file=subtitle_file,
                subtitle_language=subtitle_language,
                subtitle_language_name=subtitle_language_name,
            )

            # Run FFmpeg
            with self.ui.status("Muxing with FFmpeg..."):
                result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                self._log(f"\n   FFmpeg stderr:")
                self._log(result.stderr)
                raise Exception(f"FFmpeg failed with code {result.returncode}")
            self._log(f"\n   [OK] Download complete: {output_file}")

            # Cleanup
            self._log(f"   Cleaning up temporary files...")
            for seg in video_segments:
                os.remove(seg)
            os.remove(video_complete)
            if audio_segments:
                for seg in audio_segments:
                    os.remove(seg)
                os.remove(audio_complete)
            if subtitle_file:
                os.remove(subtitle_file)
            self._log(f"   [OK] Cleanup complete")

            return True

        except Exception as e:
            self._log(f"\n   [X] Error: {e}")
            return False

    def load_download_metadata(self, video_name):
        """Load metadata from previous download if exists"""
        movie_dir = f"output/{video_name}"
        metadata_file = f"{movie_dir}/download_state.json"

        if os.path.exists(metadata_file):
            try:
                with open(metadata_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return None
        return None

    def save_download_metadata(self, video_name, resolution, subtitle_info, bandwidth=None):
        """Save download metadata for resume capability"""
        movie_dir = f"output/{video_name}"
        metadata_file = f"{movie_dir}/download_state.json"

        # Create directory if it doesn't exist
        os.makedirs(movie_dir, exist_ok=True)

        metadata = {
            'video_name': video_name,
            'resolution': resolution,
            'bandwidth': bandwidth,
            'subtitle': subtitle_info,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        }

        try:
            with open(metadata_file, 'w', encoding='utf-8') as f:
                json.dump(metadata, f, indent=2, ensure_ascii=False)
        except Exception as e:
            self._log(f"   [DEBUG] Failed to save metadata: {e}")

    @staticmethod
    def normalize_language(value):
        """Return (ISO 639-2 code, display name) for a language code or name."""
        if not value:
            return None, None
        key = str(value).strip().lower()
        # Accept "en-US", "English (US)", "Indonesian - Indonesia"
        key = re.split(r'[-(\[]', key, maxsplit=1)[0].strip()
        if key in LANGUAGE_MAP:
            return LANGUAGE_MAP[key]
        return None, None

    def resolve_subtitle_language(self, sub_info):
        """Resolve a subtitle's language from the most reliable signal available.

        Priority: canonical ``/i18n/<code>/`` URL path, then the API
        ``language`` code, then the display ``name``.  Returns
        ``(iso639_2_code, display_name)`` or ``(None, None)``.
        """
        if not sub_info:
            return None, None

        match = re.search(r'/i18n/([^/]+)/', sub_info.get('url', ''))
        if match:
            code, name = self.normalize_language(match.group(1))
            if code:
                return code, name

        for field in ('language', 'name'):
            code, name = self.normalize_language(sub_info.get(field))
            if code:
                return code, name

        return None, None

    def get_language_from_subtitle_url(self, url):
        """Extract a display language name from a /i18n/<code>/ URL path."""
        if not url:
            return None
        match = re.search(r'/i18n/([^/]+)/', url)
        if match:
            code, name = self.normalize_language(match.group(1))
            return name if code else match.group(1).upper()
        return None

    def format_subtitle_label(self, sub_info, index=None):
        """Human-readable subtitle label. Handles playlist NAME/LANGUAGE and hash-only capture URLs."""
        if not sub_info:
            return 'none'

        name = (sub_info.get('name') or '').strip()
        _, language_display = self.resolve_subtitle_language(sub_info)

        # Hash-like capture filenames are not useful to show
        is_hash = (
            name
            and name != 'Unknown'
            and len(name) >= 16
            and name.replace('-', '').replace('_', '').isalnum()
        )
        if is_hash or not name or name == 'Unknown':
            if language_display:
                return f"{language_display} Subtitle"
            return f"Subtitle {index}" if index is not None else "Subtitle"

        if language_display and language_display.lower() not in name.lower():
            return f"{name} ({language_display})"
        return name

    def resolve_via_api(self, url, season=None, episode=None):
        """Resolve the stream through IDLIX's own API (no browser)."""
        self._log(f"[2/6] Resolving playback via IDLIX API (no browser)...")

        try:
            resolved = self.resolver.resolve(url, season=season, episode=episode)
        except TurnstileRequired:
            self._log(
                "   [!] Playback requires Cloudflare Turnstile verification. "
                "Browser fallback is disabled, so this title cannot be downloaded."
            )
            return False
        except Exception as e:
            self._log(f"   [!] API resolve failed: {e}")
            return False

        self.config_url = resolved.config_url
        self.config_expires_at = resolved.expires_at
        if resolved.title:
            self.video_name = self.sanitize_filename(resolved.title)

        for sub in resolved.subtitles:
            self.add_subtitle(sub.get('url'), sub.get('name'), sub.get('language'))

        self._log(f"   [OK] Config URL extracted via API")
        if resolved.subtitles:
            self._log(f"   [OK] {len(resolved.subtitles)} subtitle(s) from API")
        return True

    @staticmethod
    def _series_labels(seasons):
        return [
            f"Season {s['seasonNumber']}  ·  {s.get('episodeCount', '?')} episode"
            for s in seasons
        ]

    @staticmethod
    def _episode_labels(episodes):
        labels = []
        for e in episodes:
            name = e.get('name') or f"Episode {e['episodeNumber']}"
            runtime = e.get('runtime')
            suffix = f"  ·  {runtime} min" if runtime else ""
            labels.append(f"E{e['episodeNumber']:02d}  {name}{suffix}")
        return labels

    def select_series_episodes(self, url, season=None, episode=None):
        """Pick one or more ``(season, episode)`` pairs.

        Space toggles, ``a`` selects all, Enter confirms, and Esc/left goes
        back: from the episode menu to the season menu, or from the season
        menu to the URL prompt.  Returns ``None`` when the user goes back.
        """
        try:
            title, seasons = self.resolver.list_seasons(url)
        except Exception as e:
            self._log(f"   [!] Could not list seasons: {e}")
            return None

        seasons = [
            s for s in seasons
            if s.get('seasonNumber') is not None and s.get('isPublished') is not False
        ]
        seasons.sort(key=lambda s: s.get('seasonNumber') or 0)
        if not seasons:
            self._log("   [!] No seasons found")
            return None

        season_numbers = [s['seasonNumber'] for s in seasons]
        if season is not None and season not in season_numbers:
            self._log(f"   [!] Season {season} not available")
            season = None
        if season is not None and episode is not None:
            return [(season, episode)]

        while True:
            if season is None:
                selected = self.ui.select_seasons(
                    title,
                    self._series_labels(seasons),
                )
                if selected is None:
                    return None
                selected_seasons = [seasons[i]['seasonNumber'] for i in selected]
            else:
                selected_seasons = [season]

            picks = []
            back = False
            for current_season in selected_seasons:
                try:
                    episodes = self.resolver.list_episodes(url, current_season)
                except Exception as e:
                    self._log(f"   [!] Could not list episodes: {e}")
                    return None

                episodes = [
                    e for e in episodes
                    if e.get('episodeNumber') is not None
                    and e.get('hasVideo') is not False
                ]
                episodes.sort(key=lambda e: e.get('episodeNumber') or 0)
                if not episodes:
                    self._log(f"   [!] No episodes found for season {current_season}")
                    continue

                if episode is not None:
                    numbers = [e['episodeNumber'] for e in episodes]
                    if episode in numbers:
                        picks.append((current_season, episode))
                        continue
                    self._log(
                        f"   [!] Episode {episode} not available in "
                        f"season {current_season}"
                    )
                    episode = None

                selected_eps = self.ui.select_episodes(
                    title,
                    self._episode_labels(episodes),
                    current_season,
                )
                if selected_eps is None:
                    back = True
                    break
                picks.extend(
                    (current_season, episodes[i]['episodeNumber'])
                    for i in selected_eps
                )

            if not back:
                return picks

            # Esc/left in the episode menu: go back to the season menu.
            season = None
            episode = None

    def prepare_series_targets(self, url, season=None, episode=None):
        """Resolve a page URL into one or more concrete episode targets.

        Returns a list of ``(target_url, season, episode)`` or ``None`` when
        the user goes back from the top-level season menu.
        """
        parsed = self.resolver.parse_page_url(url)

        if parsed['kind'] != 'series':
            return [(url, season, episode)]

        parsed_season = parsed['season']
        parsed_episode = parsed['episode']
        if season is None:
            season = parsed_season
        if episode is None:
            episode = parsed_episode

        if season is not None and episode is not None:
            picks = [(season, episode)]
        else:
            picks = self.select_series_episodes(url, season, episode)
            if picks is None:
                return None

        result = []
        for picked_season, picked_episode in picks:
            if picked_season == parsed_season and picked_episode == parsed_episode:
                target = url
            else:
                parsed_url = urlparse(url)
                base = f"{parsed_url.scheme}://{parsed_url.netloc}"
                target = (
                    f"{base}/series/{quote(parsed['slug'])}"
                    f"/season/{picked_season}/episode/{picked_episode}"
                )
            result.append((target, picked_season, picked_episode))
        return result

    def download(self, url, config_url=None, auto_extract=True,
                 season=None, episode=None):
        """Resolve the input into one or more episodes and download them."""
        try:
            targets = None
            if auto_extract and not config_url:
                targets = self.prepare_series_targets(url, season, episode)
                if targets is None:
                    return None  # back to the URL prompt

            if not targets:
                targets = [(url, season, episode)]

            self.batch_mode = len(targets) > 1
            for index, (target_url, target_season, target_episode) in enumerate(
                targets, 1
            ):
                if self.batch_mode:
                    self._log(
                        f"\n=== Episode {index}/{len(targets)} "
                        f"(S{target_season:02d}E{target_episode:02d}) ==="
                    )
                if not self._download_one(
                    target_url,
                    config_url=config_url if not self.batch_mode else None,
                    auto_extract=auto_extract,
                    season=target_season,
                    episode=target_episode,
                ):
                    return False
            return True

        except Exception as e:
            self._log(f"\n[X] Error: {e}")
            import traceback
            self._log(traceback.format_exc())
            return False

    def _download_one(self, target_url, config_url=None, auto_extract=True,
                      season=None, episode=None):
        """Download a single movie or episode."""
        self.config_url = config_url
        self.subtitles = []
        self.resume_mode = False

        try:
            # Step 1: Get video info (best effort; the API resolver can also
            # provide the title if the page itself is blocked).
            try:
                if not self.get_video_info(target_url):
                    return False
            except Exception as e:
                self._log(f"   [!] Could not read page metadata: {e}")
                if not auto_extract and not self.config_url:
                    return False
                self.video_name = self.sanitize_filename(
                    target_url.rstrip('/').split('/')[-1].split('?', 1)[0]
                )

            # Step 2: Resolve playback through the HTTP API.
            if not self.config_url and auto_extract:
                self.resolve_via_api(target_url, season=season, episode=episode)
                if not self.config_url:
                    self._log(f"\n   [X] HTTP resolver failed")
                    return False
            elif not self.config_url:
                self._log(f"[2/6] Config URL required (or use auto_extract mode)")
                return False
            # Manual mode: config_url already set — skip resolution

            # Check for existing download to offer resume
            self.resume_mode = False
            metadata = self.load_download_metadata(self.video_name)
            if metadata:
                movie_dir = f"output/{self.video_name}"
                segments_dir = f"{movie_dir}/segments"
                if os.path.exists(segments_dir):
                    segment_count = len([f for f in os.listdir(segments_dir) if f.endswith('.m4s')])
                    self._log(f"\n[!] Previous download found:")
                    res = metadata.get('resolution', 'unknown')
                    bw = metadata.get('bandwidth')
                    if res and res != 'unknown' and bw:
                        quality_display = f"{res} ({bw // 1000} kbps)"
                    elif bw:
                        quality_display = f"{bw // 1000} kbps"
                    else:
                        quality_display = res or 'unknown'
                    self._log(f"   Quality: {quality_display}")
                    # Old state without bandwidth can't safely resume multi-unknown masters
                    if bw is None and (not res or res == 'unknown'):
                        self._log(f"   [!] Incomplete quality info (pre-bandwidth state) — choose Fresh (n)")
                    sub_info = metadata.get('subtitle')
                    self._log(f"   Subtitle: {self.format_subtitle_label(sub_info)}")
                    self._log(f"   Segments: {segment_count} files")

                    resume = self.ui.confirm_resume(
                        f"{quality_display}  ·  "
                        f"Subtitle: {self.format_subtitle_label(sub_info)}  ·  "
                        f"{segment_count} segmen"
                    )
                    if resume is None:
                        self._log("   Dibatalkan.")
                        return False
                    choice = 'continue' if resume else 'fresh'

                    if choice == 'continue':
                        self.resume_mode = True
                        self.saved_resolution = metadata.get('resolution')
                        self.saved_bandwidth = metadata.get('bandwidth')
                        self.saved_subtitle = metadata.get('subtitle')
                        self._log(f"   [OK] Resuming with saved settings")
                    elif choice == 'fresh':
                        # Delete old segments and metadata for fresh start
                        import shutil
                        if os.path.exists(segments_dir):
                            shutil.rmtree(segments_dir)
                            self._log(f"   [OK] Deleted {segment_count} old segments")

                        metadata_file = f"{movie_dir}/download_state.json"
                        if os.path.exists(metadata_file):
                            os.remove(metadata_file)

            # Step 3/4: Get config playlist
            if not self.get_config_playlist():
                return False

            # Step 4/5: Check subtitles
            subtitle_url = None
            subtitle_handled_by_resume = False  # Flag to skip prompting when resume mode handles it

            if self.subtitles:
                # Check if resuming with saved subtitle choice
                if hasattr(self, 'resume_mode') and self.resume_mode and hasattr(self, 'saved_subtitle'):
                    if self.saved_subtitle is not None:
                        # Match by base URL (ignore JWT token in query params)
                        saved_base_url = self.saved_subtitle.get('url', '').split('?')[0]
                        for sub_info in self.subtitles:
                            sub_base_url = sub_info['url'].split('?')[0]
                            if sub_base_url == saved_base_url:
                                subtitle_url = sub_info['url']  # fresh URL with new JWT
                                self._log(f"[4/6] Using saved subtitle: {self.format_subtitle_label(sub_info)}")
                                subtitle_handled_by_resume = True
                                break
                        if not subtitle_handled_by_resume:
                            self._log(f"[4/6] Saved subtitle not available, prompting for selection")
                    else:
                        # saved_subtitle is None - user previously chose no subtitle
                        self._log(f"[4/6] Skipping subtitle (previous choice: none)")
                        subtitle_handled_by_resume = True

                # Batch downloads reuse the first episode's subtitle choice.
                if (
                    not subtitle_handled_by_resume
                    and self.batch_mode
                    and self.preferred_subtitle_language is not None
                ):
                    if self.preferred_subtitle_language == '':
                        self._log("[4/6] Skipping subtitle (previous choice)")
                        subtitle_handled_by_resume = True
                    elif self.preferred_subtitle_language == '__first__':
                        sub_info = self.subtitles[0]
                        subtitle_url = sub_info['url']
                        self._log(
                            f"[4/6] Using previous subtitle: "
                            f"{self.format_subtitle_label(sub_info)}"
                        )
                        subtitle_handled_by_resume = True
                    else:
                        for sub_info in self.subtitles:
                            code, _ = self.resolve_subtitle_language(sub_info)
                            if code == self.preferred_subtitle_language:
                                subtitle_url = sub_info['url']
                                self._log(
                                    f"[4/6] Using previous subtitle: "
                                    f"{self.format_subtitle_label(sub_info)}"
                                )
                                subtitle_handled_by_resume = True
                                break
                        if not subtitle_handled_by_resume:
                            self._log("[4/6] Previous subtitle not available; skipping")
                            subtitle_handled_by_resume = True

                # Prompt for subtitle selection if not handled by resume mode
                if not subtitle_handled_by_resume and len(self.subtitles) == 1:
                    sub_info = self.subtitles[0]
                    label = self.format_subtitle_label(sub_info)
                    self._log(f"[4/6] Subtitle found: {label}")
                    include_subtitle = self.ui.confirm_subtitle(label)
                    if include_subtitle is None:
                        self._log("   Dibatalkan.")
                        return False
                    if include_subtitle:
                        subtitle_url = sub_info['url']
                    else:
                        self._log("   Subtitle skipped")
                elif not subtitle_handled_by_resume:
                    self._log(f"[4/6] {len(self.subtitles)} subtitles found")
                    labels = [
                        self.format_subtitle_label(sub_info, index=i + 1)
                        for i, sub_info in enumerate(self.subtitles)
                    ]
                    labels.append("Lewati subtitle")
                    index = self.ui.select_subtitle(labels)
                    if index is None:
                        self._log("   Dibatalkan.")
                        return False
                    if index == len(self.subtitles):
                        self._log("   Subtitle skipped")
                    else:
                        sub_info = self.subtitles[index]
                        subtitle_url = sub_info['url']
                        self._log(
                            "   Selected: "
                            f"{self.format_subtitle_label(sub_info, index=index + 1)}"
                        )

            # Resolve the selected subtitle's real language once, for both the
            # state file and the MKV track metadata.
            subtitle_language_code = None
            subtitle_language_name = None
            if subtitle_url:
                selected_sub = next(
                    (s for s in self.subtitles if s['url'] == subtitle_url), None
                )
                subtitle_language_code, subtitle_language_name = (
                    self.resolve_subtitle_language(selected_sub)
                )

            if self.batch_mode:
                if subtitle_url:
                    self.preferred_subtitle_language = (
                        subtitle_language_code or '__first__'
                    )
                else:
                    self.preferred_subtitle_language = ''

            # Save metadata for resume capability (only in normal mode, not resume mode)
            if not hasattr(self, 'resume_mode') or not self.resume_mode:
                # Find subtitle info if subtitle was selected
                subtitle_info = None
                if subtitle_url:
                    for sub in self.subtitles:
                        if sub['url'] == subtitle_url:
                            subtitle_info = sub.copy()  # Make a copy so we don't modify the original
                            if subtitle_language_code:
                                subtitle_info['language'] = subtitle_language_code
                            if subtitle_language_name:
                                subtitle_info['language_name'] = subtitle_language_name
                            break
                self.save_download_metadata(
                    self.video_name,
                    self.selected_resolution,
                    subtitle_info,
                    getattr(self, 'selected_bandwidth', None),
                )

            # Step 5/6: Download
            movie_dir = f"output/{self.video_name}"
            segments_dir = f"{movie_dir}/segments"
            os.makedirs(segments_dir, exist_ok=True)
            output = f"{movie_dir}/{self.video_name}.mkv"
            if not self.download_with_ffmpeg(
                output,
                subtitle_url,
                segments_dir,
                subtitle_language=subtitle_language_code,
                subtitle_language_name=subtitle_language_name,
            ):
                return False

            self._log(f"\n[6/6] Done!")
            return True

        except Exception as e:
            self._log(f"\n[X] Error: {e}")
            import traceback
            self._log(traceback.format_exc())
            return False

def main(argv=None):
    prog = os.path.basename(sys.argv[0])
    if prog.lower().endswith(".exe"):
        prog = prog[:-4]
    parser = argparse.ArgumentParser(
        prog=prog or "idlixdownloader2",
        description="Download videos from IDLIX without opening a browser (HTTP API mode).",
    )
    parser.add_argument(
        "video_url",
        nargs="?",
        help="IDLIX movie/series page URL (search TUI opens if omitted)",
    )
    parser.add_argument(
        "config_url",
        nargs="?",
        help="Optional MajorPlay config URL (skips API resolution)",
    )
    parser.add_argument(
        "--season",
        type=int,
        help="Season number for series URLs that do not include one",
    )
    parser.add_argument(
        "--episode",
        type=int,
        help="Episode number for series URLs that do not include one",
    )
    args = parser.parse_args(argv)

    if not args.video_url and not tui.is_interactive():
        parser.error("video_url is required when not running interactively")

    if tui.is_interactive():
        return tui.run_app(
            args.video_url,
            config_url=args.config_url,
            season=args.season,
            episode=args.episode,
        )

    print("=" * 60)
    print("   idlixdownloader2")
    print("   Downloads videos from IDLIX")
    print("=" * 60 + "\n")

    downloader = MajorPlayDownloader()
    success = downloader.download(
        args.video_url,
        args.config_url,
        auto_extract=True,
        season=args.season,
        episode=args.episode,
    )
    return 0 if success else 1

if __name__ == "__main__":
    sys.exit(main())
