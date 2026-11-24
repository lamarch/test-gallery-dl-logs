import asyncio
import io
import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tailer import Follower, read_range, read_range_back, read_tail

LOG_FILE = os.environ.get("LOG_FILE", "/downloads/logs/fb-dl.log")
ALBUMS_FILE = os.environ.get("ALBUMS_FILE", "/config/albums.txt")
DOWNLOADS_DIR = os.environ.get("DOWNLOADS_DIR", "/downloads")
DEFAULT_TAIL = int(os.environ.get("TAIL_LINES", "500"))
CHUNK_LINES = int(os.environ.get("CHUNK_LINES", "5000"))
MAX_RENDER = int(os.environ.get("MAX_RENDER_LINES", "20000"))
TAIL_WINDOW_BYTES = int(os.environ.get("TAIL_WINDOW_BYTES", str(4 * 1024 * 1024)))
# With SCAN_MAX_BYTES > 0, only the tail of the log file is scanned for
# stats/search/tree parsing (0 = scan the whole file).
SCAN_MAX_BYTES = int(os.environ.get("SCAN_MAX_BYTES", "0"))
PHOTOS_PAGE_SIZE = int(os.environ.get("PHOTOS_PAGE_SIZE", "500"))
# Max photo nodes materialized in the parsed tree (RAM guard); <= 0 = unlimited.
# Beyond the cap, photos are still counted and /api/album/{s}/{a} re-parses the
# album's log span on demand.
TREE_MAX_PHOTOS = int(os.environ.get("TREE_MAX_PHOTOS", "50000"))
# Log timestamps are UTC; the UI displays them in this IANA timezone
# (set via compose env, e.g. DISPLAY_TZ=Europe/Paris). Exposed by /api/status.
# EXIF DateTimeOriginal has no timezone: it is interpreted in DISPLAY_TZ
# (unless the file carries OffsetTimeOriginal), matching what an external
# script that syncs mtime <- EXIF in local time would produce.
DISPLAY_TZ = os.environ.get("DISPLAY_TZ") or os.environ.get("TZ") or "UTC"
# |mtime - exif| tolerance (seconds) for the exif "synced" state.
EXIF_TOLERANCE_S = float(os.environ.get("EXIF_TOLERANCE_S", "2"))
# Disk index (/api/files) cache TTL in seconds; 0 = rescan on every call.
FILES_CACHE_TTL = float(os.environ.get("FILES_CACHE_TTL", "30"))
# Max files indexed by scan_downloads (walk stops early, "truncated" reported).
FILES_MAX = int(os.environ.get("FILES_MAX", "50000"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# gallery-dl log line formats:
#   default        : "[{name}][{levelname}] {message}"
#   with {asctime} : "[{asctime}] [{name}][{levelname}] {message}"
LEVEL_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\[[^\]]*\]\[(debug|info|warning|error|critical)\]")
TS_RE = re.compile(r"^\[([^\]]+)\]")
PROGRESS_RE = re.compile(r"^\[\d+/\d+\]")
STARTED_RE = re.compile(r"Starting \w+Job for '(.+)'")  # greedy: URLs may contain '
SLEEP_RE = re.compile(r"Sleeping ([\d.]+) seconds \(([^)]+)\)")
WAIT_RE = re.compile(
    r"Waiting for (?:(\d+(?:\.\d+)?) (seconds|minutes|hours)|(\d+)h(?: *(\d+)min)?)"
    r" until (\d{2}:\d{2}:\d{2})(?: \(([^)]+)\))?")
DOWNLOAD_RE = re.compile(r"Adjusting filename extension of '([^']+)'")
FAILED_RE = re.compile(r"Failed to download ['\"]?([^'\"]+)['\"]?\s*$")
VERSION_RE = re.compile(r"\[gallery-dl\]\[debug\] Version ")
PHOTO_GET_RE = re.compile(r'"GET /photo/\?fbid=(\d+)')
RESP_RE = re.compile(r'"(?:GET|POST) [^"]+ HTTP/1\.1" (\d{3})(?: (\d+))?')
FN_ID_RE = re.compile(r"_(\d{6,})\.[A-Za-z0-9]+$")
FN_DATE_RE = re.compile(r"(\d{8})_(\d{6})_")
SET_RE = re.compile(r"set=a\.([0-9]+)")
VANITY_RE = re.compile(r"vanity=([0-9]+)")
DIR_SET_RE = re.compile(r"\((a\.[0-9]+)\)")

# Anti-bot / scraping rejection patterns (gallery-dl 1.32.21 facebook extractor).
# NOTE: no bare \b429\b — it would match progress lines like "[429/500]".
# The pattern body is also served to the UI (see /api/status and /api/summary)
# so the Python and JS rejection regexes cannot drift apart.
REJECTION_PATTERN = (
    r"temporarily blocked"
    r"|blocked from viewing"
    r"|must be logged in|logged in to continue"
    r"|AuthRequired|AuthorizationError|AuthenticationError"
    r"|Invalid login credentials|credentials required|cookies needed"
    r"|Too Many Requests"
    r"|ChallengeError|[Cc]aptcha|FunCaptcha|GeeTest|hCaptcha"
    r"|HttpError: '(?:401|403|429)"
    r"|content isn't available"
    r"|photo download URL for .+\. Skipping"  # url extraction blocked permanently
)
REJECTION_RE = re.compile(r"(?i)" + REJECTION_PATTERN)

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "mp4", "mov", "webm"}

app = FastAPI(title="gallery-dl dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------- log reading

@app.get("/api/logs")
def get_logs(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=CHUNK_LINES, ge=1, le=100000),
    tail: int = Query(default=0, ge=0, le=100000, description="if > 0: last N lines"),
    before: int = Query(default=0, ge=0, description="if > 0: N lines ending at this byte offset"),
):
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        size = 0
    base = {"path": LOG_FILE, "size": size, "max_render": MAX_RENDER}
    if before > 0:
        lines, start_off, has_more = read_range_back(LOG_FILE, before, limit, TAIL_WINDOW_BYTES)
        return {**base, "lines": lines, "offset": start_off, "start_offset": start_off,
                "has_more": has_more, "eof": False}
    if tail > 0:
        lines, end_off, start_off = read_tail(LOG_FILE, tail, TAIL_WINDOW_BYTES)
        return {**base, "lines": lines, "offset": end_off, "start_offset": start_off,
                "has_more": start_off > 0, "eof": True}
    lines, off, eof = read_range(LOG_FILE, offset, limit)
    return {**base, "lines": lines, "offset": off, "start_offset": off,
            "has_more": not eof, "eof": eof}


@app.get("/api/stream")
async def stream(since: int = 0):
    follower = Follower(LOG_FILE, since)

    async def events():
        while True:
            for line in follower.poll():
                payload = json.dumps({"line": line, "offset": follower.pos})
                yield f"event: log\ndata: {payload}\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _scan_range():
    """Return (start_byte, size) of the log region scanned for stats/search/tree."""
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        return 0, 0
    if SCAN_MAX_BYTES > 0 and size > SCAN_MAX_BYTES:
        return size - SCAN_MAX_BYTES, size
    return 0, size


