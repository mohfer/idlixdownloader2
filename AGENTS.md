# AGENTS.md

This file provides guidance to coding agents when working with code in this repository.

## Commands

**Setup:**
```bash
uv sync                              # Install dependencies
```

**Run:**
```bash
uv run idlixdownloader2                    # Search TUI: type a title, then pick seasons/episodes
uv run idlixdownloader2 <video_url>        # URL mode: skip search
uv run idlixdownloader2 <video_url> <config_url>         # Manual: skip resolution, use known config URL
uv run idlixdownloader2 <series_url> --season 1 --episode 2
```

**Test:**
```bash
uv run python -m unittest discover -s tests                     # Full suite
uv run python -m unittest discover -s tests -p "test_resolver.py"  # One module
uv run python -m unittest tests.test_downloader.TestLanguageParsing.test_parse_language_english  # Single test
```

No linter/formatter is configured. Dependencies: `cloudscraper`, `inquirerpy`, `textual`, `rich`. Python ≥ 3.13. FFmpeg must be on PATH (`ffmpeg -version`). No browser dependency.

## Architecture

`MajorPlayDownloader` in `src/idlixdownloader2/downloader.py` owns the pipeline; `IdlixResolver` in `src/idlixdownloader2/resolver.py` owns playback resolution. CLI is `idlixdownloader2` → `__main__.main()` → `downloader.main()`.

### Pipeline (`download`)

1. **Search** (no-URL mode) — Textual search screen with 350ms debounce calls `/api/search?q=...`; returns a page URL. Esc goes back to search.
2. **Series selection** — `prepare_series_targets` lists seasons via `/api/series/<slug>` and episodes via `/api/series/<slug>/season/<n>`. Textual `SelectionList` screens: Space toggles, `a` selects all, Enter confirms, Esc/left goes back. Returns one or more `(target_url, season, episode)` tuples.
3. **Metadata** (`get_video_info`) — scrape page for title (slug fallback).
4. **Config discovery** — `IdlixResolver.resolve` over HTTP only. `TurnstileRequired` is fatal. Manual config URL skips this.
5. **Resume check** — if `output/<title>/download_state.json` + `segments/` exist, prompt continue vs fresh.
6. **Playlist** (`get_config_playlist`) — fetch disguised M3U8 master; pick quality by bandwidth (or reuse saved/preferred); collect audio + subtitles. Highest = max bandwidth, not list index 1.
7. **Subtitle choice** — interactive, or restored from metadata by base URL (JWT query stripped). Labels via `format_subtitle_label` (playlist NAME/LANGUAGE, `/i18n/<code>/`, or generic `Subtitle`).
8. **Subtitle language** — `resolve_subtitle_language` prioritizes the `/i18n/<code>/` path, then the API `language` code, then the display name; `normalize_language` maps to ISO 639-2 (`eng`/`ind`). The mux step tags the track with this code (never a hardcoded language).
9. **Download + mux** (`download_segments` → `download_with_ffmpeg`) — parallel fMP4 segments with Rich progress bars, binary concat, FFmpeg → MKV. Batch downloads reuse the first episode's quality/subtitle preference (`preferred_bandwidth`, `preferred_subtitle_language`).

### HTTP resolver (`resolver.py`)

1. `GET /api/movies/<slug>` or `/api/series/<slug>/season/<s>/episode/<e>` → content id.
2. `GET /api/watch/play-info/<type>/<id>` → `gateToken`, `unlockAt` (server gate, ~15s).
3. `_wait_for_unlock` sleeps until `unlockAt` (max 16s per sleep; never bypasses the gate).
4. `POST /api/watch/session/claim` with `{"gateToken": ...}` → `kind: "pentos"` claim, or `kind: "pending"` + `remainingMs` (retry after waiting).
5. `POST <redeemUrl>` with `{"claim": ..., "mode": "browser"}` → master playlist + subtitle tracks.
6. `TurnstileRequired` is raised when `play-info`/claim returns `verification`; `ContentNotFound` on 404s.

The master token must be appended to variant and segment URLs; subtitle URLs work without it. The redeem host must be `*.majorplay.net/api/play`.

### Resume / metadata

- State file: `output/<video_name>/download_state.json` (`resolution`, `bandwidth`, `subtitle` dict, timestamp).
- Resume matches quality by **bandwidth** (MajorPlay often omits `RESOLUTION=`, so label collides as `unknown`). Legacy state without bandwidth + unknown resolution cannot auto-match — prompt / prefer Fresh.
- Helpers: `save_download_metadata`, `load_download_metadata`, `get_language_from_subtitle_url` (`/i18n/<code>/` → display name), `format_subtitle_label`, `sanitize_filename`.
- Resume keeps quality/subtitle prefs; segment download skips existing `.m4s`. Fresh mode deletes segments + state.
- Subtitle rematch ignores JWT query so resume survives token expiry; resolution must still produce a fresh config URL/token for new segment fetches.
- Different qualities ⇒ different segment counts; wrong-quality resume can mix partial segments — prefer Fresh if match is ambiguous.

### MajorPlay obfuscation

Standard HLS, disguised:
- Master playlist as `.json`; fMP4 fragments as `.js`/`.png`/`.jpg`/`.css`/`.svg`/`.html`
- Multi-CDN; JWT on query (~55 min lifetime) must stay on every authenticated URL
- Pre-roll VAST ads are replaced by the API gate; no ad interaction is needed

### Design choices worth preserving

- **HTTP only**: cloudscraper resolves the API chain in ~17s without rendering. No browser path exists.
- **Binary concat, not FFmpeg concat demuxer**: `init + frags` is already valid fMP4; faster path.
- **Retry wrapper (`_http_get`)**: playlist/init/subtitle/config fetches retry 3× on connection resets and 5xx; segment downloads retry 3× on top of that.
- **Textual search + menus**: Textual owns the whole interactive session (search, selection screens, progress, logs). InquirerPy is only the non-interactive console fallback.
- **Batch preferences**: the first episode's quality (bandwidth) and subtitle language are reused for the rest of the batch.

### Layout

- `src/idlixdownloader2/resolver.py` — HTTP API resolver + search
- `src/idlixdownloader2/downloader.py` — orchestration + download logic
- `src/idlixdownloader2/tui.py` — Textual app/screens + console fallback
- `src/idlixdownloader2/__main__.py` — CLI entry
- `tests/test_downloader.py` — unittest (playlist, video info, metadata, language, subtitle labels, bandwidth resume, series selection, TUI fallback)
- `tests/test_resolver.py` — resolver chain, wait logic, URL parsing, error mapping
- `output/` — downloads (gitignored); per-title dirs hold `segments/`, `download_state.json`, final `.mkv`

### Debug

- **HTTP resolution fails**: check whether the API returned `verification` (Turnstile, unsupported) or a 404 (bad slug/season/episode).
- **Claim stuck `pending`**: server gate not yet open; the resolver waits automatically. Persistent pending suggests clock skew — retry.
- **HTTP 403 on segments**: JWT expired → re-resolve; do not reuse stale config URLs for long runs.
- **Many segment failures**: 3 retries/segment already; wait and re-run if CDN is flaky.
