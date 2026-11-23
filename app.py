import asyncio
import json
import os
import re

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tailer import Follower, read_range, read_range_back, read_tail

LOG_FILE = os.environ.get("LOG_FILE", "/downloads/logs/fb-dl.log")
ALBUMS_FILE = os.environ.get("ALBUMS_FILE", "/config/albums.txt")
DEFAULT_TAIL = int(os.environ.get("TAIL_LINES", "500"))
CHUNK_LINES = int(os.environ.get("CHUNK_LINES", "5000"))
MAX_RENDER = int(os.environ.get("MAX_RENDER_LINES", "20000"))
TAIL_WINDOW_BYTES = int(os.environ.get("TAIL_WINDOW_BYTES", str(4 * 1024 * 1024)))
# With SCAN_MAX_BYTES > 0, only the tail of the log file is scanned for
# stats/search/album-start detection (0 = scan the whole file).
SCAN_MAX_BYTES = int(os.environ.get("SCAN_MAX_BYTES", "0"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# gallery-dl log line formats:
#   default        : "[{name}][{levelname}] {message}"
#   with {asctime} : "[{asctime}] [{name}][{levelname}] {message}"
LEVEL_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\[[^\]]*\]\[(debug|info|warning|error|critical)\]")
TS_RE = re.compile(r"^\[([^\]]+)\]")
PROGRESS_RE = re.compile(r"^\[\d+/\d+\]")
JOB_RE = re.compile(r"Starting \w+Job for ")
# greedy (.+) so URLs containing an apostrophe are captured fully
STARTED_RE = re.compile(r"Starting \w+Job for '(.+)'")
SLEEP_RE = re.compile(r"Sleeping ([\d.]+) seconds \(([^)]+)\)")
DOWNLOAD_RE = re.compile(r"Adjusting filename extension of '([^']+)'")

# Anti-bot / scraping rejection patterns, from gallery-dl's extractors
# (mainly facebook.py in 1.32.21) and exception classes:
# AbortExtraction("You've been temporarily blocked from viewing images."),
# AuthRequired(...), ChallengeError/captcha, HttpError 401/403/429, 429 waits.
REJECTION_RE = re.compile(
    r"(?i)temporarily blocked"
    r"|blocked from viewing"
    r"|must be logged in|logged in to continue"
    r"|AuthRequired|AuthorizationError|AuthenticationError"
    r"|Invalid login credentials|credentials required|cookies needed"
    r"|Too Many Requests"
    r"|ChallengeError|[Cc]aptcha|FunCaptcha|GeeTest|hCaptcha"
    r"|HttpError: '(?:401|403|429)"
    r"|content isn't available"
)

app = FastAPI(title="gallery-dl dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/logs")
def get_logs(
    offset: int = 0,
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
    """Return (start_byte, size) of the log region scanned for stats/search."""
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        return 0, 0
    if SCAN_MAX_BYTES > 0 and size > SCAN_MAX_BYTES:
        return size - SCAN_MAX_BYTES, size
    return 0, size


def _iter_log_lines(chunk=1024 * 1024):
    """Yield decoded lines of the scanned region of the log file, streaming
    in `chunk`-byte reads (bounded memory even on huge files)."""
    start, size = _scan_range()
    if size == 0:
        return
    first = start > 0
    pending = b""
    with open(LOG_FILE, "rb") as f:
        f.seek(start)
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            parts = (pending + buf).split(b"\n")
            pending = parts.pop()
            if first:
                first = False
                parts = parts[1:]  # drop the partial first line
            for b in parts:
                yield b.decode("utf-8", "replace")
    if pending:
        yield pending.decode("utf-8", "replace")


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


_started_cache = {"key": None, "value": None}


def _started_urls():
    """URLs for which the scanned log region contains 'Starting ...Job for <url>'."""
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return set()
    if _started_cache["key"] == key:
        return _started_cache["value"]
    started = set()
    for line in _iter_log_lines():
        m = STARTED_RE.search(line)
        if m:
            started.add(m.group(1))
    _started_cache["key"] = key
    _started_cache["value"] = started
    return started


@app.get("/api/albums")
def albums():
    parsed = _read_albums_file(ALBUMS_FILE)
    empty = {"done": 0, "incomplete": 0, "not_started": 0, "total": 0}
    if parsed is None:
        return {"available": False, "path": ALBUMS_FILE, "albums": [],
                "counts": empty, "current": None}
    started = _started_urls()
    out = []
    counts = {"done": 0, "incomplete": 0, "not_started": 0}
    current = None
    for url, done in parsed:
        if done:
            status = "done"
        elif url in started:
            status = "incomplete"
        else:
            status = "not_started"
        if not done and current is None:
            current = url
        counts[status] += 1
        out.append({"url": url, "status": status})
    counts["total"] = len(out)
    return {"available": True, "path": ALBUMS_FILE, "albums": out,
            "counts": counts, "current": current}


@app.get("/api/raw")
def raw():
    if not os.path.exists(LOG_FILE):
        return JSONResponse({"detail": "log file not present yet"}, status_code=404)
    return FileResponse(LOG_FILE, media_type="text/plain", filename="gallery-dl.log")


@app.get("/api/status")
def status():
    try:
        st = os.stat(LOG_FILE)
        return {"exists": True, "size": st.st_size, "mtime": st.st_mtime}
    except OSError:
        return {"exists": False, "size": 0, "mtime": None}


_stats_cache = {"key": None, "value": None}

_EMPTY_STATS = {"exists": False, "lines": 0, "errors": 0, "warnings": 0,
                "downloads": 0, "skips": 0, "jobs": 0, "rejections": 0,
                "files": 0, "current_album": None, "current_album_files": 0,
                "last_sleep": None, "scanned_bytes": 0, "truncated": False}


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
    start, size = _scan_range()
    for line in _iter_log_lines():
        if not line:
            continue
        counts["lines"] += 1
        if REJECTION_RE.search(line):
            counts["rejections"] += 1
        if DOWNLOAD_RE.search(line):
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
        else:
            counts["downloads"] += 1
    value = {"exists": True, **counts, "files": files, "current_album": last_job,
             "current_album_files": files_since_job, "last_sleep": last_sleep,
             "scanned_bytes": size - start, "truncated": start > 0}
    _stats_cache["key"] = key
    _stats_cache["value"] = value
    return value