def _iter_log_entries(chunk=1024 * 1024, start_byte=None, end_byte=None):
    """Yield (start_byte, end_byte, line) for each complete line of the scanned
    region (or of [start_byte, end_byte) when given), streaming in `chunk`-byte
    reads (bounded memory on huge files)."""
    if start_byte is None:
        start, size = _scan_range()
    else:
        start = start_byte
        try:
            size = os.path.getsize(LOG_FILE)
        except OSError:
            return
    if end_byte is not None:
        size = min(size, end_byte)
    if size <= start:
        return
    # when start_byte is given explicitly it is a line boundary -> keep the
    # first line; only the scanned-region start may begin mid-line
    skip_first_partial = start_byte is None and start > 0
    pending = b""
    pos = start
    with open(LOG_FILE, "rb") as f:
        f.seek(start)
        while pos < size:
            buf = f.read(min(chunk, size - pos))
            if not buf:
                break
            parts = (pending + buf).split(b"\n")
            pending = parts.pop()
            for b in parts:
                if skip_first_partial:
                    skip_first_partial = False
                else:
                    yield pos, pos + len(b) + 1, b.decode("utf-8", "replace")
                pos += len(b) + 1
    if pending and not skip_first_partial and pos < size + 1:
        yield pos, pos + len(pending), pending.decode("utf-8", "replace")


def _iter_log_lines():
    for _, _, line in _iter_log_entries():
        yield line


# ---------------------------------------------------------------- search/stats

_search_cache = {"key": None, "value": None}


@app.get("/api/search")
def search(q: str = "", limit: int = Query(default=200, ge=1, le=2000)):
    """Search the scanned region of the log file."""
    ql = q.strip().lower()
    empty = {"q": q, "count": 0, "shown": 0, "lines": [],
             "scanned_bytes": 0, "truncated": False}
    if not ql:
        return empty
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns, ql)
    except OSError:
        return empty
    if _search_cache["key"] == key:
        return _search_cache["value"]
    start, size = _scan_range()
    count = 0
    matches = []
    for line in _iter_log_lines():
        if ql in line.lower():
            count += 1
            if len(matches) < limit:
                matches.append(line)
    value = {"q": q, "count": count, "shown": len(matches), "lines": matches,
             "scanned_bytes": size - start, "truncated": start > 0}
    _search_cache["key"] = key
    _search_cache["value"] = value
    return value


# ---------------------------------------------------------------- albums file

def _read_albums_file(path):
    """Parse a gallery-dl -I input file. Return [(url, done)] or None if missing.

    gallery-dl comments out ('# ' prefix) URLs that were processed successfully."""
    albums = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                done = line.startswith("#")
                url = line.lstrip("#").strip()
                url = re.sub(r"\s+#.*", "", url).strip()
                if url:
                    albums.append((url, done))
    except OSError:
        return None
    return albums


def _dir_names(files=None):
    """Fallback album names from download dir names: '.../<title> (a.<id>)'."""
    out = {}
    if files is None:
        files = scan_downloads()["files"]
    for f in files:
        s = f.get("album_set")
        if not s or s in out:
            continue
        base = os.path.basename(os.path.dirname(f["path"]))
        m = re.match(r"^(.*?)\s*\(a\.[0-9]+\)$", base)
        if m and m.group(1) and not m.group(1).isdigit():
            out[s] = m.group(1)
    return out


def _album_display_name(url, set_id, dirnames):
    # the download dir title gallery-dl itself created (".../<title> (a.<id>)")
    # — i.e. the album name as downloaded
    if set_id and dirnames.get("a." + set_id):
        return dirnames["a." + set_id]
    return None


def _started_urls():
    """URLs for which the scanned log region contains 'Starting ...Job for <url>'.
    The tree parser already collected them as album nodes."""
    t, _ = get_tree()
    return {a["url"] for s in t["sessions"] for a in s["albums"]}


@app.get("/api/albums")
def albums():
    """3-way comparison source for the Albums view: albums.txt x logs x disk.

    Each entry carries: url, set_id, name, link, status (done / incomplete /
    not_started / not_in_file), in_file, done ('#' in albums.txt), in_logs
    (a 'Starting ...Job' line was seen), on_disk (files indexed under the
    album's dir). Albums seen in the logs or on disk but absent from
    albums.txt are appended with in_file=False, status="not_in_file".
    "incomplete" = started in logs but not marked done (unfinished);
    "not_started" = in albums.txt but never started (remaining)."""
    parsed = _read_albums_file(ALBUMS_FILE)
    empty = {"done": 0, "incomplete": 0, "not_started": 0, "not_in_file": 0, "total": 0}
    if parsed is None:
        return {"available": False, "path": ALBUMS_FILE, "albums": [],
                "counts": empty, "current": None}
    started = _started_urls()
    files = scan_downloads()["files"]
    dirnames = _dir_names(files)
    disk_counts = Counter(f.get("album_set") for f in files if f.get("album_set"))
    disk_mtimes = {}
    for f in files:
        if f.get("album_set"):
            k = f["album_set"]
            disk_mtimes[k] = max(disk_mtimes.get(k, 0), f.get("mtime") or 0)
    t, _ = get_tree()
    log_albums = {}
    for s in t["sessions"]:
        for a in s["albums"]:
            key = "a." + a["set_id"] if a.get("set_id") else a["url"]
            log_albums.setdefault(key, []).append(a)

    def _timing(key):
        """(total_duration_s, last_activity_epoch) for one album key: sum of
        its log spans' durations, and the newest of log-end / disk-mtime."""
        nodes = log_albums.get(key) or []
        spans = [(_ts_epoch(a["start"]), _ts_epoch(a["end"])) for a in nodes]
        total = sum(e - s for s, e in spans if s is not None and e is not None)
        ends = [e for _, e in spans if e]
        # log activity only: disk mtimes are EXIF-synced (photo shoot date),
        # not a real "last activity"
        return total, (max(ends) if ends else None)
    out = []
    counts = {"done": 0, "incomplete": 0, "not_started": 0, "not_in_file": 0}
    current = None
    seen = set()
    for url, done in parsed:
        set_id, _ = _parse_url_ids(url)
        key = "a." + set_id if set_id else url
        seen.add(key)
        in_logs = url in started
        if done:
            status = "done"
        elif in_logs:
            status = "incomplete"
        else:
            status = "not_started"
        if not done and current is None:
            current = url
        counts[status] += 1
        duration, last = _timing(key)
        out.append({"url": url, "set_id": set_id, "link": _album_link(url, set_id),
                    "status": status, "in_file": True, "done": done, "in_logs": in_logs,
                    "on_disk": disk_counts.get(key, 0),
                    "total_duration_s": duration,
                    "last_activity": last,
                    "name": _album_display_name(url, set_id, dirnames)})
    for key in sorted(set(log_albums) | set(disk_counts)):
        if key in seen:
            continue
        nodes = log_albums.get(key) or []
        a = nodes[0] if nodes else None
        set_id = a["set_id"] if a else (key[2:] if key.startswith("a.") else None)
        counts["not_in_file"] += 1
        duration, last = _timing(key)
        out.append({"url": a["url"] if a else None, "set_id": set_id,
                    "link": a["link"] if a else None,
                    "status": "not_in_file", "in_file": False, "done": False,
                    "in_logs": a is not None, "on_disk": disk_counts.get(key, 0),
                    "total_duration_s": duration,
                    "last_activity": last,
                    "name": _album_display_name(a["url"], a["set_id"], dirnames) if a
                            else dirnames.get(key)})
    counts["total"] = len(out)
    return {"available": True, "path": ALBUMS_FILE, "albums": out,
            "counts": counts, "current": current}


