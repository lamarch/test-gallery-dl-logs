import asyncio
import json
import os
import re
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from tailer import Follower, read_range, read_range_back, read_span, read_tail

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
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# gallery-dl log line formats:
#   default        : "[{name}][{levelname}] {message}"
#   with {asctime} : "[{asctime}] [{name}][{levelname}] {message}"
LEVEL_RE = re.compile(r"^(?:\[[^\]]*\]\s*)?\[[^\]]*\]\[(debug|info|warning|error|critical)\]")
TS_RE = re.compile(r"^\[([^\]]+)\]")
PROGRESS_RE = re.compile(r"^\[\d+/\d+\]")
STARTED_RE = re.compile(r"Starting \w+Job for '(.+)'")  # greedy: URLs may contain '
SLEEP_RE = re.compile(r"Sleeping ([\d.]+) seconds \(([^)]+)\)")
DOWNLOAD_RE = re.compile(r"Adjusting filename extension of '([^']+)'")
FAILED_RE = re.compile(r"Failed to download '([^']+)'")
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
# Keep in sync with REJECTION_RE in static/index.html.
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

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "avif", "bmp", "mp4", "mov", "webm"}

app = FastAPI(title="gallery-dl dashboard")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# ---------------------------------------------------------------- log reading

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


# ---------------------------------------------------------------- tree parser

def _ts_of(line):
    m = TS_RE.match(line)
    return m.group(1) if m else ""


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
                             "file": None, "sleep": 0.0, "bytes": 0}
                    cur_a["photos"].append(cur_p)
                else:
                    # cap hit: count the photo but don't materialize it;
                    # /api/album/{s}/{a} re-parses this album's span on demand
                    cur_p = None
                    cur_a["photos_truncated"] = True
            rm = RESP_RE.search(line)
            if rm:
                status = int(rm.group(1))
                nbytes = int(rm.group(2) or 0)
                if status < 400 and nbytes:
                    if cur_p is not None:
                        cur_p["bytes"] += nbytes
                    if cur_a is not None:
                        cur_a["bytes"] += nbytes
                    if cur_s is not None:
                        cur_s["bytes"] += nbytes
            dm = DOWNLOAD_RE.search(line)
            if dm:
                fname = dm.group(1)
                if cur_p is not None:
                    cur_p["status"] = "downloaded"
                    cur_p["file"] = fname
                    fm = FN_ID_RE.search(fname)
                    if fm and not cur_p["fbid"]:
                        cur_p["fbid"] = fm.group(1)
                if cur_a is not None:
                    cur_a["files"] += 1
                if cur_s is not None:
                    cur_s["files"] += 1
            fm = FAILED_RE.search(line)
            if fm and cur_p is not None:
                cur_p["status"] = "failed"
                fid = FN_ID_RE.search(fm.group(1))
                if fid and not cur_p["fbid"]:
                    cur_p["fbid"] = fid.group(1)
            sm = SLEEP_RE.search(line)
            if sm:
                secs = float(sm.group(1))
                if cur_a is not None:
                    cur_a["sleeps"] += secs
                if cur_s is not None:
                    cur_s["sleeps"] += secs
                if sm.group(2) == "download" and cur_p is not None:
                    cur_p["sleep"] += secs
            if REJECTION_RE.search(line):
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


@app.get("/api/tree")
def tree():
    t, _ = get_tree()
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
                   "file": None, "sleep": 0.0, "bytes": 0}
            photos.append(cur)
        rm = RESP_RE.search(line)
        if rm and cur is not None and int(rm.group(1)) < 400 and rm.group(2):
            cur["bytes"] += int(rm.group(2))
        dm = DOWNLOAD_RE.search(line)
        if dm and cur is not None:
            cur["status"] = "downloaded"
            cur["file"] = dm.group(1)
            fm = FN_ID_RE.search(dm.group(1))
            if fm and not cur["fbid"]:
                cur["fbid"] = fm.group(1)
        fm = FAILED_RE.search(line)
        if fm and cur is not None:
            cur["status"] = "failed"
            fid = FN_ID_RE.search(fm.group(1))
            if fid and not cur["fbid"]:
                cur["fbid"] = fid.group(1)
        sm = SLEEP_RE.search(line)
        if sm and cur is not None and sm.group(2) == "download":
            cur["sleep"] += float(sm.group(1))
        prev_end = end
    if cur is not None:
        cur["end_off"] = prev_end
        spans[("s", s, a, cur["idx"])] = (cur["start_off"], cur["end_off"])
    return photos


@app.get("/api/album/{s}/{a}")
def album_detail(s: int, a: int, offset: int = 0,
                 limit: int = Query(default=PHOTOS_PAGE_SIZE, ge=1, le=5000)):
    t, spans = get_tree()
    try:
        alb = t["sessions"][s]["albums"][a]
    except (IndexError, KeyError):
        raise HTTPException(404, "album not found")
    photos_src = alb["photos"]
    if alb.get("photos_truncated") and alb["photos_n"] > len(photos_src):
        full = _photos_from_album_span(spans, s, a)
        if full is not None:
            photos_src = full
    by_fbid = {f["fbid"]: f for f in scan_downloads()["files"] if f.get("fbid")}
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
        q["link_photo"] = _photo_link(p.get("fbid"), alb.get("set_id"))
        photos.append(q)
    out = {k: v for k, v in alb.items() if k != "photos"}
    out["photos"] = photos
    return out


