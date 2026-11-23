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
# Caddyfile (with auth — the dashboard exposes /api/raw and file downloads, do not
# leave it public; flush_interval -1 is required so the SSE live-follow is not buffered):
#   logs.example.tld {
#       basicauth /* {
#           admin JDJhJDE0JG...   # generate with: caddy hash-password
#       }
#       reverse_proxy fb-dl-dashboard:8081 {
#           flush_interval -1
#       }
#   }
# Alternative: restrict to your VPN/office IPs instead of basicauth, e.g.
#   @allowed remote_ip 10.0.0.0/8 192.168.1.0/24
#   handle @allowed { reverse_proxy fb-dl-dashboard:8081 { flush_interval -1 } }
#   handle { respond 403 }

docker compose up -d --build webui   # builds ./web-dashboard and starts the dashboard
docker compose up -d fb-dl            # your scraper, unchanged
```

Then open `https://logs.example.tld` (behind the auth above). `docker compose logs -f fb-dl` still works.

The webui container runs as `1000:1000` (same uid as fb-dl, so it can read the log)
with `no-new-privileges`, and the image has a `HEALTHCHECK` on `/api/status`.

## Features

Four tabs in the header, plus an **Export JSON** button (`/api/export`: full tree +
albums overview + disk index as a downloadable attachment).

### Log
- **Fast by default, full history on demand**: initial load reads only the last 1000
  lines (`/api/logs?tail=N`, windowed server-side read); older lines load with the
  "⤒ Load earlier" button (`/api/logs?before=OFFSET&limit=N`, backwards chunks);
  the whole file from byte 0 remains available via `/api/logs?offset=0&limit=N`.
  Live follow via SSE (`/api/stream`, green dot = connected). DOM capped at
  `MAX_RENDER_LINES`.
- **Parsed view**: toggle Raw/Parsed — log lines become structured entries
  (💤 sleeping Xs (reason), ▶ job/album start, 🔍 extractor, 🌐 GET → status+size,
  💾 downloaded file, ❌ failed download, 🚫 scraping rejection, …).
- Search over the whole scanned file (count + matches), level filters, full-text
  filter, pause, auto-scroll, raw log download (`/api/raw`).

### Status bar (chips under the header, refreshed every 5 s)
Current sleep with reason + estimated wake-up, current run #N (restart counter) and
its start time, current album + files downloaded on it, albums done/total
(done ◐ incomplete ○ not-started), total files, errors, rejections, last log activity.

### Tree — session → album → photo
The log is parsed into a tree. **Each run = one session branch** (a new branch starts
at every `[gallery-dl][debug] Version …` line, so restarts/power cuts are visible):
- **Session**: start/end, photos, files, bytes, total sleep time, errors, rejections.
- **Album**: Facebook browser link `https://www.facebook.com/media/set/?set=a.<id>&type=3`,
  set id + vanity, start/end, end reason (done per `albums.txt` `#` marker / banned /
  error / interrupted), status badge, same stats, an "album logs" button showing the
  exact raw log lines of that album.
- **Photos**: lazy-loaded and **paginated** (`PHOTOS_PAGE_SIZE=500` per page +
  "load more", so albums with thousands of photos stay fast). Per photo: status
  (downloaded / failed / pending), fbid with browser link
  `https://www.facebook.com/photo/?fbid=<id>&set=a.<set_id>`, bytes, download sleep,
  on-disk state. Expanding a photo shows its **EXIF/mtime state** and its **own raw
  log lines** (`/api/photo/{s}/{a}/{p}/logs`).

### Albums — cross-session view
Album-centric (keyed by set id, not chronological): which sessions touched it, a
**retried** badge when an album appears in more than one session (e.g. after a crash
or an anti-bot ban), total photos/files/bytes/sleeps, errors, rejections, and how
many files are actually on disk. Deep-linkable with `#album=<set_id>` anchors.

### Files — disk view
Read-only index of `DOWNLOADS_DIR` (the `logs/` dir is skipped). Expected layout
`downloads/facebook/<user>/<album> (a.<set_id>)/YYYYMMDD_HHMMSS_<fbid>.<ext>` —
the album set id comes from the `(a.…)` directory, the photo fbid and the capture
date from the filename. Thumbnails (`/api/file?path=…`, traversal-safe, images only),
filters by album / name / fbid / **EXIF state**, per-row EXIF button, rescan button.

**EXIF check** (`/api/exif`): compares EXIF `DateTimeOriginal` with the file mtime
(±2 s): `synced` / `mismatch` / **`no_exif` — meaning your external EXIF/mtime script
did not run on that file** — plus the date parsed from the filename.

### Scraping rejection detection
Rejection-pattern lines are counted and shown with a red banner — click it to show
only those lines. Patterns based on gallery-dl 1.32.21's Facebook extractor:
`You've been temporarily blocked from viewing images.`, `AuthRequired` /
`AuthorizationError` / `AuthenticationError` / `Invalid login credentials` /
`cookies needed`, `ChallengeError` / captcha names, `HttpError` 401/403/429,
`429 Too Many Requests`, `This content isn't available`. Paste any undetected
rejection line to add its pattern. (`[429/500]` progress lines deliberately do NOT
match.)

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

## Environment variables (service `webui` in your compose)

