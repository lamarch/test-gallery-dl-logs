# web-dashboard — live log viewer for gallery-dl (fb-dl)

This repo is the **content of the `web-dashboard/` folder** of your gallery-dl
project: a self-contained Docker image (Dockerfile at the repo root) that serves a
web UI to follow the `fb-dl` container's log file live and track album progress.
It publishes **no port** — it is served by **Caddy** on a private Docker network.

Only `config/albums.txt` is mounted into the webui container (single file) — the rest
of `config/` (including `fb-cookies.txt`) never enters it. CI asserts this.

## Expected layout

```
./gallery-dl/                     <- compose project directory (your own, out of git)
├── compose.yml                   <- your compose: fb-dl + webui (snippet: compose.example.yml)
├── config/                       <- gallery-dl.conf, fb-cookies.txt, albums.txt (never commit)
├── downloads-2/                  <- downloads + logs/fb-dl.log
└── web-dashboard/                <- THIS repo (the build context of the webui service)
    ├── Dockerfile
    ├── app.py  tailer.py  static/  requirements*.txt  test_*.py
    └── compose.example.yml       <- snippet to paste into ../compose.yml
```

If you ever `git init` the parent `gallery-dl/` project, add `config/` and
`downloads-2/` to its `.gitignore` (cookies + downloaded media must stay out of git).

## Quickstart

```bash
cd gallery-dl
git clone https://github.com/lamarch/test-gallery-dl-logs.git web-dashboard
touch config/albums.txt   # single-file bind mount needs the file to exist
# paste the `webui` service from web-dashboard/compose.example.yml into your
# compose.yml (under the existing `services:` key) + top-level `networks: caddy: external: true`
# (network name confirmed as `caddy`; check with `docker network ls`)
# Caddyfile:  logs.example.tld { reverse_proxy fb-dl-dashboard:8081 }

docker compose up -d --build webui   # builds ./web-dashboard and starts the dashboard
docker compose up -d fb-dl            # your scraper, unchanged
```

Then open `https://logs.example.tld`. `docker compose logs -f fb-dl` still works.

## Features

- **Fast by default, full history on demand**: initial load reads only the last 1000
  lines (`/api/logs?tail=N`, windowed server-side read); older lines are loaded with
  the "⤒ Load earlier" button (`/api/logs?before=OFFSET&limit=N`, backwards chunks);
  the whole file from byte 0 remains available via `/api/logs?offset=0&limit=N`.
  Live follow via SSE (`/api/stream`). DOM capped at `MAX_RENDER_LINES`.
- **Parsed view**: toggle Raw/Parsed — log lines become structured entries
  (💤 sleeping Xs (reason), ▶ job/album start, 🔍 extractor, 🌐 GET → status+size,
  🖼 downloaded file, ❌ failed download, 🚫 scraping rejection, …).
- **Scraper status bar**: current sleep (reason + estimated wake-up time), current
  album + files downloaded on it, albums done/total + current album, total files
  (from `/api/stats` + `/api/albums`, refreshed every 5 s).
- **Albums panel**: from your `-I` input file (`albums.txt`) crossed with the log:
  `# url` (commented out by gallery-dl) = **done**; `Starting …Job for '<url>'` seen
  but not done = **incomplete**; the rest = **not started**. Click an album to search
  its URL in the logs.
- **Scraping rejection detection**: rejection-pattern lines are counted (`rejections`
  in `/api/stats`) and shown with a red banner — click it to show only those lines.
  Patterns based on gallery-dl 1.32.21's Facebook extractor: `You've been temporarily
  blocked from viewing images.`, `AuthRequired` / `AuthorizationError` /
  `AuthenticationError` / `Invalid login credentials` / `cookies needed`,
  `ChallengeError` / captcha names, `HttpError` 401/403/429, `429 Too Many Requests`,
  `This content isn't available`. Paste any undetected rejection line to add its pattern.
- Level filters, full-text filter, pause, auto-scroll, raw log download (`/api/raw`).

Note on "files downloaded": counted from `[downloader.http][debug] Adjusting filename
extension of '…' to '…'` lines (one per downloaded file in your logfile).

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

## Environment variables (service `webui` in your compose)

| Var | Default | Description |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | log file to tail (must match `output.logfile.path` in `config/gallery-dl.conf`) |
| `ALBUMS_FILE` | `/config/albums.txt` | `-I` input file for album status; if missing, the albums panel is hidden |
| `CHUNK_LINES` | `5000` | lines per chunk for forward reads |
| `MAX_RENDER_LINES` | `20000` | max lines kept in the browser DOM |
| `TAIL_LINES` | `500` | default N for `/api/logs?tail=N` |
| `TAIL_WINDOW_BYTES` | `4194304` (4 MiB) | max bytes scanned for a tail/backwards read |
| `SCAN_MAX_BYTES` | `0` (whole file) | if > 0, `/api/stats`, `/api/search` and album-start detection only scan the last N bytes (`truncated: true` reported). The example compose sets 128 MiB since the log grows unbounded in `mode: "a"` |

## API

| Endpoint | Description |
|---|---|
| `GET /` | dashboard page |
| `GET /api/logs?offset=O&limit=N` | N lines from byte offset O (`eof`, `has_more`) |
| `GET /api/logs?before=O&limit=N` | N lines ending at byte offset O (backwards, for "load earlier") |
| `GET /api/logs?tail=N` | last N lines (`offset`, `start_offset`, `has_more`) |
| `GET /api/stream?since=OFFSET` | SSE stream of new lines |
| `GET /api/search?q=…&limit=N` | search the scanned region, total count + first matches |
| `GET /api/albums` | albums with status + counts + `current` (first non-done, file order) |
| `GET /api/stats` | counters incl. `files`, `current_album`, `current_album_files`, `last_sleep`, `rejections`, `truncated` |
| `GET /api/status` | existence / size / mtime of the log file |
| `GET /api/raw` | raw log file (download) |

## Notes

- With `mode: "a"` and no rotation, `fb-dl.log` grows forever. Browsing stays chunked;
  the one-pass scans are capped via `SCAN_MAX_BYTES`. If you add logrotate
  `copytruncate` later, truncation is detected live.
- If you change `output.logfile.path` in `config/gallery-dl.conf`, update `LOG_FILE`.
- `fb-dl` runs as `1000:1000`; the webui container (root) can read the log without issues.

## Development / tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest test_tailer test_api -v
LOG_FILE=/tmp/fb-dl.log ALBUMS_FILE=/tmp/albums.txt uvicorn app:app --port 8081
```

CI (`.github/workflows/ci.yml`) runs those tests, builds the image from the repo root
and validates `compose.example.yml` (no `ports`, `build: ./web-dashboard`, external
caddy network, albums.txt-only config mount).
