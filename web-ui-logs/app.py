import asyncio
import json
import os
import re

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from tailer import Follower, read_range, read_tail

LOG_FILE = os.environ.get("LOG_FILE", "/downloads/logs/fb-dl.log")
ALBUMS_FILE = os.environ.get("ALBUMS_FILE", "/config/albums.txt")
DEFAULT_TAIL = int(os.environ.get("TAIL_LINES", "500"))
CHUNK_LINES = int(os.environ.get("CHUNK_LINES", "5000"))
MAX_RENDER = int(os.environ.get("MAX_RENDER_LINES", "20000"))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# gallery-dl log line formats:
#   default        : "[{name}][{levelname}] {message}"
#   with {asctime} : "[{asctime}] [{name}][{levelname}] {message}"
LEVEL_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\[[^\]]*\]\[(debug|info|warning|error|critical)\]")
PROGRESS_RE = re.compile(r"^\[\d+/\d+\]")
JOB_RE = re.compile(r"Starting \w+Job for ")
STARTED_RE = re.compile(r"Starting \w+Job for '([^']+)'")

app = FastAPI(title="gallery-dl dashboard")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/logs")
def get_logs(
    offset: int = 0,
    limit: int = Query(default=CHUNK_LINES, ge=1, le=100000),
    tail: int = Query(default=0, ge=0, description="if > 0, return the last N lines instead"),
):
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        size = 0
    if tail > 0:
        lines, off = read_tail(LOG_FILE, tail)
        eof = True
    else:
        lines, off, eof = read_range(LOG_FILE, offset, limit)
    return {"path": LOG_FILE, "size": size, "lines": lines,
            "offset": off, "eof": eof, "max_render": MAX_RENDER}


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


_search_cache = {"key": None, "value": None}


@app.get("/api/search")
def search(q: str = "", limit: int = Query(default=200, ge=1, le=2000)):
    """Search the whole log file (not just what is rendered)."""
    ql = q.strip().lower()
    if not ql:
        return {"q": q, "count": 0, "shown": 0, "lines": []}
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns, ql)
    except OSError:
        return {"q": q, "count": 0, "shown": 0, "lines": []}
    if _search_cache["key"] == key:
        return _search_cache["value"]
    count = 0
    matches = []
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
            if ql in line.lower():
                count += 1
                if len(matches) < limit:
                    matches.append(line.rstrip("\n"))
    value = {"q": q, "count": count, "shown": len(matches), "lines": matches}
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
    """URLs for which the log contains a 'Starting ...Job for <url>' line."""
    try:
        st = os.stat(LOG_FILE)
        key = (st.st_size, st.st_mtime_ns)
    except OSError:
        return set()
    if _started_cache["key"] == key:
        return _started_cache["value"]
    started = set()
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for line in f:
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
        return {"available": False, "path": ALBUMS_FILE, "albums": [], "counts": empty}
    started = _started_urls()
    out = []
    counts = {"done": 0, "incomplete": 0, "not_started": 0}
    for url, done in parsed:
        if done:
            status = "done"
        elif url in started:
            status = "incomplete"
        else:
            status = "not_started"
        counts[status] += 1
        out.append({"url": url, "status": status})
    counts["total"] = len(out)
    return {"available": True, "path": ALBUMS_FILE, "albums": out, "counts": counts}


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
                "downloads": 0, "skips": 0, "jobs": 0}


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
              "downloads": 0, "skips": 0, "jobs": 0}
    with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            if not line:
                continue
            counts["lines"] += 1
            m = LEVEL_RE.match(line)
            if m:
                level = m.group(1)
                if level in ("error", "critical"):
                    counts["errors"] += 1
                elif level == "warning":
                    counts["warnings"] += 1
                if JOB_RE.search(line):
                    counts["jobs"] += 1
            elif line.startswith("# "):
                counts["skips"] += 1
            elif PROGRESS_RE.match(line):
                pass
            else:
                counts["downloads"] += 1
    value = {"exists": True, **counts}
    _stats_cache["key"] = key
    _stats_cache["value"] = value
    return value
