# idlixdownloader2

A fast, browser-free downloader for [IDLIX](https://z2.idlixku.com). Search for a movie or series, pick seasons and episodes with a keyboard-driven TUI, choose quality and subtitles, and get a ready-to-play MKV.

No URL hunting, no page rendering, and no browser process.

## Features

- **One consistent TUI** — search, season/episode pickers, quality/subtitle prompts, progress, and process logs all live in the same Textual app.
- **Search-first TUI** — run the command, type a title, and results appear after a 350 ms debounce.
- **Multi-select** — use `Space` to toggle seasons/episodes and `a` to select all. Download one episode, a full season, or several seasons in one run.
- **Browser-free** — playback is resolved through IDLIX's HTTP API, not by opening a page.
- **Quality and subtitle pickers** — arrow-key menus with bandwidth-aware quality selection and correct ISO 639-2 subtitle tags (`eng`/`ind`).
- **Batch downloads** — quality and subtitle choices are asked once and reused for the rest of the batch.
- **Live progress** — a progress bar for video/audio segments, live download logs, and an FFmpeg status line.
- **Resume support** — interrupted downloads keep segment progress and saved quality/subtitle preferences.
- **Parallel fetching** — five segment workers with automatic retries and JWT-aware URL handling.
- **Clean output** — fMP4 fragments are assembled correctly and muxed to a single MKV.

## Requirements

- **Python 3.13+**
- **[uv](https://docs.astral.sh/uv/)**
- **FFmpeg** available on `PATH` — verify with `ffmpeg -version`

> [!IMPORTANT]
> FFmpeg is required for the final mux. The downloader handles the HLS/fMP4 part itself.

No browser is required.

## Installation

```bash
git clone https://github.com/mohfer/idlixdownloader2.git
cd idlixdownloader2
uv sync
```

## Usage

### Search and download

```bash
uv run idlixdownloader2
```

Then:

1. Type a movie or series title. Results appear automatically after you stop typing.
2. Press `Down` to enter the results, then `Enter` to open one.
3. For a series, select one or more seasons with `Space` (`a` selects all), press `Enter`, then select episodes the same way.
4. Pick quality and subtitle.
5. Watch the progress bar and live logs; the final MKV is written to `output/`.

All prompts stay inside the TUI. There is no switch back to plain terminal output during an interactive session.

### Keyboard controls

| Key | Action |
| --- | --- |
| `Up` / `Down` | Move between items |
| `Space` | Toggle a season/episode |
| `a` | Select all items in the current list |
| `Enter` | Confirm / open |
| `Esc` / `Left` | Go back one screen |

Going back from the episode list returns to the season list. Going back from the season list returns to search.

### URL mode

You can skip search and pass a page URL directly:

```bash
# Movie
uv run idlixdownloader2 "https://z2.idlixku.com/movie/toy-story-5-2026"

# Specific episode
uv run idlixdownloader2 "https://z2.idlixku.com/series/oh-my-guard-2026/season/1/episode/1"
```

For a parent series URL, the TUI opens the season/episode pickers. Skip the menus with explicit flags:

```bash
uv run idlixdownloader2 "https://z2.idlixku.com/series/oh-my-guard-2026" --season 1 --episode 2
```

### Manual config URL

If you already have a MajorPlay config URL, pass it as the second argument to skip API resolution:

```bash
uv run idlixdownloader2 "https://z2.idlixku.com/movie/..." "https://e2e.majorplay.net/.../config.json?t=JWT_TOKEN"
```

### Resume an interrupted download

Re-run the same command. If `output/<title>/download_state.json` and segments exist, the tool offers:

- **Lanjutkan** — reuse the saved quality/subtitle and skip completed segments.
- **Mulai baru** — delete old segments and start fresh.

Resume matches quality by **bandwidth**, so it still works when MajorPlay omits `RESOLUTION=` and only reports kbps.

## How it works

### Search

The search screen calls:

```text
GET /api/search?q=<query>
```

Results include `contentType` and `slug`. Selecting a result builds `/movie/<slug>` or `/series/<slug>` and continues to the pickers.

### Playback resolution

IDLIX's player resolves a stream in four HTTP calls:

1. `GET /api/movies/<slug>` or `/api/series/<slug>/season/<s>/episode/<e>` — content id
2. `GET /api/watch/play-info/<type>/<id>` — gate token and unlock time
3. Wait for the server-side gate (~15 seconds)
4. `POST /api/watch/session/claim` — exchange the gate token for a playback claim
5. `POST <redeemUrl>` — get the MajorPlay master playlist and subtitle tracks

> [!NOTE]
> The ~15 second gate is enforced by the server. Claiming earlier returns `kind: "pending"` with `remainingMs`; the resolver waits and retries automatically.

> [!WARNING]
> This tool does not solve Cloudflare Turnstile. If the API asks for verification, that title cannot be downloaded and the command stops.

### Download and mux

1. Parse the disguised M3U8 master playlist.
2. Pick a video variant by bandwidth, plus the audio track and subtitles.
3. Download the init segment and all fMP4 fragments in parallel (5 workers, 3 attempts per segment).
4. Concatenate `init + fragments` into a complete MP4.
5. Mux video, audio, and subtitle into `output/<title>/<title>.mkv`.

The JWT token from the master playlist is appended to variant and segment URLs. Subtitle URLs work without it.

## Project structure

```text
idlixdownloader2/
├── src/idlixdownloader2/
│   ├── __init__.py
│   ├── __main__.py        # CLI entry point
│   ├── downloader.py      # pipeline, resume, progress, FFmpeg mux
│   ├── resolver.py        # search + HTTP API resolver
│   └── tui.py             # Textual app/screens + console fallback
├── tests/
│   ├── test_downloader.py
│   └── test_resolver.py
├── output/                # downloads (gitignored)
├── pyproject.toml
└── uv.lock
```

## Troubleshooting

**Search shows no results**

- Wait for the debounce to finish, or check your connection.
- Try a shorter query (at least 2 characters).
- If the API is unavailable, use URL mode as a workaround.

**Resolution fails**

- Transient API/CDN failures are retried automatically; run again first.
- A `TurnstileRequired` error means the API wants browser verification, which this tool intentionally does not handle.
- Check that the title still exists on IDLIX.

**Claim stays `pending`**

- The gate is server-enforced. The resolver waits automatically.
- Persistent `pending` usually means clock skew; retry, or sync your system time.

**Connection reset while downloading**

- Connection resets and HTTP 5xx responses are retried (3 attempts).
- Re-run the command; completed segments are skipped.

**Segments fail repeatedly**

- Check your connection and retry.
- If the CDN is flaky, wait a few minutes.
- A fresh resolution gets a new JWT when the old one expires.

**Subtitle track has the wrong language label**

- The MKV track is tagged from the selected subtitle (`eng` or `ind`), not hardcoded.
- For files downloaded by an older version, remux without re-downloading:

```bash
ffmpeg -i old.mkv -map 0 -c copy \
  -metadata:s:s:0 language=eng \
  -metadata:s:s:0 title=English \
  -y fixed.mkv
```

**FFmpeg not found**

- Install FFmpeg and add its `bin` directory to `PATH`.
- Verify with `ffmpeg -version`.

**Resume picked a different quality**

- Different qualities have different segment counts; that is normal.
- If the saved quality is unavailable or ambiguous, choose **Mulai baru**.
