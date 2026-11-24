# web-dashboard — live log viewer for gallery-dl (fb-dl)

The UI is a single self-contained page (no external CDN — it must work on a private
Docker network), styled like classic Bootstrap (light theme, cards, badges, pill chips,
buttons) for readability and contrast. Every stat segment has a tooltip explaining its
source, and the browser back/forward buttons navigate tabs and `#album=<set_id>` /
`#file=<path>` permalinks (History API). Dates render `DD/MM/YY HH:MM:SS` in `DISPLAY_TZ`.

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
- **Opens scrolled at the END of the file** (live tail). Older history loads with the
  "⏒ Load earlier" button (`/api/logs?before=OFFSET&limit=N`, backwards chunks);
  the whole file from byte 0 remains available via `/api/logs?offset=0&limit=N`
  (`/api/logs?tail=N` for the last N lines). Live follow via SSE (`/api/stream`,
  green dot = connected). DOM capped at `MAX_RENDER_LINES`.
- Search over the whole scanned file (count + matches), level filters, full-text
  filter, pause, auto-scroll, raw log download (`/api/raw`).

### Status banner (fixed 3-line panel under the header, refreshed every 5 s via textContent - no flicker)
- **Line 1**: files on disk / photos detected in logs / files downloaded (CDN payloads) /
  albums: *unfinished* (started, not `#`-marked) - *remaining* (in albums.txt, never
  started) - total (done).
- **Line 2**: current sleep + reason + wake-up time / current album (click opens it in
  the Albums view via `#album=<set_id>`).
- **Line 3**: errors / rejections / last activity / log size, lines, display timezone.

### Tree - session -> album -> photo, 3 lines per node
The log is parsed into a tree. **Each run = one session branch** (a new branch starts
at every `[gallery-dl][debug] Version ...` line, so restarts/power cuts are visible).
Only the most recent session/album auto-expands. The tree **live-refreshes** (expanded
nodes and scroll position are preserved) and has a **filter box** (album name / set id /
fbid). Each node (session / album / photo) renders 3 lines:
- **Line 1**: icon + label (Session #N / album name + `a.<id>` / photo fbid) + a
  **copy-ID button**, start -> end, **duration**, status badge.
- **Line 2**: the 5 counters **Full / Interrupted / Missing / Already / Foreign**
  (counts for a session/album, the state badge for a photo) + sleep + downloaded size +
  FB browser link + `#album=<set_id>` link (album) **XOR** `#file=<path>` link (photo,
  opens the Files tab filtered on it) + a **logs** button (raw log span of that node:
  `/api/session/{s}/logs`, `/api/album/{s}/{a}/logs`, `/api/photo/{s}/{a}/{p}/logs`).
- **Line 3**: photo -> filename, name-date, mtime, EXIF time + status chip; album ->
  most recent mtime + EXIF status counts (background precompute, see below).
- **Expanding an album also expands its photos** (paginated by `PHOTOS_PAGE_SIZE=500`
  with "load more", so huge albums stay fast); each photo shows a **40 px thumbnail**.
- Album status: done per `albums.txt` `#` marker / banned / error / interrupted, with
  the end reason as tooltip. Photo FB browser link:
  `https://www.facebook.com/photo/?fbid=<id>&set=a.<set_id>`.

### Albums - 3-way comparison + cross-session view
Top table compares **albums.txt x logs x disk** per album: in albums.txt (done `#` /
pending / -), started in logs, files on disk, status - with a counts line separating
**unfinished** (started, not done) from **remaining** (in albums.txt, never started)
and **not_in_file** (seen in logs/on disk but absent from albums.txt). Below, one card
per album (keyed by set id, not chronological) listing **each session** that touched
it - expandable to show **that session's photos** (paginated, 3-line rows, per-photo
logs) - plus a **retried** badge when an album appears in more than one session (e.g.
after a crash or an anti-bot ban), the 5 state counters, totals, errors/rejections.
Deep-linkable with `#album=<set_id>` anchors; filter box on top.

