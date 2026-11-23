# fb-dl log dashboard (`web-ui-logs/`)

A small **self-contained Docker image** that serves a web UI to follow a gallery-dl
container's log file live and track album progress. It publishes **no port**: it is
meant to sit behind **Caddy** on a private Docker network.

## How to deploy it next to your existing gallery-dl setup

Your `compose.yml`, `config/`, `downloads-2/` stay **out of git** and untouched. From this
repo, copy only the `web-ui-logs/` folder into your gallery-dl project directory:

```bash
cp -r web-ui-logs /path/to/your/gallery-dl-project/
```

Then declare the service in your own compose, next to your existing `fb-dl` service
(`compose.example.yml` in this repo is a ready-to-paste reference):

```yaml
services:
  fb-dl: ...                 # your existing service, unchanged
  webui:
    build: ./web-ui-logs
    container_name: fb-dl-dashboard
    restart: unless-stopped
    environment:
      LOG_FILE: /downloads/logs/fb-dl.log   # = output.logfile.path in gallery-dl.conf
      ALBUMS_FILE: /config/albums.txt       # the -I input file
      MAX_RENDER_LINES: "20000"             # optional: browser DOM cap
    volumes:
      - ./downloads-2:/downloads:ro         # same mount as fb-dl, read-only
      - ./config:/config:ro                 # for albums.txt (contains cookies: never commit it)
    networks:
      - caddy

networks:
  caddy:
    external: true
```

And in your Caddyfile:

```
logs.example.tld {
    reverse_proxy fb-dl-dashboard:8081
}
```

```bash
docker compose up -d --build webui
```

## Features

- **Full log readable from the beginning**: on open, the UI loads the whole file from
  byte 0 in chunks (`GET /api/logs?offset=…&limit=…`, no size window), then follows
  live via SSE (`/api/stream`). The DOM is capped at `MAX_RENDER_LINES` (oldest lines
  dropped, a notice is shown); the search box reports match counts over the **whole
  file** (`/api/search`).
- **Album tracking** from your `-I` input file (`albums.txt`): URLs commented out by
  gallery-dl (`# url`) = **done**; URLs seen in the log (`Starting …Job for '<url>'`)
  but not done = **incomplete** (in progress or failed); the rest = **not started**.
  Endpoint: `GET /api/albums`, panel in the UI header.
- Level coloring + filters (debug / info / warning / error / other), text filter,
  pause, auto-scroll, counters (lines, jobs, warnings, errors, file size), raw log
  download (`/api/raw`).

### Scraping rejection detection

Lines matching common anti-bot / rejection patterns are counted as `rejections` in
`GET /api/stats` and highlighted in the UI with a red banner (click it to show only
those lines). Patterns are based on gallery-dl 1.32.21's Facebook extractor and
exception classes: `You've been temporarily blocked from viewing images.`,
`AuthRequired` / `AuthorizationError` / `AuthenticationError` / `Invalid login
credentials` / `cookies needed`, `ChallengeError` / captcha names, `HttpError`
with status 401/403/429, `429 Too Many Requests`, `This content isn't available`.
If a rejection message you hit is not detected, paste the exact log line and the
pattern will be added.

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

## Environment variables

| Var | Default | Description |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | log file to tail (must match `output.logfile.path`, via the `/downloads` mount) |
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
| `GET /api/stats` | counters (lines, jobs, errors, warnings, downloads, skips) |
| `GET /api/status` | existence / size / mtime of the log file |
| `GET /api/raw` | raw log file (download) |

## Notes

- With `mode: "a"` in gallery-dl.conf the log accumulates across runs — rotate or purge
  `downloads-2/logs/fb-dl.log` from time to time.
- If you change `output.logfile.path` in `config/gallery-dl.conf`, update `LOG_FILE`.
- `fb-dl` runs as `1000:1000`; the webui container (root) can read the log without issues.

## Development / tests

```bash
cd web-ui-logs
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest test_tailer test_api -v
LOG_FILE=/tmp/fb-dl.log ALBUMS_FILE=/tmp/albums.txt uvicorn app:app --port 8081
```

CI (`.github/workflows/ci.yml`) runs those tests, builds the Docker image and validates
`compose.example.yml` (including "no published ports").
