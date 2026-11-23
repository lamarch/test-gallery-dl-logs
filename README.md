# web-dashboard — live log viewer for gallery-dl (fb-dl)

This repo is the **content of the `web-dashboard/` folder** of your gallery-dl
project: a self-contained Docker image (Dockerfile at the repo root) that serves a
web UI to follow the `fb-dl` container's log file live and track album progress.
It publishes **no port** — it is served by **Caddy** on a private Docker network.

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
# paste the `webui` service from web-dashboard/compose.example.yml into your
# compose.yml (under the existing `services:` key) + the top-level `networks: caddy: external: true`
# Caddyfile:  logs.example.tld { reverse_proxy fb-dl-dashboard:8081 }

docker compose up -d --build webui   # builds ./web-dashboard and starts the dashboard
docker compose up -d fb-dl            # your scraper, unchanged
```

Then open `https://logs.example.tld`. `docker compose logs -f fb-dl` still works.

## Features

- **Full log readable from the beginning**: on open, the UI loads the whole file from
  byte 0 in chunks (`GET /api/logs?offset=…&limit=…`, no size window), then follows
  live via SSE (`/api/stream`). The DOM is capped at `MAX_RENDER_LINES` (oldest lines
  dropped, a notice is shown); the search box reports match counts over the **whole
  file** (`/api/search`).
- **Album tracking** from your `-I` input file (`albums.txt`) crossed with the log
  (`GET /api/albums` + UI panel): lines commented out by gallery-dl (`# url`) =
  **done**; URLs seen in the log (`Starting …Job for '<url>'`) but not done =
  **incomplete**; the rest = **not started**.
- **Scraping rejection detection**: lines matching anti-bot / rejection patterns are
  counted as `rejections` in `GET /api/stats` and highlighted in the UI with a red
  banner (click it to show only those lines). Patterns based on gallery-dl 1.32.21's
  Facebook extractor: `You've been temporarily blocked from viewing images.`,
  `AuthRequired` / `AuthorizationError` / `AuthenticationError` / `Invalid login
  credentials` / `cookies needed`, `ChallengeError` / captcha names, `HttpError`
  401/403/429, `429 Too Many Requests`, `This content isn't available`. If a
  rejection message you hit is not detected, paste the exact log line and the pattern
  will be added.
- Level coloring + filters (debug / info / warning / error / other), text filter,
  pause, auto-scroll, counters (lines, jobs, warnings, errors, rejections, file size),
  raw log download (`/api/raw`).

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

## Environment variables (service `webui` in your compose)

| Var | Default | Description |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | log file to tail (must match `output.logfile.path` in `config/gallery-dl.conf`) |
| `ALBUMS_FILE` | `/config/albums.txt` | `-I` input file for album status; if missing, the albums panel is hidden |
| `CHUNK_LINES` | `5000` | lines per chunk when reading history |
| `MAX_RENDER_LINES` | `20000` | max lines kept in the browser DOM |
| `TAIL_LINES` | `500` | default N for `/api/logs?tail=N` |

## API

| Endpoint | Description |
|---|---|
| `GET /` | dashboard page |
| `GET /api/logs?offset=O&limit=N` | N lines from byte offset O (`eof` flag, `max_render`) |
| `GET /api/logs?tail=N` | last N lines of the whole file |
| `GET /api/stream?since=OFFSET` | SSE stream of new lines |
| `GET /api/search?q=…&limit=N` | search the whole file, returns total count + first matches |
| `GET /api/albums` | album list with status (`done` / `incomplete` / `not_started`) + counts |
| `GET /api/stats` | counters (lines, jobs, errors, warnings, rejections, downloads, skips) |
| `GET /api/status` | existence / size / mtime of the log file |
| `GET /api/raw` | raw log file (download) |

## Notes

- With `mode: "a"` in gallery-dl.conf the log accumulates across runs — rotate or purge
  `downloads-2/logs/fb-dl.log` from time to time (the app detects truncation/rotation).
- If you change `output.logfile.path` in `config/gallery-dl.conf`, update `LOG_FILE`
  in your compose.
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
caddy network).