### Files - disk view
Read-only index of `DOWNLOADS_DIR` (the `logs/` dir is skipped). Expected layout
`downloads/facebook/<user>/<album title> (a.<set_id>)/YYYYMMDD_HHMMSS_<fbid>.<ext>` -
the album set id **and title** come from the `(a. ...)` directory, the photo fbid and the
capture date from the filename. Lightweight server-side thumbnails
(`/api/thumb?path=...&w=120`, so no full-HD download per row), filters by album /
name / fbid / **log state** (full / already / foreign) / **EXIF state**, per-row EXIF
button, rescan button. A `#file=<path>` link (from the tree) opens this tab prefilled
and scrolls/highlights the row.

**EXIF check** (`/api/exif`): compares EXIF `DateTimeOriginal` with the file mtime
(±2 s): `synced` / `mismatch` / **`no_exif` — meaning your external EXIF/mtime script
did not run on that file** — plus the date parsed from the filename.

### Scraping rejection detection
Rejection-pattern lines are counted and shown with a red banner — **click it to jump
to the first rejection line in the Log tab** (rejection-only filter enabled). Patterns based on gallery-dl 1.32.21's Facebook extractor:
`You've been temporarily blocked from viewing images.`, `AuthRequired` /
`AuthorizationError` / `AuthenticationError` / `Invalid login credentials` /
`cookies needed`, `ChallengeError` / captcha names, `HttpError` 401/403/429,
`429 Too Many Requests`, `This content isn't available`. Paste any undetected
rejection line to add its pattern. (`[429/500]` progress lines deliberately do NOT
match.)

Log line format handled (verified against gallery-dl 1.32.21 output):
`[2026-11-22 18:40:12] [facebook][debug] …` and the default `[facebook][debug] …`;
legacy non-interpolated lines (`[%(asctime)s][%(levelname)s] …`) are shown as "other".

**Photos vs files vs on-disk** (each number in the UI has a tooltip saying which source
it comes from):
- `photos` = `"GET /photo/?fbid=…` lines in the log = photo pages visited (log);
- `downloaded`/`files` = media payloads actually downloaded in this run — HTTP `< 400`
  with a byte count from the CDN `fbcdn.net` (log). Do NOT count `Adjusting filename
  extension` lines for this: they only appear on avif→jpg and undercount when the CDN
  serves `.jpg` directly;
- `on disk` = files physically present under `DOWNLOADS_DIR`, matched by fbid (disk).
They differ when a page was seen but not downloaded (failed, anti-bot, already-on-disk
skip) or when the file was already there from a previous run.

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
| `DISPLAY_TZ` | `TZ` env, else `UTC` | IANA timezone used to **display** the log timestamps (logs are UTC) **and to interpret EXIF `DateTimeOriginal`** (EXIF has no timezone; `OffsetTimeOriginal` wins if present). UI renders via `Intl`; exposed by `/api/status` as `timezone` |
| `EXIF_TOLERANCE_S` | `2` | `|mtime − EXIF|` tolerance in seconds for the `synced` state |
| `EXIF_PRECOMPUTE` | `1` | background EXIF scan of the disk index feeding `/api/exif_stats` and the per-album EXIF counts on tree line 3 (`0` disables the thread) |
| `EXIF_SCAN_BATCH` | `500` | max new files EXIF-read per precompute pass (progressive: big libraries fill in over successive passes) |
| `EXIF_SCAN_INTERVAL` | `10` | seconds between precompute passes |
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
| `GET /api/albums` | 3-way comparison source: one entry per album with `in_file` / `done` / `in_logs` / `on_disk` / `set_id` / `link` / `name`; albums seen in logs or on disk but absent from albums.txt are appended (`status=not_in_file`); `counts`: done / incomplete (unfinished) / not_started (remaining) / not_in_file / total; `current` |
| `GET /api/summary` | fixed 3-line banner payload: files on disk, photos detected, downloaded, global 5-state counters, albums (done/unfinished/remaining/total), last sleep + `wake_epoch`, current album (+set/name/link), errors/warnings/rejections, `last_activity`, log size/lines, `timezone` |
| `GET /api/exif_stats` | aggregated EXIF precompute results: `global` and `by_album` per-state counts (synced/mismatch/no_exif) + `scanned`/`total`/`done` progress |
| `GET /api/stats` | counters incl. `files`, `current_album`, `current_album_set`, `current_album_files`, `last_sleep`, `last_activity`, `rejections`, `truncated` |
| `GET /api/tree` | parsed session → album → photo tree (`runs`, `done_urls`), per node: `states` (5-state counters incl. `foreign`), `on_disk`, `max_mtime`, per-photo `state`/`dl`/`path`/`mtime`/`name_date`, album `name`/`missing_on_disk` |
| `GET /api/album/{s}/{a}?offset=O&limit=N` | one album + its photos (paginated, merged with the disk index: per-photo `on_disk`/`path`/`link_photo`, plus album-level `on_disk` and `missing_on_disk` = fbids downloaded in the log but absent on disk) |
| `GET /api/session/{s}/logs` | raw log lines of that session (paged like album/photo spans) |
| `GET /api/album/{s}/{a}/logs` | raw log lines of that album |
| `GET /api/photo/{s}/{a}/{p}/logs` | raw log lines of that photo |
| `GET /api/albums_overview` | album-centric cross-session aggregation: `name`, `session_details` (per-session stats so the UI can show per-session photos), `retried`, `on_disk`, `missing_on_disk`, `extra_on_disk`, totals |
| `GET /api/files?refresh=1` | disk index of `DOWNLOADS_DIR` (images only, `logs/` skipped), each file annotated `in_log` (CDN payload seen in the log) + `state` (full/already/foreign) + `in_log_count` |
| `GET /api/file?path=REL` | serve one image, full size (traversal-safe) |
| `GET /api/thumb?path=REL&w=160` | server-side resized JPEG thumbnail (Pillow, cached) — used by the Files tab so it does not download full-HD images; same traversal rules |
| `GET /api/exif?path=REL` | EXIF DateTimeOriginal (Exif sub-IFD, `OffsetTimeOriginal` if present, else `DISPLAY_TZ`) vs mtime → `synced` / `mismatch` / `no_exif` + filename date + `exif_epoch` |
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
  mtime) in that timezone, formatted `DD/MM/YY HH:MM:SS` (fr-FR); raw API/JSON-export
  values stay UTC.