# ---------------------------------------------------------------- tree parser

def _ts_of(line):
    m = TS_RE.match(line)
    return m.group(1) if m else ""


def _ts_epoch(ts):
    """Epoch of a log timestamp string (logs are UTC); None if unparsable."""
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _parse_url_ids(url):
    m = SET_RE.search(url)
    set_id = m.group(1) if m else None
    m = VANITY_RE.search(url)
    vanity = m.group(1) if m else None
    return set_id, vanity


def _album_link(url, set_id):
    if set_id:
        return f"https://www.facebook.com/media/set/?set=a.{set_id}&type=3"
    return url


def _wait_secs(m):
    "Seconds from a WAIT_RE match (single unit or the combined '2h 5min')."
    if m.group(1):
        return float(m.group(1)) * {"seconds": 1, "minutes": 60, "hours": 3600}[m.group(2)]
    return float(m.group(3)) * 3600 + float(m.group(4) or 0) * 60


def _update_photo_node(p, line):
    """Match one log line against an in-progress photo node `p` (may be None:
    the caller then only gets the accounting return values). Updates p's
    payload / filename / failure / sleep fields and returns (payload_bytes,
    is_media, sleep_secs, sleep_reason) so callers can do their album/session
    accounting from the same match without re-running the regexes."""
    payload = is_media = 0
    secs = 0.0
    reason = None
    rm = RESP_RE.search(line)
    if rm and int(rm.group(1)) < 400 and rm.group(2):
        payload = int(rm.group(2))
        is_media = "fbcdn.net" in line
        if p is not None:
            p["bytes"] += payload
            if is_media:
                p["status"] = "downloaded"
                p["dl"] = True
    dm = DOWNLOAD_RE.search(line)
    if dm and p is not None:
        # carries the final filename only; NOT proof of download (it only
        # appears when the extension changes) -> no dl flag here
        p["file"] = dm.group(1)
        fm = FN_ID_RE.search(dm.group(1))
        if fm and not p["fbid"]:
            p["fbid"] = fm.group(1)
    fm = FAILED_RE.search(line)
    if fm and p is not None:
        p["status"] = "failed"
        fid = FN_ID_RE.search(fm.group(1))
        if fid and not p["fbid"]:
            p["fbid"] = fid.group(1)
    sm = SLEEP_RE.search(line)
    if sm:
        secs = float(sm.group(1))
        reason = sm.group(2)
        if p is not None and reason == "download":
            p["sleep"] += secs
    wm = WAIT_RE.search(line)
    if wm:
        secs = _wait_secs(wm)
        reason = wm.group(6) or "waiting"
        if p is not None:
            p["sleep"] += secs
    return payload, is_media, secs, reason


def _photo_link(fbid, set_id):
    if fbid and set_id:
        return f"https://www.facebook.com/photo/?fbid={fbid}&set=a.{set_id}"
    if fbid:
        return f"https://www.facebook.com/photo/?fbid={fbid}"
    return None


def _album_status(a, done_urls):
    if a["url"] in done_urls:
        return "done", "completed (marked '#' in albums.txt)"
    if a["rejections"]:
        return "banned", "scraping rejection (anti-bot)"
    if a["errors"]:
        return "error", "error(s) in log"
    return "incomplete", "session end / interrupted"


def parse_log():
    """Parse the scanned log region into a session -> album -> photo tree.

    Return (tree, spans): spans maps ("s", si) / ("s", si, ai) / ("s", si, ai, pi)
    to (start_byte, end_byte) ranges in the log file."""
    parsed_albums = _read_albums_file(ALBUMS_FILE) or []
    done_urls = {u for u, d in parsed_albums if d}
    dirnames = _dir_names()

    sessions = []
    spans = {}
    stored_photos = [0]  # RAM guard: cap on materialized photo nodes
    si = ai = pi = -1
    cur_s = cur_a = cur_p = None

    def close_photo(end_off, ts):
        nonlocal cur_p
        if cur_p is not None:
            spans[("s", si, ai, pi)] = (cur_p["start_off"], end_off)
            cur_p = None

    def close_album(end_off, ts):
        nonlocal cur_a, ai
        close_photo(end_off, ts)
        if cur_a is not None:
            cur_a["end"] = ts
            cur_a["photos_n"] = cur_a["photos_seen"]
            cur_a["status"], cur_a["end_reason"] = _album_status(cur_a, done_urls)
            spans[("s", si, ai)] = (cur_a["start_off"], end_off)
            cur_a = None

    def close_session(end_off, ts):
        nonlocal cur_s, si
        close_album(end_off, ts)
        if cur_s is not None:
            cur_s["end"] = ts
            cur_s["photos_n"] = sum(a["photos_n"] for a in cur_s["albums"])
            spans[("s", si)] = (cur_s["start_off"], end_off)
            cur_s = None

    def new_session(start, ts):
        nonlocal cur_s, si
        si += 1
        cur_s = {"idx": si, "start": ts, "end": ts, "start_off": start, "end_off": start,
                 "albums": [], "photos_n": 0, "files": 0, "bytes": 0, "sleeps": 0.0,
                 "errors": 0, "rejections": 0}
        sessions.append(cur_s)

    prev_end, prev_ts = 0, ""
    for start, end, line in _iter_log_entries():
        ts = _ts_of(line)
        if VERSION_RE.search(line):
            close_session(prev_end, prev_ts)
            ai = pi = -1
            cur_a = cur_p = None
            new_session(start, ts)
        else:
            if cur_s is None:
                new_session(start, ts)
            m = STARTED_RE.search(line)
            if m:
                close_album(prev_end, prev_ts)
                ai += 1
                pi = -1
                cur_p = None
                url = m.group(1)
                set_id, vanity = _parse_url_ids(url)
                cur_a = {"idx": ai, "url": url, "set_id": set_id, "vanity": vanity,
                         "name": _album_display_name(url, set_id, dirnames),
                         "link": _album_link(url, set_id), "start": ts, "end": ts,
                         "start_off": start, "end_off": end, "photos": [], "photos_n": 0,
                         "photos_seen": 0, "photos_truncated": False,
                         "files": 0, "bytes": 0, "sleeps": 0.0, "errors": 0, "rejections": 0}
                cur_s["albums"].append(cur_a)
            pm = PHOTO_GET_RE.search(line)
            if pm and cur_a is not None:
                close_photo(prev_end, prev_ts)
                pi += 1
                cur_a["photos_seen"] += 1
                if TREE_MAX_PHOTOS <= 0 or stored_photos[0] < TREE_MAX_PHOTOS:
                    stored_photos[0] += 1
                    cur_p = {"idx": pi, "fbid": pm.group(1), "ts": ts,
                             "start_off": start, "end_off": end, "status": "pending",
                             "file": None, "sleep": 0.0, "bytes": 0, "dl": False}
                    cur_a["photos"].append(cur_p)
                else:
                    # cap hit: count the photo but don't materialize it;
                    # /api/album/{s}/{a} re-parses this album's span on demand
                    cur_p = None
                    cur_a["photos_truncated"] = True
            # one shared match for photo node + album/session accounting
            payload, media, secs, _reason = _update_photo_node(cur_p, line)
            if payload:
                if cur_a is not None:
                    cur_a["bytes"] += payload
                if cur_s is not None:
                    cur_s["bytes"] += payload
                # a media payload from the CDN = one file actually written.
                # ("Adjusting filename extension" only appears when the
                # extension CHANGES, e.g. avif->jpg, so it undercounts)
                if media:
                    if cur_a is not None:
                        cur_a["files"] += 1
                    if cur_s is not None:
                        cur_s["files"] += 1
            if secs:
                if cur_a is not None:
                    cur_a["sleeps"] += secs
                if cur_s is not None:
                    cur_s["sleeps"] += secs
            if REJECTION_RE.search(line):
                if cur_p is not None:
                    cur_p["rejected"] = True
                if cur_a is not None:
                    cur_a["rejections"] += 1
                if cur_s is not None:
                    cur_s["rejections"] += 1
            lm = LEVEL_RE.match(line)
            if lm and lm.group(1) in ("error", "critical"):
                if cur_a is not None:
                    cur_a["errors"] += 1
                if cur_s is not None:
                    cur_s["errors"] += 1
        if cur_p is not None:
            cur_p["end_off"] = end
        if cur_a is not None:
            cur_a["end"] = ts
            cur_a["end_off"] = end
        if cur_s is not None:
            cur_s["end"] = ts
            cur_s["end_off"] = end
        prev_end, prev_ts = end, ts
    close_session(prev_end, prev_ts)

    return {"sessions": sessions, "runs": len(sessions), "done_urls": sorted(done_urls)}, spans


