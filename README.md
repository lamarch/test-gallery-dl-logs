# fb-dl — gallery-dl log dashboard

A small Docker web UI to follow your `fb-dl` (gallery-dl) container's log file live,
instead of scrolling through huge `docker logs`: search, level filters, counters,
pause, auto-scroll.

## Architecture

- **`fb-dl`**: your existing gallery-dl service, **unchanged**. Your config
  (`config/gallery-dl.conf`) already writes structured logs to
  `/downloads/logs/fb-dl.log` via `output.logfile` (level `debug`, format
  `[{asctime}] [{name}][{levelname}] {message}`, mode `a`).
- **`webui`**: a small Python (FastAPI) app added to the same `compose.yml`. It mounts
  `./downloads-2` read-only and tails `/downloads/logs/fb-dl.log`, pushing new lines
  to the browser over SSE (Server-Sent Events).

Real log line format (verified with gallery-dl 1.32.21 and your config):

```
[2026-11-23 16:59:43] [gallery-dl][debug] Starting DownloadJob for 'https://...'
[2026-11-23 16:59:44] [gallery-dl][error] Unsupported URL '...'
```

## Usage

```bash
cp .env.example .env        # adjust WEBUI_PORT if needed
docker compose up -d --build
```

Then open <http://localhost:8081>.

Features: live stream, toggleable auto-scroll, pause (incoming lines are still counted),
full-text search, level filters (debug / info / warning / error / other), counters
(lines, jobs started, warnings, errors, file size), raw log download ("⬇ Raw log").

## Notes

- Your config uses `mode: "a"`: the log **accumulates across runs**. The dashboard shows
  the whole history (last 500 lines on load, display capped at 3000 lines in the
  browser, and only the most recent 1 MiB is read for history). Consider rotating or
  purging `downloads-2/logs/fb-dl.log` from time to time.
- If you change the `path` of `output.logfile` in `config/gallery-dl.conf`, update the
  `LOG_FILE` variable of the `webui` service in `compose.yml`.
- `fb-dl` runs as `1000:1000`; the webui container (root) can read the log without
  permission issues.
- The webui starts even if the log file does not exist yet (it waits for it to appear).

## Local development of the webui

```bash
cd webui
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
LOG_FILE=/tmp/fb-dl.log uvicorn app:app --port 8081
```

## Webui API

| Endpoint | Description |
|---|---|
| `GET /` | dashboard page |
| `GET /api/logs?tail=N` | last N lines + resume offset |
| `GET /api/stream?since=OFFSET` | SSE stream of new lines |
| `GET /api/stats` | counters (lines, jobs, errors, warnings, downloads, skips) |
| `GET /api/status` | existence / size / mtime of the log file |
| `GET /api/raw` | raw log file (download) |
