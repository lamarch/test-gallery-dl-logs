# fb-dl log dashboard

Self-contained Docker Compose project: your `fb-dl` gallery-dl service plus a `webui`
container that serves a live log dashboard (with album tracking) behind **Caddy** —
no published ports, only an external `caddy` Docker network.

`config/` (cookies, gallery-dl.conf, albums.txt) and `downloads-2/` are **gitignored**:
they live only on your machine.

## Quickstart

```bash
git clone https://github.com/lamarch/test-gallery-dl-logs.git
cd test-gallery-dl-logs

# 1. drop your private data into the gitignored dirs:
#      config/gallery-dl.conf   config/fb-cookies.txt   config/albums.txt
#      downloads-2/             (created automatically on first run if missing)
# 2. make sure the gallery-dl image exists (build it once from gallery-dl sources):
#      docker build -t gallery-dl:latest .
# 3. Caddyfile:
#      logs.example.tld { reverse_proxy fb-dl-dashboard:8081 }

docker compose up -d --build     # builds web-ui-logs and starts everything
```

Then open `https://logs.example.tld`. Run the scraper with `docker compose up -d fb-dl`
(or `restart fb-dl`); `docker compose logs -f fb-dl` still works as before.

## Layout

- `compose.yml` — `fb-dl` (your service, unchanged) + `webui` (built from source).
- `web-ui-logs/` — the self-contained webui image: Dockerfile + FastAPI app
  (`app.py`, `tailer.py`) + static UI (`static/index.html`) + tests.
- `.github/workflows/ci.yml` — tests, image build, compose validation.

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
- **Scraping rejection detection**: lines matching common anti-bot / rejection
  patterns are counted as `rejections` in `GET /api/stats` and highlighted in the UI
  with a red banner (click it to show only those lines). Patterns are based on
  gallery-dl 1.32.21's Facebook extractor and exception classes: `You've been
  temporarily blocked from viewing images.`, `AuthRequired` / `AuthorizationError` /
  `AuthenticationError` / `Invalid login credentials` / `cookies needed`,
  `ChallengeError` / captcha names, `HttpError` with status 401/403/429,
  `429 Too Many Requests`, `This content isn't available`. If a rejection message you
  hit is not detected, paste the exact log line and the pattern will be added.
- Level coloring + filters (debug / info / warning / error / other), text filter,
  pause, auto-scroll, counters (lines, jobs, warnings, errors, rejections, file size),
  raw log download (`/api/raw`).

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

## Environment variables (service `webui`)

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
  `downloads-2/logs/fb-dl.log` from time to time.
- If you change `output.logfile.path` in `config/gallery-dl.conf`, update `LOG_FILE`
  in `compose.yml`.
- `fb-dl` runs as `1000:1000`; the webui container (root) can read the log without issues.

## Development / tests

```bash
cd web-ui-logs
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest test_tailer test_api -v
LOG_FILE=/tmp/fb-dl.log ALBUMS_FILE=/tmp/albums.txt uvicorn app:app --port 8081
```

CI runs those tests, builds the Docker image and validates `compose.yml`
(including the assertion that `webui` publishes no ports).