- **EXIF check**: `DateTimeOriginal` carries no timezone, so it is interpreted in
  `DISPLAY_TZ` (or the file's `OffsetTimeOriginal` when present) — this matches a
  script that syncs `mtime <- EXIF` in local time. Reading it as UTC made every file
  show `mismatch` with a constant tz-offset error. `no_exif` still means the script
  did not run on that file.
- **Log-vs-disk comparison** (the primary per-photo check): every photo parsed as
  `downloaded` from the logs is cross-referenced with the disk index by fbid —
  `on_disk` per photo/album, `missing_on_disk` (in logs, not on disk), `extra_on_disk`
  (on disk, not in logs), and `in_log` per file in the Files tab (with a
  "not in logs" filter).
- **Album names**: taken from the download directory title gallery-dl itself created
  (`…/<title> (a.<id>)`) — i.e. the name as downloaded, nothing to maintain.
  Names show in the Tree and Albums tabs.
- **Per-photo state** (crash-safe; log signal x disk presence, matched by fbid), shown as
  a badge on every photo row and aggregated per album/session (`states`):
  `full` = on disk **AND** download payload seen in logs (fbcdn.net, HTTP < 400 with
  bytes) - `already` = on disk, page seen in logs, but no payload (skipped, e.g. from a
  previous run) - `missing` = CDN payload seen in logs but the file is NOT on disk -
  `interrupted` = page seen, no completed payload, not on disk (crash mid-write, failed
  download, ban) - `foreign` = on disk but never seen in any log session. The
  `Adjusting filename extension` line is deliberately **not** used as proof of download
  (it only appears when the extension changes, e.g. avif->jpg): adjusting seen but not
  on disk is still `interrupted`.
- A photo seen in the log but without a download payload keeps `pending`/`failed` as its
  raw parsed status, and gets the explicit state `already` / `interrupted` from the rules
  above once the disk index is crossed.
- The Tree tab auto-expands only the **most recent** session and album; everything
  else stays collapsed. Tree and Albums live-refresh every 5 s while open, preserving
  expanded nodes and scroll. Browser back/forward works across tabs and
  `#album=<set_id>` / `#file=<path>` permalinks (History API).

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