_tree_cache = {"key": None, "value": None}


def get_tree():
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return {"sessions": [], "runs": 0, "done_urls": []}, {}
    if _tree_cache["key"] == key:
        return _tree_cache["value"]
    value = parse_log()
    _tree_cache.update(key=key, value=value)
    return value


def _photo_state(p, disk_fbids):
    """Per-photo state. Proof rules (crash-safe):
    - `dl` = a CDN media payload was seen in the logs (fbcdn.net, HTTP < 400 with
      bytes). The "Adjusting filename" line is NOT proof (it only appears when the
      extension changes) - adjusting seen but not on disk is still interrupted.
    - full        = on disk AND download seen in logs
    - already     = on disk, page seen in logs, but no download payload (skip)
    - missing     = download seen in logs (CDN payload) but NOT on disk
    - interrupted = page seen, no completed download payload, not on disk
                    (server crash mid-write, failed download, ban, ...)
    - foreign     = on disk but never seen in the logs (counted at album level)
    """
    if p.get("rejected"):
        return "rejected"
    on_disk = bool(p.get("fbid")) and p["fbid"] in disk_fbids
    dl = bool(p.get("dl"))
    if on_disk and dl:
        return "full"
    if on_disk:
        return "already"
    if dl:
        return "missing"
    return "interrupted"


def _annotate_disk(t, files=None):
    """Cross log photos with files actually on disk.

    Per photo: explicit 5-state (see _photo_state) + disk file info when present.
    Per album/session: `states` counters (foreign = on disk in this album's dir
    but never seen in ANY log session for this set) + on_disk + max_mtime."""
    if files is None:
        files = scan_downloads()["files"]
    by_fbid = {}
    disk_fbids_by_set = {}
    max_mtime_by_set = {}
    for f in files:
        if f.get("fbid"):
            by_fbid.setdefault(f["fbid"], f)
        if f.get("album_set"):
            k = f["album_set"]
            if f.get("fbid"):
                disk_fbids_by_set.setdefault(k, set()).add(f["fbid"])
            max_mtime_by_set[k] = max(max_mtime_by_set.get(k, 0), f.get("mtime") or 0)
    # union of photo fbids seen in logs, per album set, across ALL sessions
    # (a file seen in another session is not "foreign" for this album)
    log_fbids_by_set = {}
    sessions_by_set = {}
    dl_fbids_by_set = {}
    size_by_set = {}
    first_mtime_by_set = {}
    for f in files:
        if f.get("album_set"):
            k = f["album_set"]
            size_by_set[k] = size_by_set.get(k, 0) + f.get("size") or 0
            if f.get("fbid"):
                first_mtime_by_set[k] = min(first_mtime_by_set.get(k, float("inf")), f["mtime"])
    for s in t["sessions"]:
        seen_keys = set()
        for a in s["albums"]:
            key = "a." + a["set_id"] if a.get("set_id") else None
            if key:
                log_fbids_by_set.setdefault(key, set()).update(
                    p["fbid"] for p in a["photos"] if p.get("fbid"))
                dl_fbids_by_set.setdefault(key, set()).update(
                    p["fbid"] for p in a["photos"] if p.get("dl") and p.get("fbid"))
                if key not in seen_keys:  # this session touched this set once
                    seen_keys.add(key)
                    sessions_by_set[key] = sessions_by_set.get(key, 0) + 1
    for s in t["sessions"]:
        s["on_disk"] = 0
        s["states"] = {}
        s["max_mtime"] = 0
        s["rejections_unresolved"] = 0
        counted_foreign = set()
        counted_size = set()
        for a in s["albums"]:
            key = "a." + a["set_id"] if a.get("set_id") else None
            disk = disk_fbids_by_set.get(key, set()) if key else set()
            a["retried"] = bool(key) and sessions_by_set.get(key, 1) > 1
            a["on_disk"] = len(disk)
            s["on_disk"] += a["on_disk"]
            for p in a["photos"]:
                p["state"] = _photo_state(p, disk)
                f = by_fbid.get(p.get("fbid")) if p.get("fbid") else None
                if f:
                    p["path"] = f["path"]
                    p["size"] = f["size"]
                    p["mtime"] = f["mtime"]
                    p["name_date"] = f.get("name_date")
            states = Counter(p["state"] for p in a["photos"])
            # "downloaded" = CDN download payload seen in the logs (on-disk +
            # missing); photos already on disk from a previous run don't count
            states["downloaded"] = sum(1 for p in a["photos"] if p.get("dl"))
            if key and key not in counted_foreign:
                counted_foreign.add(key)
                # on disk in this album's dir, never seen in any log session
                states["foreign"] = len(disk - log_fbids_by_set.get(key, set()))
            a["states"] = dict(states)
            a["missing_on_disk"] = states.get("missing", 0)
            a["extra_on_disk"] = (len(disk - dl_fbids_by_set.get(key, set()))
                                 if key else 0)  # on disk, never downloaded in log
            a["on_disk_size"] = size_by_set.get(key, 0) if key else 0
            a["max_mtime"] = max_mtime_by_set.get(key, 0) if key else 0
            a["first_mtime"] = (first_mtime_by_set[key]
                                if key and key in first_mtime_by_set else None)
            unresolved = 0
            for p in a["photos"]:
                if not p.get("rejected"):
                    continue
                fb = p.get("fbid")
                resolved = p.get("dl") or (
                    fb and fb in disk or fb in dl_fbids_by_set.get(key, set()))
                if not resolved:
                    unresolved += 1
            a["rejections_unresolved"] = unresolved
            if a.get("status") == "banned" and unresolved == 0:
                # every rejected photo of this album was downloaded later (or is
                # on disk) -> the anti-bot ban no longer holds
                if a["errors"]:
                    a["status"], a["end_reason"] = "error", "error(s) in log"
                else:
                    a["status"], a["end_reason"] = (
                        "incomplete", "rejection(s) recovered by a later successful download")
            s["max_mtime"] = max(s["max_mtime"], a["max_mtime"])
            if key and key not in counted_size:
                counted_size.add(key)
                s["on_disk_size"] = s.get("on_disk_size", 0) + a["on_disk_size"]
                if a["first_mtime"]:
                    s["first_mtime"] = min(s.get("first_mtime") or float("inf"),
                                           a["first_mtime"])
                s["rejections_unresolved"] += a["rejections_unresolved"]
            for k, v in states.items():
                s["states"][k] = s["states"].get(k, 0) + v
    if t["sessions"] and t["sessions"][-1]["albums"]:
        # the album the scraper is (as far as the log shows) working on
        t["sessions"][-1]["albums"][-1]["running"] = True