| Var | Default | Description |
|---|---|---|
| `LOG_FILE` | `/downloads/logs/fb-dl.log` | log file to tail (must match `output.logfile.path` in `config/gallery-dl.conf`) |
| `ALBUMS_FILE` | `/config/albums.txt` | `-I` input file for album status; if missing, the albums panels are hidden |
| `DOWNLOADS_DIR` | `/downloads` | root of the disk index (Files tab, on-disk merging); must be the same mount as fb-dl |
| `CHUNK_LINES` | `5000` | lines per chunk for forward reads |
| `MAX_RENDER_LINES` | `20000` | max lines kept in the browser DOM |
| `TAIL_LINES` | `500` | default N for `/api/logs?tail=N` |
| `TAIL_WINDOW_BYTES` | `4194304` (4 MiB) | max bytes scanned for a tail/backwards read |
| `SCAN_MAX_BYTES` | `0` (whole file) | if > 0, `/api/stats`, `/api/search`, album-start detection and the tree parser only scan the last N bytes (`truncated: true` reported). The example compose sets 128 MiB since the log grows unbounded in `mode: "a"` |
| `PHOTOS_PAGE_SIZE` | `500` | photos per page in the Tree tab ("load more" pagination) |
| `TREE_MAX_PHOTOS` | `50000` | RAM cap on photo nodes materialized in the parsed tree (`<= 0` = unlimited). Beyond the cap, photos are still counted and `/api/album/{s}/{a}` re-parses the album's log span on demand (per-photo log spans keep working) |
| `DISPLAY_TZ` | `TZ` env, else `UTC` | IANA timezone used to **display** the log timestamps (logs are UTC). The UI parses them as UTC and renders in this zone via `Intl`; exposed by `/api/status` as `timezone` |
| `FILES_CACHE_TTL` | `30` | seconds the disk index (`/api/files`) is cached before an automatic rescan, so new downloads show as "on disk" without a manual refresh (`0` = rescan every call, `?refresh=1` forces) |
| `FILES_MAX` | `50000` | max files indexed by the disk scan (walk stops early, `truncated: true` reported) |

## API

| Endpoint | Description |
|---|---|
| `GET /` | dashboard page |
| `GET /api/logs?offset=O&limit=N` | N lines from byte offset O (`eof`, `has_more`) |
| `GET /api/logs?before=O&limit=N` | N lines ending at byte offset O (backwards, "load earlier") |
| `GET /api/logs?tail=N` | last N lines (`offset`, `start_offset`, `has_more`) |
| `GET /api/stream?since=OFFSET` | SSE stream of new lines |
| `GET /api/search?q=…&limit=N` | search the scanned region, total count + first matches |
| `GET /api/albums` | albums.txt statuses (done/incomplete/not_started) + counts + `current` |
| `GET /api/stats` | counters incl. `files`, `current_album`, `current_album_files`, `last_sleep`, `rejections`, `truncated` |
| `GET /api/tree` | parsed session → album → photo tree (`runs`, `done_urls`) |
| `GET /api/album/{s}/{a}?offset=O&limit=N` | one album + its photos (paginated, merged with the disk index: `on_disk`, `path`, `link_photo`) |
| `GET /api/album/{s}/{a}/logs` | raw log lines of that album |
| `GET /api/photo/{s}/{a}/{p}/logs` | raw log lines of that photo |
| `GET /api/albums_overview` | album-centric cross-session aggregation (`retried`, `on_disk`, stats) |
| `GET /api/files?refresh=1` | disk index of `DOWNLOADS_DIR` (images only, `logs/` skipped) |
| `GET /api/file?path=REL` | serve one image (traversal-safe) |
| `GET /api/exif?path=REL` | EXIF DateTimeOriginal vs mtime → `synced` / `mismatch` / `no_exif` + filename date |
| `GET /api/export` | JSON attachment: tree + albums overview + files |
| `GET /api/status` | existence / size / mtime of the log file + `timezone` (DISPLAY_TZ) |
| `GET /api/raw` | raw log file (download) |

## Notes

- With `mode: "a"` and no rotation, `fb-dl.log` grows forever. Browsing stays chunked;
  the one-pass scans are capped via `SCAN_MAX_BYTES` (streamed, bounded memory), and the
  parsed tree keeps at most `TREE_MAX_PHOTOS` photo nodes in RAM. Log rotation (rename
  **and** copytruncate) is detected live in the SSE stream.
- **Power outage / restart**: set `restart: unless-stopped` on `fb-dl` so it resumes
  by itself after a reboot — with `-I albums.txt` it skips the albums already marked
  `#` and continues where it stopped. Each restart shows up as a new session branch
  in the Tree tab (and the `retried` badge on albums seen in several sessions).
- If you change `output.logfile.path` in `config/gallery-dl.conf`, update `LOG_FILE`.
- `fb-dl` runs as `1000:1000`; the webui container uses the same uid, so it can read
  the log and the downloads without issues.
- Log timestamps are written in UTC. Set `DISPLAY_TZ` (e.g. `Europe/Paris`) in the
  webui service environment to display every timestamp (log lines, tree, chips, files
  mtime) in that timezone; raw API/JSON-export values stay UTC.

## Development / tests

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
python -m unittest test_tailer test_api -v
LOG_FILE=/tmp/fb-dl.log ALBUMS_FILE=/tmp/albums.txt DOWNLOADS_DIR=/tmp/dl uvicorn app:app --port 8081
```

CI (`.github/workflows/ci.yml`) runs those tests, builds the image from the repo root
and validates `compose.example.yml` (no `ports`, `build: ./web-dashboard`, external
caddy network, albums.txt-only config mount, non-root user, `no-new-privileges`).