def _span_lines(spans, key, offset, limit):
    span = spans.get(key)
    if span is None:
        raise HTTPException(404, "not found")
    lines = read_span(LOG_FILE, span[0], span[1])
    return {"lines": lines[offset:offset + limit], "total": len(lines), "offset": offset}


@app.get("/api/album/{s}/{a}/logs")
def album_logs(s: int, a: int, offset: int = 0,
               limit: int = Query(default=2000, ge=1, le=20000)):
    _, spans = get_tree()
    return _span_lines(spans, ("s", s, a), offset, limit)


@app.get("/api/photo/{s}/{a}/{p}/logs")
def photo_logs(s: int, a: int, p: int, offset: int = 0,
               limit: int = Query(default=2000, ge=1, le=20000)):
    _, spans = get_tree()
    return _span_lines(spans, ("s", s, a, p), offset, limit)


@app.get("/api/albums_overview")
def albums_overview():
    """Album-centric view across all sessions (set_id keyed), with retry flag."""
    t, _ = get_tree()
    by_set = {}
    for f in scan_downloads()["files"]:
        if f.get("album_set"):
            by_set.setdefault(f["album_set"], []).append(f)
    agg = {}
    for s in t["sessions"]:
        for a in s["albums"]:
            key = a["set_id"] or a["url"]
            e = agg.setdefault(key, {
                "key": key, "url": a["url"], "set_id": a["set_id"], "vanity": a["vanity"],
                "link": a["link"], "sessions": [], "status": a["status"],
                "end_reason": a.get("end_reason"), "photos": 0, "files": 0, "bytes": 0,
                "sleeps": 0.0, "errors": 0, "rejections": 0,
                "on_disk": len(by_set.get("a." + a["set_id"], [])) if a["set_id"] else 0})
            e["sessions"].append(s["idx"])
            e["photos"] += a["photos_n"]
            e["files"] += a["files"]
            e["bytes"] += a["bytes"]
            e["sleeps"] += a["sleeps"]
            e["errors"] += a["errors"]
            e["rejections"] += a["rejections"]
    out = list(agg.values())
    for e in out:
        e["retried"] = len(set(e["sessions"])) > 1
    out.sort(key=lambda e: (e["sessions"][0] if e["sessions"] else 0))
    return {"albums": out, "done_urls": t["done_urls"]}


# ---------------------------------------------------------------- disk index

_files_cache = {"value": None}


def scan_downloads(refresh=False):
    """Index image files under DOWNLOADS_DIR (skipping the logs dir).

    Filenames look like 20250429_081104_29388560487456506.jpg and album dirs
    contain '(a.29388587357453819)' -> fbid / name_date / album_set."""
    if not refresh and _files_cache["value"] is not None:
        return _files_cache["value"]
    files = []
    root = DOWNLOADS_DIR
    for dirpath, dirnames, filenames in os.walk(root):
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
            if len(files) >= 50000:
                break
    value = {"root": root, "count": len(files), "files": files}
    _files_cache["value"] = value
    return value


@app.get("/api/files")
def files_endpoint(refresh: int = 0):
    return scan_downloads(refresh=bool(refresh))


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


@app.get("/api/exif")
def exif_endpoint(path: str = ""):
    """EXIF DateTimeOriginal + fs mtime + filename date for one file.

    state: synced (|mtime - exif| <= 2s) / mismatch / no_exif (script not run)."""
    full = _safe_path(path)
    if not full:
        raise HTTPException(404)
    st = os.stat(full)
    dto = None
    try:
        from PIL import Image
        with Image.open(full) as im:
            ex = im.getexif()
            # DateTimeOriginal lives in the Exif sub-IFD (0x8769) on real files,
            # but getexif() only merges IFD0 -> check both
            dto = ex.get(36867) or ex.get_ifd(0x8769).get(36867) or None
    except Exception:
        dto = None
    name_date = None
    m = FN_DATE_RE.search(os.path.basename(full))
    if m:
        d, t = m.group(1), m.group(2)
        name_date = f"{d[:4]}-{d[4:6]}-{d[6:8]} {t[:2]}:{t[2:4]}:{t[4:6]}"
    state = "no_exif"
    diff_s = None
    if dto:
        try:
            exif_ts = datetime.strptime(dto, "%Y:%m:%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
            diff_s = round(st.st_mtime - exif_ts, 3)
            state = "synced" if abs(diff_s) <= 2 else "mismatch"
        except ValueError:
            state = "no_exif"
    return {"path": path, "mtime": st.st_mtime, "exif": dto, "name_date": name_date,
            "state": state, "diff_s": diff_s}


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