@app.get("/api/tree")
def tree():
    t, _ = get_tree()
    _annotate_disk(t)
    return t


def _photos_from_album_span(spans, s, a):
    """Re-parse one album's log span into photo nodes (used when the tree's
    TREE_MAX_PHOTOS cap was hit). Also registers per-photo log spans."""
    span = spans.get(("s", s, a))
    if span is None:
        return None
    photos = []
    cur = None
    prev_end = span[0]
    for start, end, line in _iter_log_entries(start_byte=span[0], end_byte=span[1]):
        ts = _ts_of(line)
        pm = PHOTO_GET_RE.search(line)
        if pm:
            if cur is not None:
                cur["end_off"] = prev_end
                spans[("s", s, a, cur["idx"])] = (cur["start_off"], cur["end_off"])
            cur = {"idx": len(photos), "fbid": pm.group(1), "ts": ts,
                   "start_off": start, "end_off": end, "status": "pending",
                   "file": None, "sleep": 0.0, "bytes": 0, "dl": False}
            photos.append(cur)
        if REJECTION_RE.search(line) and cur is not None:
            cur["rejected"] = True
        _update_photo_node(cur, line)
        prev_end = end
    if cur is not None:
        cur["end_off"] = prev_end
        spans[("s", s, a, cur["idx"])] = (cur["start_off"], cur["end_off"])
    return photos


@app.get("/api/album/{s}/{a}")
def album_detail(s: int, a: int, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=PHOTOS_PAGE_SIZE, ge=1, le=5000)):
    t, spans = get_tree()
    try:
        alb = t["sessions"][s]["albums"][a]
    except (IndexError, KeyError):
        raise HTTPException(404, "album not found")
    files = scan_downloads()["files"]
    _annotate_disk(t, files)  # gives alb: on_disk / states / max_mtime / per-photo state
    photos_src = alb["photos"]
    if alb.get("photos_truncated") and alb["photos_n"] > len(photos_src):
        full = _photos_from_album_span(spans, s, a)
        if full is not None:
            photos_src = full
    by_fbid = {f["fbid"]: f for f in files if f.get("fbid")}
    disk_key = "a." + alb["set_id"] if alb.get("set_id") else None
    disk_fbids = {f["fbid"] for f in files
                  if f.get("fbid") and f.get("album_set") == disk_key} if disk_key else set()
    photos = []
    for p in photos_src[offset:offset + limit]:
        q = dict(p)
        f = by_fbid.get(p.get("fbid"))
        if f:
            q["on_disk"] = True
            q["path"] = f["path"]
            q["size"] = f["size"]
            q["mtime"] = f["mtime"]
            q["name_date"] = f.get("name_date")
        else:
            q["on_disk"] = False
        q["dl"] = bool(p.get("dl"))
        # explicit state from log signal + disk presence (no status rewriting)
        q["state"] = _photo_state(p, disk_fbids)
        q["link_photo"] = _photo_link(p.get("fbid"), alb.get("set_id"))
        photos.append(q)
    out = {k: v for k, v in alb.items() if k != "photos"}
    out["photos"] = photos
    # downloaded in logs (CDN payload) but not on disk -> explicit fbid list
    out["missing_on_disk"] = sorted(
        p["fbid"] for p in photos_src if _photo_state(p, disk_fbids) == "missing")
    return out


def _span_lines(spans, key, offset, limit):
    """Stream the byte span, counting lines and keeping only the requested
    window (bounded memory even for huge album spans)."""
    span = spans.get(key)
    if span is None:
        raise HTTPException(404, "not found")
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        return {"lines": [], "total": 0, "offset": offset}
    start = max(0, min(span[0], size))
    end = max(start, min(span[1], size))
    lines = []
    total = 0
    pending = b""
    remaining = end - start
    with open(LOG_FILE, "rb") as f:
        f.seek(start)
        while remaining > 0:
            buf = f.read(min(1024 * 1024, remaining))
            if not buf:
                break
            remaining -= len(buf)
            parts = (pending + buf).split(b"\n")
            pending = parts.pop()
            for b in parts:
                if offset <= total < offset + limit:
                    lines.append(b.decode("utf-8", "replace"))
                total += 1
    if pending:
        if offset <= total < offset + limit:
            lines.append(pending.decode("utf-8", "replace"))
        total += 1
    return {"lines": lines, "total": total, "offset": offset}


@app.get("/api/session/{s}/logs")
def session_logs(s: int, offset: int = Query(default=0, ge=0),
                 limit: int = Query(default=2000, ge=1, le=20000)):
    _, spans = get_tree()
    return _span_lines(spans, ("s", s), offset, limit)


@app.get("/api/album/{s}/{a}/logs")
def album_logs(s: int, a: int, offset: int = Query(default=0, ge=0),
               limit: int = Query(default=2000, ge=1, le=20000)):
    _, spans = get_tree()
    return _span_lines(spans, ("s", s, a), offset, limit)


@app.get("/api/photo/{s}/{a}/{p}/logs")
def photo_logs(s: int, a: int, p: int, offset: int = Query(default=0, ge=0),
               limit: int = Query(default=2000, ge=1, le=20000)):
    _, spans = get_tree()
    return _span_lines(spans, ("s", s, a, p), offset, limit)


@app.get("/api/albums_overview")
def albums_overview():
    """Album-centric view across all sessions (set_id keyed), with retry flag."""
    t, _ = get_tree()
    files = scan_downloads()["files"]
    _annotate_disk(t, files)
    by_set = {}
    for f in files:
        if f.get("album_set"):
            by_set.setdefault(f["album_set"], []).append(f)
    agg = {}
    for s in t["sessions"]:
        for a in s["albums"]:
            running = s is t["sessions"][-1] and a is s["albums"][-1]
            key = a["set_id"] or a["url"]
            e = agg.setdefault(key, {
                "key": key, "url": a["url"], "set_id": a["set_id"], "vanity": a["vanity"],
                "name": a.get("name"),
                "link": a["link"], "sessions": [], "session_details": [], "status": a["status"],
                "end_reason": a.get("end_reason"), "photos": 0, "files": 0, "bytes": 0,
                "sleeps": 0.0, "errors": 0, "rejections": 0, "_dl_fbids": set(),
                "_seen_fbids": set(), "states": {}, "rejections_unresolved": 0,
                "on_disk": len(by_set.get("a." + a["set_id"], [])) if a["set_id"] else 0,
                "on_disk_size": (sum(f["size"] for f in by_set.get("a." + a["set_id"], []))
                                 if a["set_id"] else 0),
                "first_mtime": (min([f["mtime"] for f in by_set.get("a." + a["set_id"], [])
                                     if f.get("mtime")], default=None) if a["set_id"] else None)})
            e["sessions"].append(s["idx"])
            e["_dl_fbids"].update(p["fbid"] for p in a["photos"]
                                  if p.get("dl") and p.get("fbid"))
            e["_seen_fbids"].update(p["fbid"] for p in a["photos"] if p.get("fbid"))
            for k, v in a.get("states", {}).items():
                if k == "foreign":
                    continue  # recomputed once per album set below
                e["states"][k] = e["states"].get(k, 0) + v
            e["running"] = e.get("running", False) or running
            e["session_details"].append({
                "session": s["idx"], "album_idx": a["idx"], "running": running,
                "session_start": s["start"], "start": a["start"], "end": a["end"],
                "photos_n": a["photos_n"], "files": a["files"], "bytes": a["bytes"],
                "states": a.get("states", {}),
                "sleeps": a["sleeps"], "errors": a["errors"], "rejections": a["rejections"],
                "rejections_unresolved": a.get("rejections_unresolved", 0),
                "status": a["status"], "end_reason": a.get("end_reason")})
            e["photos"] += a["photos_n"]
            e["files"] += a["files"]
            e["bytes"] += a["bytes"]
            e["sleeps"] += a["sleeps"]
            e["errors"] += a["errors"]
            e["rejections"] += a["rejections"]
    out = list(agg.values())
    done_set = set(t["done_urls"])
    for e in out:
        e["retried"] = len(set(e["sessions"])) > 1
        # recompute from the aggregated counters, not from the first session
        e["status"], e["end_reason"] = _album_status(e, done_set)
        e["rejections_unresolved"] = sum(sd.get("rejections_unresolved", 0)
                                        for sd in e["session_details"])
        if e["status"] == "banned" and e["rejections_unresolved"] == 0:
            # recovery-aware: all rejected photos were downloaded later
            if e["errors"]:
                e["status"], e["end_reason"] = "error", "error(s) in log"
            else:
                e["status"], e["end_reason"] = (
                    "incomplete", "rejection(s) recovered by a later successful download")
        # log-vs-disk comparison for this album set
        key = "a." + e["set_id"] if e.get("set_id") else None
        disk = {f["fbid"] for f in by_set.get(key, []) if f.get("fbid")} if key else set()
        e["missing_on_disk"] = len(e["_dl_fbids"] - disk)    # downloaded in log, not on disk
        e["extra_on_disk"] = len(disk - e["_dl_fbids"])      # on disk, never downloaded in log
        e["states"]["foreign"] = len(disk - e["_seen_fbids"])  # on disk, never seen in logs
        del e["_dl_fbids"], e["_seen_fbids"]
    out.sort(key=lambda e: (e["sessions"][0] if e["sessions"] else 0))
    return {"albums": out, "done_urls": t["done_urls"]}


# ---------------------------------------------------------------- disk index

_files_cache = {"value": None, "ts": 0.0}


def scan_downloads(refresh=False):
    """Index image files under DOWNLOADS_DIR (skipping the logs dir).

    Filenames look like 20250429_081104_29388560487456506.jpg and album dirs
    contain '(a.29388587357453819)' -> fbid / name_date / album_set.
    Cached for FILES_CACHE_TTL seconds (0 = rescan every call) so newly
    downloaded files show up as "on disk" without a manual refresh."""
    now = time.time()
    if not refresh and _files_cache["value"] is not None \
            and now - _files_cache["ts"] < FILES_CACHE_TTL:
        return _files_cache["value"]
    files = []
    truncated = False
    root = DOWNLOADS_DIR
    for dirpath, dirnames, filenames in os.walk(root):
        if len(files) >= FILES_MAX:
            truncated = True
            break
        dirnames[:] = [d for d in dirnames if d != "logs"]
        rel_dir = os.path.relpath(dirpath, root)
        m = DIR_SET_RE.search(rel_dir)
        album_set = m.group(1) if m else None
        for fn in filenames:
            ext = fn.rsplit(".", 1)[-1].lower() if "." in fn else ""
            if ext not in IMAGE_EXTS:
                continue
            p = os.path.join(dirpath, fn)
            try:
                st = os.stat(p)
            except OSError:
                continue
            fbid = None
            m = FN_ID_RE.search(fn)
            if m:
                fbid = m.group(1)
            name_date = None
            m = FN_DATE_RE.search(fn)
            if m:
                d, t = m.group(1), m.group(2)
                name_date = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
            files.append({"path": os.path.join(rel_dir, fn), "name": fn, "size": st.st_size,
                          "mtime": st.st_mtime, "fbid": fbid, "name_date": name_date,
                          "album_set": album_set})
            if len(files) >= FILES_MAX:
                truncated = True
                break
    value = {"root": root, "count": len(files), "truncated": truncated, "files": files}
    _files_cache["value"] = value
    _files_cache["ts"] = now
    return value


@app.get("/api/files")
def files_endpoint(refresh: int = 0):
    val = scan_downloads(refresh=bool(refresh))
    # annotate each file with its 5-state (the file IS on disk):
    #   full = CDN download seen in logs, already = page seen but skipped,
    #   foreign = never seen in logs. `in_log` = CDN payload seen (dl flag).
    t, _ = get_tree()
    dl_fbids = set()
    seen_fbids = set()
    for s in t["sessions"]:
        for a in s["albums"]:
            for p in a["photos"]:
                if p.get("fbid"):
                    seen_fbids.add(p["fbid"])
                    if p.get("dl"):
                        dl_fbids.add(p["fbid"])
    in_log = 0
    for f in val["files"]:
        fb = f.get("fbid")
        f["in_log"] = bool(fb) and fb in dl_fbids
        f["state"] = "full" if f["in_log"] else ("already" if fb in seen_fbids else "foreign")
        in_log += 1 if f["in_log"] else 0
    val["in_log_count"] = in_log
    return val


def _safe_path(rel):
    """Resolve `rel` under DOWNLOADS_DIR; only image files, no traversal."""
    root = os.path.realpath(DOWNLOADS_DIR)
    full = os.path.realpath(os.path.join(root, rel))
    if full != root and not full.startswith(root + os.sep):
        return None
    if not os.path.isfile(full):
        return None
    if full.rsplit(".", 1)[-1].lower() not in IMAGE_EXTS:
        return None
    return full


@app.get("/api/file")
def file_endpoint(path: str = ""):
    full = _safe_path(path)
    if not full:
        raise HTTPException(404)
    return FileResponse(full)


_thumb_cache = {}
THUMB_MAX_W = 640


@app.get("/api/thumb")
def thumb_endpoint(path: str = "", w: int = Query(default=160, ge=16, le=THUMB_MAX_W)):
    """Server-side resized JPEG thumbnail (the Files tab must not download
    full-HD images for every row). Same traversal rules as /api/file."""
    full = _safe_path(path)
    if not full:
        raise HTTPException(404)
    try:
        st = os.stat(full)
        key = (full, st.st_mtime_ns, w)
    except OSError:
        raise HTTPException(404)
    hit = _thumb_cache.get(key)
    if hit is not None:
        return Response(hit, media_type="image/jpeg")
    try:
        from PIL import Image
        with Image.open(full) as im:
            im.thumbnail((w, w))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=70)
            data = buf.getvalue()
    except Exception:
        raise HTTPException(422, "not a decodable image")
    if len(_thumb_cache) > 1000:
        _thumb_cache.clear()
    _thumb_cache[key] = data
    return Response(data, media_type="image/jpeg")


def _exif_epoch(dto, offset):
    """Epoch of an EXIF DateTimeOriginal. EXIF carries no timezone: use the
    file's OffsetTimeOriginal (tag 36881, e.g. '+02:00') if present, else
    DISPLAY_TZ (local time — what a script syncing mtime <- EXIF in local
    time produces), else UTC."""
    dt = datetime.strptime(dto, "%Y:%m:%d %H:%M:%S")
    if offset:
        m = re.match(r"([+-])(\d{2}):(\d{2})", str(offset))
        if m:
            delta = timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
            if m.group(1) == "-":
                delta = -delta
            return dt.replace(tzinfo=timezone(delta)).timestamp()
    try:
        return dt.replace(tzinfo=ZoneInfo(DISPLAY_TZ)).timestamp()
    except Exception:
        return dt.replace(tzinfo=timezone.utc).timestamp()


def _exif_info(full, rel):
    """EXIF DateTimeOriginal + fs mtime + filename date for one file.

    state: synced (|mtime - exif| <= EXIF_TOLERANCE_S) / mismatch / no_exif
    (script not run). EXIF time is interpreted via OffsetTimeOriginal if
    present, else in DISPLAY_TZ (not UTC - EXIF has no timezone)."""
    st = os.stat(full)
    dto = None
    off = None
    try:
        from PIL import Image
        with Image.open(full) as im:
            ex = im.getexif()
            # DateTimeOriginal lives in the Exif sub-IFD (0x8769) on real files,
            # but getexif() only merges IFD0 -> check both
            sub = ex.get_ifd(0x8769)
            dto = ex.get(36867) or sub.get(36867) or None
            off = sub.get(36881) or None  # OffsetTimeOriginal
    except Exception:
        dto = None
    name_date = None
    m = FN_DATE_RE.search(os.path.basename(full))
    if m:
        d, t = m.group(1), m.group(2)
        name_date = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
    state = "no_exif"
    diff_s = None
    exif_epoch = None
    if dto:
        try:
            exif_epoch = _exif_epoch(dto, off)
            diff_s = round(st.st_mtime - exif_epoch, 3)
            state = "synced" if abs(diff_s) <= EXIF_TOLERANCE_S else "mismatch"
        except ValueError:
            state = "no_exif"
    return {"path": rel, "mtime": st.st_mtime, "exif": dto, "exif_epoch": exif_epoch,
            "offset": off, "name_date": name_date, "state": state, "diff_s": diff_s,
            "tz": DISPLAY_TZ}


@app.get("/api/exif")
def exif_endpoint(path: str = ""):
    full = _safe_path(path)
    if not full:
        raise HTTPException(404)
    return _exif_info(full, path)


# Background EXIF precompute: the tree's album line shows EXIF status counts
# without a per-file /api/exif call. Progressive: up to EXIF_SCAN_BATCH new
# files per pass, one pass every EXIF_SCAN_INTERVAL seconds.
EXIF_SCAN_BATCH = int(os.environ.get("EXIF_SCAN_BATCH", "500"))
EXIF_SCAN_INTERVAL = float(os.environ.get("EXIF_SCAN_INTERVAL", "10"))

_exif_lock = threading.Lock()
_exif_cache = {}  # (realpath, mtime_ns) -> _exif_info dict (+ "album_set")
_exif_progress = {"scanned": 0, "total": 0, "done": False}


def _exif_scan_pass(batch=EXIF_SCAN_BATCH):
    """One progressive pass over the disk index; returns nb of new files read."""
    files = scan_downloads()["files"]
    paths = {f["path"] for f in files}
    fresh = 0
    for f in files:
        if fresh >= batch:
            break
        full = _safe_path(f["path"])
        if full is None:
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        key = (full, st.st_mtime_ns)
        with _exif_lock:
            if key in _exif_cache:
                continue
        info = _exif_info(full, f["path"])
        info["album_set"] = f.get("album_set")
        with _exif_lock:
            _exif_cache[key] = info
        fresh += 1
    with _exif_lock:
        for k in [k for k, v in _exif_cache.items() if v.get("path") not in paths]:
            del _exif_cache[k]  # file disappeared from the disk index
        n = len(_exif_cache)
        if n > 4 * FILES_MAX:
            _exif_cache.clear()
            n = 0
    total = len(files)
    _exif_progress.update(scanned=n, total=total, done=bool(total) and n >= total)
    return fresh


def _exif_worker():
    while True:
        try:
            _exif_scan_pass()
        except Exception:
            pass
        time.sleep(EXIF_SCAN_INTERVAL)


if os.environ.get("EXIF_PRECOMPUTE", "1").lower() not in ("0", "false", "no"):
    threading.Thread(target=_exif_worker, name="exif-precompute", daemon=True).start()


@app.get("/api/exif_stats")
def exif_stats():
    """Aggregated EXIF check results from the background precompute.

    global: per-state counts over all scanned files; by_album: same per album
    set; scanned/total/done expose the precompute progress."""
    files = scan_downloads()
    total = len(files["files"])
    with _exif_lock:
        infos = list(_exif_cache.values())
    g = Counter(i["state"] for i in infos)
    by_album = {}
    for i in infos:
        c = by_album.setdefault(i.get("album_set") or "(no album)", Counter())
        c[i["state"]] += 1
    states = ("synced", "mismatch", "no_exif")
    return {
        "global": {s: g.get(s, 0) for s in states},
        "by_album": {k: {s: c.get(s, 0) for s in states}
                     for k, c in sorted(by_album.items())},
        "scanned": len(infos), "total": total,
        "done": bool(total) and len(infos) >= total,
        "tz": DISPLAY_TZ,
    }


@app.get("/api/export")
def export():
    t, _ = get_tree()
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "log_file": LOG_FILE,
        "albums_file": ALBUMS_FILE,
        "tree": t,
        "albums_overview": albums_overview(),
        "files": scan_downloads(),
    }
    return JSONResponse(payload, headers={
        "Content-Disposition": 'attachment; filename="web-dashboard-export.json"'})


# ---------------------------------------------------------------- misc

@app.get("/api/raw")
def raw():
    if not os.path.exists(LOG_FILE):
        return JSONResponse({"detail": "log file not present yet"}, status_code=404)
    return FileResponse(LOG_FILE, media_type="text/plain", filename="gallery-dl.log")


@app.get("/api/status")
def status():
    try:
        st = os.stat(LOG_FILE)
        return {"exists": True, "size": st.st_size, "mtime": st.st_mtime,
                "timezone": DISPLAY_TZ, "rejection_regex": REJECTION_PATTERN}
    except OSError:
        return {"exists": False, "size": 0, "mtime": None, "timezone": DISPLAY_TZ,
                "rejection_regex": REJECTION_PATTERN}


_stats_cache = {"key": None, "value": None}

_EMPTY_STATS = {"exists": False, "lines": 0, "errors": 0, "warnings": 0,
                "downloads": 0, "skips": 0, "jobs": 0, "rejections": 0,
                "files": 0, "current_album": None, "current_album_set": None,
                "current_album_files": 0, "last_sleep": None,
                "last_activity": None, "scanned_bytes": 0, "truncated": False}


@app.get("/api/stats")
def stats():
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return dict(_EMPTY_STATS)
    if _stats_cache["key"] == key:
        return _stats_cache["value"]
    counts = {"lines": 0, "errors": 0, "warnings": 0,
              "downloads": 0, "skips": 0, "jobs": 0, "rejections": 0}
    files = 0
    last_job = None
    files_since_job = 0
    last_sleep = None
    last_ts = ""
    start, size = _scan_range()
    for line in _iter_log_lines():
        if not line:
            continue
        counts["lines"] += 1
        tsm = TS_RE.match(line)
        if tsm:
            last_ts = tsm.group(1)
        if REJECTION_RE.search(line):
            counts["rejections"] += 1
        rm = RESP_RE.search(line)
        if rm and int(rm.group(1)) < 400 and rm.group(2) and "fbcdn.net" in line:
            # one media payload downloaded = one file written to disk
            files += 1
            files_since_job += 1
        m = STARTED_RE.search(line)
        if m:
            last_job = m.group(1)
            files_since_job = 0
            counts["jobs"] += 1
        m = SLEEP_RE.search(line)
        if m:
            ts = TS_RE.match(line)
            last_sleep = {"seconds": float(m.group(1)), "reason": m.group(2),
                          "at": ts.group(1) if ts else ""}
        w = WAIT_RE.search(line)
        if w:
            ts = TS_RE.match(line)
            # "Waiting for 15 minutes until HH:MM:SS (429 Too Many Requests)"
            # or the combined unit: "Waiting for 2h 5min until HH:MM:SS (...)"
            last_sleep = {"seconds": _wait_secs(w),
                          "reason": w.group(6) or "waiting",
                          "at": ts.group(1) if ts else ""}
        m = LEVEL_RE.match(line)
        if m:
            level = m.group(1)
            if level in ("error", "critical"):
                counts["errors"] += 1
            elif level == "warning":
                counts["warnings"] += 1
        elif line.startswith("# "):
            counts["skips"] += 1
        elif PROGRESS_RE.match(line):
            pass
        elif line[:1] in (" ", "\t"):
            pass  # indented traceback continuation lines are not downloads
        elif "][" not in line and not line.startswith("["):
            pass  # continuation lines of multi-line messages are not downloads
        else:
            counts["downloads"] += 1
    cur_set = None
    if last_job:
        m = SET_RE.search(last_job)
        cur_set = m.group(1) if m else None
    value = {"exists": True, **counts, "files": files, "current_album": last_job,
             "current_album_set": cur_set,
             "current_album_files": files_since_job, "last_sleep": last_sleep,
             "last_activity": last_ts or None,
             "scanned_bytes": size - start, "truncated": start > 0}
    _stats_cache["key"] = key
    _stats_cache["value"] = value
    return value


@app.get("/api/summary")
def summary():
    """Top banner payload: 3 fixed lines of global state.

    Line 1: files on disk / photos detected / downloaded + albums
    (done / unfinished = started but not '#'-marked / remaining = never
    started). Line 2: last sleep (+ wake-up epoch) + current album.
    Line 3: errors / warnings / rejections + last activity."""
    st = stats()
    t, _ = get_tree()
    files = scan_downloads()
    _annotate_disk(t, files["files"])
    parsed = _read_albums_file(ALBUMS_FILE)
    started = _started_urls()
    dirnames = _dir_names(files["files"])
    if parsed is not None:
        albums_total = len(parsed)
        albums_done = sum(1 for _, d in parsed if d)
        albums_unfinished = sum(1 for u, d in parsed if not d and u in started)
        albums_remaining = albums_total - albums_done - albums_unfinished
        albums_available = True
    else:
        n_started = len({a["url"] for s in t["sessions"] for a in s["albums"]})
        albums_total, albums_done = n_started, 0
        albums_unfinished, albums_remaining = n_started, 0
        albums_available = False
    # global 5-state counters, aggregated per album set (union of sessions)
    disk_by_set = {}
    for f in files["files"]:
        if f.get("album_set") and f.get("fbid"):
            disk_by_set.setdefault(f["album_set"], set()).add(f["fbid"])
    photos_by_set = {}
    for s in t["sessions"]:
        for a in s["albums"]:
            key = "a." + a["set_id"] if a.get("set_id") else a["url"]
            photos_by_set.setdefault(key, []).extend(a["photos"])
    states = Counter()
    for key, photos in photos_by_set.items():
        disk = disk_by_set.get(key, set())
        for p in photos:
            states[_photo_state(p, disk)] += 1
            if p.get("dl"):
                states["downloaded"] += 1
        states["foreign"] += len(disk - {p["fbid"] for p in photos if p.get("fbid")})
    cur_set = st.get("current_album_set")
    wake_epoch = None
    ls = st.get("last_sleep")
    if ls and ls.get("at"):
        try:
            at = datetime.strptime(ls["at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            wake_epoch = (at + timedelta(seconds=float(ls["seconds"]))).timestamp()
        except (ValueError, TypeError):
            wake_epoch = None
    return {
        "exists": st["exists"], "timezone": DISPLAY_TZ,
        "rejection_regex": REJECTION_PATTERN,
        "log_size": os.path.getsize(LOG_FILE) if st["exists"] else 0,
        "lines": st["lines"], "scanned_bytes": st["scanned_bytes"],
        "truncated": st["truncated"],
        "files_on_disk": files["count"], "files_truncated": files["truncated"],
        "photos_detected": sum(s["photos_n"] for s in t["sessions"]),
        "downloaded": sum(s["files"] for s in t["sessions"]),
        "bytes": sum(s["bytes"] for s in t["sessions"]),
        "states": dict(states),
        "albums": {"available": albums_available, "total": albums_total,
                   "done": albums_done, "unfinished": albums_unfinished,
                   "remaining": albums_remaining},
        "last_sleep": ls, "wake_epoch": wake_epoch,
        "current_album": st.get("current_album"), "current_album_set": cur_set,
        "current_album_name": dirnames.get("a." + cur_set) if cur_set else None,
        "current_album_link": _album_link(st["current_album"], cur_set)
                              if st.get("current_album") else None,
        "errors": st["errors"], "warnings": st["warnings"],
        "rejections": st["rejections"], "last_activity": st.get("last_activity"),
    }
