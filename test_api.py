import os
import tempfile
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

BASE = tempfile.mkdtemp()
LOG_FILE = os.path.join(BASE, "fb-dl.log")
ALBUMS_FILE = os.path.join(BASE, "albums.txt")
DOWNLOADS_DIR = os.path.join(BASE, "downloads")

SET_URL = "https://www.facebook.com/media/set?vanity=61580320494056&set=a.29493527130293174"
SET_ID = "29493527130293174"
ALBUM_DIR = os.path.join(DOWNLOADS_DIR, "facebook", "user42", f"Vacances 2025 (a.{SET_ID})")
SYNCED_JPG = "20250506_082802_29493495376963016.jpg"
NOEXIF_JPG = "20250506_082803_29493523296960224.jpg"
MISMATCH_JPG = "20250506_082805_555444333222111.jpg"
FOREIGN_JPG = "20250506_082806_999888777666555.jpg"  # on disk, never seen in logs
ALBUM_REL = f"facebook/user42/Vacances 2025 (a.{SET_ID})"
SYNCED_REL = f"{ALBUM_REL}/{SYNCED_JPG}"
NOEXIF_REL = f"{ALBUM_REL}/{NOEXIF_JPG}"
MISMATCH_REL = f"{ALBUM_REL}/{MISMATCH_JPG}"
FOREIGN_REL = f"{ALBUM_REL}/{FOREIGN_JPG}"

# 18 log lines, 2 sessions (two "Version" lines = restart), same album started
# in both -> "retried"; photo downloaded in s0, failed+banned in s1.
LINES = [
    "[2026-11-22 18:40:12] [gallery-dl][debug] Version 1.32.21",
    f"[2026-11-22 18:40:12] [gallery-dl][debug] Starting DownloadJob for '{SET_URL}'",
    "[2026-11-22 18:40:12] [facebook][debug] Sleeping 3600.00 seconds (extractor)",
    f'[2026-11-22 19:40:31] [urllib3.connectionpool][debug] https://www.facebook.com:443 "GET /photo/?fbid=29493495376963016&set=a.{SET_ID} HTTP/1.1" 200 None',
    "[2026-11-22 19:40:33] [facebook][debug] Sleeping 7.52 seconds (download)",
    '[2026-11-22 19:40:41] [urllib3.connectionpool][debug] https://scontent-cdg4-2.xx.fbcdn.net:443 "GET /v/t39.30808-6/910981173_n.avif HTTP/1.1" 200 392546',
    "[2026-11-22 19:40:41] [downloader.http][debug] Adjusting filename extension of '20250506_082802_29493495376963016.avif' to 'jpg'",
    "[2026-11-22 19:40:42] [gallery-dl][debug] Version 1.32.21",
    f"[2026-11-22 19:40:42] [gallery-dl][debug] Starting DownloadJob for '{SET_URL}'",
    f'[2026-11-22 19:40:49] [urllib3.connectionpool][debug] https://www.facebook.com:443 "GET /photo/?fbid=29493523296960224&set=a.{SET_ID} HTTP/1.1" 200 None',
    "[2026-11-22 19:40:51] [facebook][error] Failed to download '20250506_082802_29493523296960224.avif'",
    "[2026-11-22 19:40:52] [facebook][error] You've been temporarily blocked from viewing images.",
    "[%(asctime)s][%(levelname)s] %(name)s: %(message)s",  # legacy literal line
    "[429/500] https://example.com/progress",              # progress: NOT a rejection
    '  File "/usr/lib/python3.12/site-packages/x.py", line 42, in <module>',  # traceback
    "[2026-11-23 10:00:00] [gallery-dl][debug] Sleeping 11.34 seconds (request)",
    f'[2026-11-23 10:00:01] [urllib3.connectionpool][debug] https://www.facebook.com:443 "GET /photo/?fbid=29493523083626912&set=a.{SET_ID} HTTP/1.1" 200 None',
    # media served as .jpg directly by the CDN: 200 with bytes, NO "Adjusting" line
    # -> must still count as 1 downloaded file
    '[2026-11-23 10:00:02] [urllib3.connectionpool][debug] https://scontent-cdg4-2.xx.fbcdn.net:443 "GET /v/t39.30808-6/555_n.jpg HTTP/1.1" 200 54321',
    "[2026-11-23 10:00:03] [downloader.http][debug] Adjusting filename extension of '20250506_082802_29493523083626912.avif' to 'jpg'",
    # photo page seen, NO download line (file already on disk from a previous run,
    # MISMATCH_JPG) -> status "pending" in the tree, but on_disk -> state "already"
    f'[2026-11-23 10:00:04] [urllib3.connectionpool][debug] https://www.facebook.com:443 "GET /photo/?fbid=555444333222111&set=a.{SET_ID} HTTP/1.1" 200 None',
]
with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")

with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
    f.write("# https://example.com/done\n")
    f.write(SET_URL + "\n")
    f.write("https://example.com/never\n")

os.makedirs(os.path.join(DOWNLOADS_DIR, "logs"))
with open(os.path.join(DOWNLOADS_DIR, "logs", "fb-dl.log"), "w") as f:
    f.write("this must be skipped by the disk scan\n")
os.makedirs(ALBUM_DIR)

from PIL import Image  # noqa: E402

# EXIF DateTimeOriginal carries no timezone: the app interprets it in
# DISPLAY_TZ (Europe/Paris here), like a script syncing mtime <- EXIF in local time
_exif_ts = datetime(2025, 5, 6, 8, 28, 2, tzinfo=ZoneInfo("Europe/Paris")).timestamp()
_im = Image.new("RGB", (16, 16), (200, 100, 50))
_ex = Image.Exif()
_ex.get_ifd(0x8769)[36867] = "2025:05:06 08:28:02"  # DateTimeOriginal (Exif sub-IFD)
_im.save(os.path.join(ALBUM_DIR, SYNCED_JPG), "JPEG", exif=_ex)
os.utime(os.path.join(ALBUM_DIR, SYNCED_JPG), (_exif_ts, _exif_ts))  # synced
Image.new("RGB", (16, 16), (1, 2, 3)).save(os.path.join(ALBUM_DIR, NOEXIF_JPG), "JPEG")
_ex2 = Image.Exif()
_ex2.get_ifd(0x8769)[36867] = "2025:05:06 08:28:05"
Image.new("RGB", (16, 16), (9, 9, 9)).save(os.path.join(ALBUM_DIR, MISMATCH_JPG), "JPEG", exif=_ex2)
_bad = _exif_ts + 3 + 3600  # exif(08:28:05 Paris) + 1h -> mismatch
os.utime(os.path.join(ALBUM_DIR, MISMATCH_JPG), (_bad, _bad))
Image.new("RGB", (16, 16), (5, 5, 5)).save(os.path.join(ALBUM_DIR, FOREIGN_JPG), "JPEG")

os.environ["LOG_FILE"] = LOG_FILE
os.environ["ALBUMS_FILE"] = ALBUMS_FILE
os.environ["DOWNLOADS_DIR"] = DOWNLOADS_DIR
os.environ["DISPLAY_TZ"] = "Europe/Paris"

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from app import REJECTION_RE, app  # noqa: E402


def clear_caches():
    for c in (app_module._stats_cache, app_module._search_cache,
              app_module._tree_cache):
        c.update(key=None, value=None)
    app_module._files_cache.update(value=None, ts=0.0)
    with app_module._exif_lock:
        app_module._exif_cache.clear()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_index_serves_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn(b"<title>", r.content)
        self.assertIn(b"tab-tree", r.content)
        self.assertIn(b"tab-files", r.content)

    def test_favicon_served(self):
        r = self.client.get("/static/favicon.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<svg", r.content)

    def test_rejection_regex_no_progress_false_positive(self):
        self.assertFalse(REJECTION_RE.search("[429/500] https://example.com/x"))
        self.assertTrue(REJECTION_RE.search(
            "[2026-11-22 19:40:33] [facebook][debug] Sleeping 900.00 seconds (429 Too Many Requests)"))
        self.assertTrue(REJECTION_RE.search(
            "[facebook][error] HttpError: '429 Client Error: Too Many Requests' for url: 'x'"))

    def test_logs_tail_and_offsets(self):
        r = self.client.get("/api/logs", params={"tail": 2})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["path"], LOG_FILE)
        self.assertEqual(len(data["lines"]), 2)
        self.assertIn("555444333222111", data["lines"][-1])
        self.assertEqual(data["offset"], os.path.getsize(LOG_FILE))
        self.assertTrue(data["eof"])
        # 18-line file, tail=2 -> earlier lines exist: has_more must be True
        # (this is what enables the "Load earlier" button)
        self.assertTrue(data["has_more"])
        self.assertGreater(data["start_offset"], 0)
        self.assertIn("max_render", data)

    def test_logs_tail_is_bounded(self):
        r = self.client.get("/api/logs", params={"tail": 10**9})
        self.assertEqual(r.status_code, 422)

    def test_logs_range_from_beginning(self):
        r = self.client.get("/api/logs", params={"offset": 0, "limit": 2})
        data = r.json()
        self.assertEqual(data["lines"][0], LINES[0])
        self.assertEqual(len(data["lines"]), 2)
        self.assertFalse(data["eof"])
        r2 = self.client.get("/api/logs", params={"offset": data["offset"], "limit": 100})
        data2 = r2.json()
        self.assertEqual(len(data2["lines"]), len(LINES) - 2)
        self.assertTrue(data2["eof"])

    def test_logs_before(self):
        size = os.path.getsize(LOG_FILE)
        r = self.client.get("/api/logs", params={"before": size, "limit": 3})
        data = r.json()
        self.assertEqual(len(data["lines"]), 3)
        self.assertIn("555444333222111", data["lines"][-1])  # last 3 lines of the fixture
        self.assertIn("54321", data["lines"][0])
        self.assertTrue(data["has_more"])
        self.assertLess(data["start_offset"], size)
        r2 = self.client.get("/api/logs", params={"before": data["start_offset"], "limit": 100})
        data2 = r2.json()
        self.assertEqual(len(data2["lines"]), len(LINES) - 3)
        self.assertFalse(data2["has_more"])
        self.assertEqual(data2["start_offset"], 0)

    def test_search_whole_file(self):
        data = self.client.get("/api/search", params={"q": "failed"}).json()
        self.assertEqual(data["count"], 1)
        self.assertIn("Failed to download", data["lines"][0])
        data = self.client.get("/api/search", params={"q": "photo/?fbid"}).json()
        self.assertEqual(data["count"], 4)
        data = self.client.get("/api/search", params={"q": ""}).json()
        self.assertEqual(data["count"], 0)

    def test_albums_status(self):
        data = self.client.get("/api/albums").json()
        self.assertTrue(data["available"])
        # 3 entries in albums.txt; the log/disk album is SET_URL -> no extra
        self.assertEqual(data["counts"],
                         {"done": 1, "incomplete": 1, "not_started": 1,
                          "not_in_file": 0, "total": 3})
        self.assertEqual(data["current"], SET_URL)
        by_url = {a["url"]: a["status"] for a in data["albums"]}
        self.assertEqual(by_url["https://example.com/done"], "done")
        self.assertEqual(by_url[SET_URL], "incomplete")   # started, not '#' -> unfinished
        self.assertEqual(by_url["https://example.com/never"], "not_started")  # remaining
        names = {a["url"]: a.get("name") for a in data["albums"]}
        # album name = the download dir title gallery-dl created
        self.assertEqual(names[SET_URL], "Vacances 2025")
        self.assertIsNone(names["https://example.com/never"])  # no dir -> no name
        # 3-way comparison fields
        e = by_url and {a["url"]: a for a in data["albums"]}[SET_URL]
        self.assertTrue(e["in_file"])
        self.assertFalse(e["done"])
        self.assertTrue(e["in_logs"])
        self.assertEqual(e["on_disk"], 4)
        self.assertEqual(e["set_id"], SET_ID)
        self.assertEqual(e["link"],
                         f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")
        # timing fields for the compare table: total span duration + last activity
        self.assertGreater(e["total_duration_s"], 0)
        self.assertIsNotNone(e["last_activity"])

    def test_albums_not_in_file(self):
        # an album dir on disk but absent from albums.txt -> not_in_file entry
        other_dir = os.path.join(DOWNLOADS_DIR, "facebook", "user42", "Other (a.777000111222333)")
        os.makedirs(other_dir, exist_ok=True)
        from PIL import Image as _Img
        extra = os.path.join(other_dir, "20250506_082807_777000111222333.jpg")
        _Img.new("RGB", (8, 8)).save(extra, "JPEG")
        try:
            self.client.get("/api/files", params={"refresh": 1})
            clear_caches()
            data = self.client.get("/api/albums").json()
            self.assertEqual(data["counts"]["not_in_file"], 1)
            self.assertEqual(data["counts"]["total"], 4)
            e = [a for a in data["albums"] if a["status"] == "not_in_file"][0]
            self.assertFalse(e["in_file"])
            self.assertFalse(e["in_logs"])       # never started in logs
            self.assertEqual(e["on_disk"], 1)
            self.assertEqual(e["set_id"], "777000111222333")
            self.assertEqual(e["name"], "Other")  # from the download dir title
        finally:
            os.remove(extra)
            os.rmdir(other_dir)
            self.client.get("/api/files", params={"refresh": 1})
            clear_caches()

    def test_session_log_span(self):
        r = self.client.get("/api/session/0/logs").json()
        self.assertEqual(r["total"], 7)          # L1..L7 (up to the 2nd Version line)
        self.assertIn("Version 1.32.21", r["lines"][0])
        self.assertIn("Starting DownloadJob", r["lines"][1])
        r1 = self.client.get("/api/session/1/logs").json()
        self.assertEqual(r1["total"], len(LINES) - 7)
        self.assertEqual(self.client.get("/api/session/9/logs").status_code, 404)

    def test_stats_counts(self):
        data = self.client.get("/api/stats").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["lines"], len(LINES))
        self.assertEqual(data["errors"], 2)
        self.assertEqual(data["warnings"], 0)
        self.assertEqual(data["jobs"], 2)
        self.assertEqual(data["downloads"], 1)   # legacy literal line only
        self.assertEqual(data["skips"], 0)
        self.assertEqual(data["rejections"], 1)  # NOT 2: [429/500] must not match
        # files = media payloads downloaded from the CDN (2 fbcdn 200-with-bytes),
        # NOT "Adjusting filename extension" lines (which only appear on avif->jpg)
        self.assertEqual(data["files"], 2)
        self.assertEqual(data["current_album"], SET_URL)
        self.assertEqual(data["current_album_files"], 1)
        self.assertEqual(data["last_sleep"],
                         {"seconds": 11.34, "reason": "request", "at": "2026-11-23 10:00:00"})
        self.assertEqual(data["last_activity"], "2026-11-23 10:00:04")
        self.assertEqual(data["current_album_set"], SET_ID)
        self.assertFalse(data["truncated"])

    def test_summary_banner(self):
        data = self.client.get("/api/summary").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["timezone"], "Europe/Paris")
        self.assertEqual(data["files_on_disk"], 4)
        self.assertEqual(data["photos_detected"], 4)   # 1 photo in s0 + 3 in s1
        self.assertEqual(data["downloaded"], 2)        # 2 CDN payloads (L6 + L18)
        self.assertEqual(data["albums"], {"available": True, "total": 3, "done": 1,
                                          "unfinished": 1, "remaining": 1})
        # global counters: 5-state + downloaded (CDN payload seen in logs)
        self.assertEqual(data["states"],
                         {"full": 1, "downloaded": 2, "rejected": 1, "already": 1,
                          "missing": 1, "foreign": 1})
        self.assertEqual(data["current_album"], SET_URL)
        self.assertEqual(data["current_album_set"], SET_ID)
        self.assertEqual(data["current_album_name"], "Vacances 2025")
        self.assertEqual(data["current_album_link"],
                         f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")
        self.assertEqual(data["last_sleep"]["reason"], "request")
        self.assertEqual(data["last_sleep"]["seconds"], 11.34)
        # wake-up = last sleep "at" (UTC) + seconds
        self.assertAlmostEqual(
            data["wake_epoch"],
            datetime(2026, 11, 23, 10, 0, 0, tzinfo=timezone.utc).timestamp() + 11.34)
        self.assertEqual(data["errors"], 2)
        self.assertEqual(data["warnings"], 0)
        self.assertEqual(data["rejections"], 1)
        self.assertEqual(data["last_activity"], "2026-11-23 10:00:04")

    def test_stats_scan_cap(self):
        old = app_module.SCAN_MAX_BYTES
        clear_caches()
        app_module.SCAN_MAX_BYTES = 60
        try:
            data = self.client.get("/api/stats").json()
            self.assertTrue(data["truncated"])
            self.assertEqual(data["scanned_bytes"], 60)
            self.assertLess(data["lines"], len(LINES))
        finally:
            app_module.SCAN_MAX_BYTES = old
            clear_caches()
        data = self.client.get("/api/stats").json()
        self.assertEqual(data["lines"], len(LINES))
        self.assertFalse(data["truncated"])

    def test_status(self):
        data = self.client.get("/api/status").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["size"], os.path.getsize(LOG_FILE))
        self.assertEqual(data["timezone"], "Europe/Paris")  # DISPLAY_TZ env

    def test_negative_offsets_rejected(self):
        for path in ("/api/logs?offset=-1",
                     "/api/album/0/0?offset=-1",
                     "/api/album/0/0/logs?offset=-5",
                     "/api/photo/0/0/0/logs?offset=-5"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 422, path)

    def test_raw_download(self):
        r = self.client.get("/api/raw")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])

    def test_stream_replays_history(self):
        with self.client.stream("GET", "/api/stream", params={"since": 0}) as r:
            events = 0
            for line in r.iter_lines():
                if line == "event: log":
                    events += 1
                if events >= len(LINES):
                    break
        self.assertEqual(events, len(LINES))


class TreeApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_tree_sessions_albums_photos(self):
        t = self.client.get("/api/tree").json()
        self.assertEqual(t["runs"], 2)
        self.assertEqual(len(t["sessions"]), 2)
        s0, s1 = t["sessions"]
        self.assertEqual(s0["start"], "2026-11-22 18:40:12")
        self.assertGreater(s0["on_disk_size"], 0)  # per-session on-disk size
        self.assertEqual(s0["rejections_unresolved"], 0)
        self.assertEqual(s1["rejections_unresolved"], 0)  # rejection recovered
        self.assertEqual(s0["albums"][0]["set_id"], SET_ID)
        self.assertEqual(s0["albums"][0]["vanity"], "61580320494056")
        self.assertEqual(s0["albums"][0]["link"],
                         f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")
        a0 = s0["albums"][0]
        self.assertEqual(a0["name"], "Vacances 2025")     # download dir title (as downloaded)
        self.assertTrue(a0["retried"])   # the SET album appears in sessions 0 and 1
        self.assertEqual(a0["extra_on_disk"], 3)  # on disk, never downloaded in logs
        self.assertEqual(a0["on_disk"], 4)                # log-vs-disk annotation (4 files in dir)
        self.assertEqual(a0["missing_on_disk"], 0)        # its only photo is on disk
        # full = on disk AND CDN payload in logs; foreign = on disk, never in logs
        self.assertEqual(a0["states"], {"full": 1, "downloaded": 1, "foreign": 1})
        a1t = s1["albums"][0]
        self.assertEqual(a1t["missing_on_disk"], 1)       # fbid ...083626912 downloaded in s1, not on disk
        # failed-but-on-disk and pending-but-on-disk -> already; downloaded-not-on-disk -> missing
        self.assertEqual(a1t["states"],
                         {"downloaded": 1, "rejected": 1, "already": 1, "missing": 1,
                          "foreign": 1})
        self.assertEqual(a0["photos_n"], 1)
        self.assertEqual(a0["files"], 1)
        self.assertEqual(a0["bytes"], 392546)
        self.assertAlmostEqual(a0["sleeps"], 3607.52)
        self.assertEqual(a0["status"], "incomplete")
        p = a0["photos"][0]
        self.assertEqual(p["fbid"], "29493495376963016")
        self.assertEqual(p["status"], "downloaded")
        self.assertTrue(p["dl"])                            # CDN payload seen = proof
        self.assertEqual(p["state"], "full")                # on disk AND downloaded
        self.assertEqual(p["path"], SYNCED_REL)
        self.assertEqual(p["file"], "20250506_082802_29493495376963016.avif")
        self.assertEqual(p["bytes"], 392546)
        self.assertAlmostEqual(p["sleep"], 7.52)
        a1 = s1["albums"][0]
        self.assertEqual(a1["photos_n"], 3)
        # the rejection was recovered (the photo file is on disk) -> not banned
        self.assertEqual(a1["status"], "error")
        self.assertTrue(a1t.get("running"))        # last album of the last session
        self.assertFalse(a0.get("running"))
        self.assertEqual(a1["rejections"], 1)
        self.assertEqual(a1["errors"], 2)
        self.assertEqual(a1["photos"][0]["status"], "failed")
        self.assertFalse(a1["photos"][0]["dl"])
        # failed + rejection line within its span -> rejected (kept even though
        # the file is on disk - the rejection history stays visible)
        self.assertEqual(a1["photos"][0]["state"], "rejected")
        self.assertTrue(a1["photos"][0]["rejected"])
        # photo 1: downloaded via the CDN .jpg payload (200 + bytes), no Adjusting
        # line needed; the later Adjusting line only sets the filename (no dl flag)
        self.assertEqual(a1["photos"][1]["status"], "downloaded")
        self.assertTrue(a1["photos"][1]["dl"])
        self.assertEqual(a1["photos"][1]["state"], "missing")   # dl in logs, NOT on disk
        self.assertEqual(a1["photos"][1]["bytes"], 54321)
        self.assertEqual(a1["photos"][1]["file"], "20250506_082802_29493523083626912.avif")
        # last photo: seen in log, no download payload, but on disk -> already
        self.assertEqual(a1["photos"][2]["status"], "pending")
        self.assertFalse(a1["photos"][2]["dl"])
        self.assertEqual(a1["photos"][2]["state"], "already")
        self.assertEqual(a1["photos"][2]["fbid"], "555444333222111")
        self.assertEqual(a1["files"], 1)   # 1 CDN payload, not 2 "Adjusting" lines
        self.assertEqual(a1["bytes"], 54321)
        self.assertEqual(s1["files"], 1)

    def test_album_detail_merges_disk(self):
        d = self.client.get("/api/album/0/0").json()
        self.assertEqual(d["photos_n"], 1)
        self.assertEqual(len(d["photos"]), 1)
        self.assertEqual(d["on_disk"], 4)
        self.assertEqual(d["states"], {"full": 1, "downloaded": 1, "foreign": 1})
        self.assertEqual(d["missing_on_disk"], [])        # s0's photo is on disk
        d1 = self.client.get("/api/album/1/0").json()
        self.assertEqual(d1["missing_on_disk"], ["29493523083626912"])  # downloaded, not on disk
        by_fbid = {p["fbid"]: p for p in d1["photos"]}
        # pending in log but on disk (skipped, file from a previous run) -> already
        self.assertTrue(by_fbid["555444333222111"]["on_disk"])
        self.assertEqual(by_fbid["555444333222111"]["status"], "pending")
        self.assertEqual(by_fbid["555444333222111"]["state"], "already")
        # failed in log but file on disk -> already (the error is still in errors)
        self.assertEqual(by_fbid["29493523296960224"]["state"], "rejected")
        # downloaded in log (CDN payload) but not on disk -> missing
        self.assertEqual(by_fbid["29493523083626912"]["state"], "missing")
        self.assertTrue(by_fbid["29493523083626912"]["dl"])
        self.assertEqual(d1["states"],
                         {"downloaded": 1, "rejected": 1, "already": 1, "missing": 1,
                          "foreign": 1})
        p = d["photos"][0]
        self.assertTrue(p["on_disk"])
        self.assertEqual(p["path"], SYNCED_REL)
        self.assertEqual(p["name_date"], "2025-05-06 08:28:02")
        self.assertGreater(p["size"], 0)
        self.assertEqual(p["link_photo"],
                         f"https://www.facebook.com/photo/?fbid=29493495376963016&set=a.{SET_ID}")
        d1 = self.client.get("/api/album/1/0").json()
        self.assertEqual(d1["photos_n"], 3)
        by_fbid = {p["fbid"]: p for p in d1["photos"]}
        self.assertTrue(by_fbid["29493523296960224"]["on_disk"])   # noexif jpg exists on disk
        self.assertFalse(by_fbid["29493523083626912"]["on_disk"])  # not downloaded to disk

    def test_album_detail_pagination(self):
        d = self.client.get("/api/album/1/0", params={"offset": 0, "limit": 1}).json()
        self.assertEqual(len(d["photos"]), 1)
        self.assertEqual(d["photos_n"], 3)
        d2 = self.client.get("/api/album/1/0", params={"offset": 1, "limit": 1}).json()
        self.assertEqual(len(d2["photos"]), 1)
        self.assertNotEqual(d2["photos"][0]["fbid"], d["photos"][0]["fbid"])

    def test_album_not_found(self):
        self.assertEqual(self.client.get("/api/album/9/9").status_code, 404)
        self.assertEqual(self.client.get("/api/album/9/9/logs").status_code, 404)

    def test_album_and_photo_log_spans(self):
        r = self.client.get("/api/album/0/0/logs").json()
        self.assertEqual(r["total"], 6)          # L2..L7
        self.assertIn("Starting DownloadJob", r["lines"][0])
        self.assertIn("Adjusting filename", r["lines"][-1])
        r = self.client.get("/api/photo/0/0/0/logs").json()
        self.assertEqual(r["total"], 4)          # L4..L7
        self.assertIn("fbid=29493495376963016", r["lines"][0])
        r = self.client.get("/api/photo/1/0/0/logs").json()
        self.assertEqual(r["total"], 7)          # L10..L16
        self.assertIn("fbid=29493523296960224", r["lines"][0])
        self.assertTrue(any("temporarily blocked" in l for l in r["lines"]))

    def test_tree_photo_cap_and_span_fallback(self):
        old = app_module.TREE_MAX_PHOTOS
        clear_caches()
        app_module.TREE_MAX_PHOTOS = 1
        try:
            t = self.client.get("/api/tree").json()
            total_seen = sum(a["photos_n"] for s in t["sessions"] for a in s["albums"])
            stored = sum(len(a["photos"]) for s in t["sessions"] for a in s["albums"])
            self.assertEqual(total_seen, 4)   # all photos still counted
            self.assertEqual(stored, 1)       # but only 1 materialized (RAM cap)
            self.assertTrue(any(a["photos_truncated"]
                                for s in t["sessions"] for a in s["albums"]))
            # album detail still returns ALL photos of a truncated album
            # (re-parsed from its log span)
            d = self.client.get("/api/album/1/0").json()
            self.assertEqual(d["photos_n"], 3)
            self.assertEqual(len(d["photos"]), 3)
            self.assertEqual({p["fbid"] for p in d["photos"]},
                             {"29493523296960224", "29493523083626912", "555444333222111"})
            # the fallback also registers per-photo log spans
            r = self.client.get("/api/photo/1/0/0/logs").json()
            self.assertIn("fbid=29493523296960224", r["lines"][0])
        finally:
            app_module.TREE_MAX_PHOTOS = old
            clear_caches()
        d = self.client.get("/api/album/1/0").json()
        self.assertEqual(len(d["photos"]), 3)

    def test_span_offset_beyond_total(self):
        r = self.client.get("/api/photo/0/0/0/logs", params={"offset": 100}).json()
        self.assertEqual(r["lines"], [])
        self.assertEqual(r["total"], 4)
        self.assertEqual(r["offset"], 100)

    def test_albums_overview_retried(self):
        o = self.client.get("/api/albums_overview").json()
        self.assertEqual(len(o["albums"]), 1)
        a = o["albums"][0]
        self.assertEqual(a["set_id"], SET_ID)
        # status recomputed from aggregated counters: the rejection was
        # recovered (photo downloaded right after / on disk) -> not banned
        self.assertEqual(a["status"], "error")
        self.assertEqual(a["rejections_unresolved"], 0)
        self.assertTrue(a["running"])              # its last session's album
        # per-session details: one entry per session, with album_idx + photos
        sds = {d["session"]: d for d in a["session_details"]}
        self.assertTrue(sds[1]["running"])         # that session detail row
        self.assertFalse(sds[0]["running"])
        self.assertEqual(sorted(sds), [0, 1])
        self.assertEqual(sds[0]["album_idx"], 0)
        self.assertEqual(sds[0]["photos_n"], 1)
        self.assertEqual(sds[0]["status"], "incomplete")
        self.assertEqual(sds[1]["photos_n"], 3)
        self.assertEqual(sds[1]["files"], 1)
        self.assertEqual(sds[1]["status"], "error")
        self.assertIn("session_start", sds[0])
        self.assertIn("start", sds[1])
        self.assertEqual(a["sessions"], [0, 1])
        self.assertTrue(a["retried"])
        self.assertEqual(a["name"], "Vacances 2025")
        self.assertEqual(a["photos"], 4)
        self.assertEqual(a["files"], 2)            # 2 CDN payloads (L6 + L18)
        self.assertEqual(a["bytes"], 392546 + 54321)
        self.assertEqual(a["errors"], 2)
        self.assertEqual(a["rejections"], 1)
        self.assertEqual(a["on_disk"], 4)
        self.assertEqual(a["missing_on_disk"], 1)  # downloaded in log, not on disk
        self.assertEqual(a["extra_on_disk"], 3)    # on disk, not downloaded in log
        self.assertEqual(a["states"], {"full": 1, "downloaded": 2, "rejected": 1,
                                      "already": 1, "missing": 1, "foreign": 1})
        self.assertEqual(a["link"], f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")


class WaitingLineTests(unittest.TestCase):
    """gallery-dl's anti-bot wait: "Waiting for 15 minutes until HH:MM:SS
    (429 Too Many Requests)" - must feed the banner sleep + wake-up."""

    def setUp(self):
        self.client = TestClient(app)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write("[2026-11-23 11:00:00] [facebook][info] "
                    "Waiting for 15 minutes until 11:15:00 (429 Too Many Requests)\n")

    def tearDown(self):
        # restore the shared fixture for the classes that run after
        orig = open(LOG_FILE, encoding="utf-8").read()
        keep = orig[:orig.rfind("[2026-11-23 11:00:00]")]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(keep)

    def test_waiting_line_sleep(self):
        ls = self.client.get("/api/stats").json()["last_sleep"]
        self.assertEqual(ls["seconds"], 900.0)
        self.assertEqual(ls["reason"], "429 Too Many Requests")
        self.assertEqual(ls["at"], "2026-11-23 11:00:00")
        wake = self.client.get("/api/summary").json()["wake_epoch"]
        self.assertAlmostEqual(wake, datetime(2026, 11, 23, 11, 15,
                                              tzinfo=timezone.utc).timestamp())
        # the wait accumulates into the running session/album sleep totals
        t = self.client.get("/api/tree").json()
        last = t["sessions"][-1]
        self.assertGreaterEqual(last["sleeps"], 900.0)


class RejectionRecoveryTests(unittest.TestCase):
    """A rejected photo keeps its 'rejected' state even when it is downloaded
    right after (second try), and the album only stays 'banned' while some
    rejected photo was never successfully downloaded later (or is on disk)."""

    def setUp(self):
        self.client = TestClient(app)
        self._orig = open(LOG_FILE, encoding="utf-8").read()
        lines = [
            "[2026-11-22 18:00:00] [gallery-dl][debug] Version 1.32.21",
            "[2026-11-22 18:00:01] [facebook][debug] Starting DownloadJob for "
            "'https://www.facebook.com/media/set/?set=a.777&type=3'",
            '[2026-11-22 18:00:02] [urllib3.connectionpool][debug] '
            'https://www.facebook.com:443 "GET /photo/?fbid=77001&set=a.777 HTTP/1.1" 200 None',
            "[2026-11-22 18:00:03] [facebook][warning] "
            "You've been temporarily blocked from viewing images.",
            "[2026-11-22 18:00:04] [gallery-dl][debug] Version 1.32.21",
            "[2026-11-22 18:02:01] [facebook][debug] Starting DownloadJob for "
            "'https://www.facebook.com/media/set/?set=a.777&type=3'",
            '[2026-11-22 18:02:02] [urllib3.connectionpool][debug] '
            'https://www.facebook.com:443 "GET /photo/?fbid=77001&set=a.777 HTTP/1.1" 200 None',
            '[2026-11-22 18:02:03] [urllib3.connectionpool][debug] '
            'https://x.fbcdn.net:443 "GET /v/t39/1.jpg HTTP/1.1" 200 1000',
        ]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def tearDown(self):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig)

    def test_rejected_state_is_kept_after_recovery(self):
        t = self.client.get("/api/tree").json()
        s0, s1 = t["sessions"]
        p0 = s0["albums"][0]["photos"][0]
        self.assertTrue(s0["albums"][0]["retried"])   # retried across the 2 sessions
        self.assertEqual(s0["rejections_unresolved"], 0)
        self.assertTrue(p0["rejected"])
        self.assertEqual(p0["state"], "rejected")     # visible even if re-downloaded
        p1 = s1["albums"][0]["photos"][0]             # second try: downloaded fine
        self.assertTrue(p1["dl"])
        self.assertFalse(p1.get("rejected"))
        self.assertEqual(p1["state"], "missing")      # payload seen, not on disk

    def test_album_unbanned_once_every_rejection_recovered(self):
        t = self.client.get("/api/tree").json()
        a0 = t["sessions"][0]["albums"][0]
        self.assertEqual(a0["status"], "incomplete")
        self.assertEqual(a0["rejections_unresolved"], 0)
        self.assertIn("recovered", a0["end_reason"])   # explicit recovery reason
        for s in t["sessions"][1:]:
            self.assertNotEqual(s["albums"][0]["status"], "banned")
        o = self.client.get("/api/albums_overview").json()["albums"][0]
        self.assertEqual(o["status"], "incomplete")
        self.assertEqual(o["rejections_unresolved"], 0)


class FacebookLogCasesTests(unittest.TestCase):
    """Every log line gallery-dl 1.32.21's Facebook path can emit - source-
    audited from facebook.py, job.py, extractor common.py (wait/sleep) and
    downloader/http.py of the v1.32.21 tag."""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        self._orig = open(LOG_FILE, encoding="utf-8").read()
        GET = ('[2026-12-01 10:00:0%d] [urllib3.connectionpool][debug] '
               'https://www.facebook.com:443 "GET /photo/?fbid=%s&set=a.%s HTTP/1.1" 200 None')
        lines = [
            "[2026-12-01 10:00:00] [gallery-dl][debug] Version 1.32.21",
            "[2026-12-01 10:00:01] [gallery-dl][debug] Using FacebookSetExtractor for '" + SET_URL + "'",
            "[2026-12-01 10:00:02] [gallery-dl][debug] Starting DownloadJob for '" + SET_URL + "'",
            GET % (3, "100", SET_ID),
            # followups found in comments: logged (debug), fetched later
            "[2026-12-01 10:00:04] [facebook][debug] Found a followup in comments: 101",
            # url extraction blocked permanently -> Skipping (error),
            # emitted BEFORE the next photo page is fetched
            "[2026-12-01 10:00:05] [facebook][error] Failed to find photo download URL for "
            "https://www.facebook.com/photo/?fbid=100&set=a." + SET_ID + ". Skipping.",
            GET % (6, "101", SET_ID),
            # url extraction failed once -> Retrying (warning) + 429 wait (info)
            "[2026-12-01 10:00:07] [facebook][warning] Failed to find photo download URL for "
            "https://www.facebook.com/photo/?fbid=101&set=a." + SET_ID + ". Retrying in 900.0 seconds.",
            "[2026-12-01 10:00:08] [facebook][info] Waiting for 15 minutes until "
            "10:15:08 (429 Too Many Requests)",
            # combined-hours wait variant ("2h 5min")
            "[2026-12-01 10:16:00] [facebook][info] Waiting for 2h 5min until 12:21:00 (rate limit)",
            # download failure: 1.32.21 logs the filename WITHOUT quotes
            "[2026-12-01 10:17:00] [downloader.http][error] "
            "Failed to download 20261201_101700_101.avif",
            # anti-bot abort: multi-line message + non-prefixed continuation lines
            "[2026-12-01 10:17:01] [facebook][error] "
            "You've been temporarily blocked from viewing images.",
            "Please try using a different account, using a VPN or waiting before you retry.",
            'You can use this URL to continue from where you left off (added "&setextract"): ',
            "https://www.facebook.com/photo/?fbid=101&set=a." + SET_ID + "&setextract",
            "[2026-12-01 10:18:00] [facebook][debug] Can't find next image in the set. Extraction is over.",
            "[2026-12-01 10:18:01] [facebook][info] Detected jump to the beginning of the set. (101 -> 1)",
            # the retry succeeded: CDN payload for photo 101
            '[2026-12-01 10:19:00] [urllib3.connectionpool][debug] '
            'https://scontent.xx.fbcdn.net:443 "GET /v/t34/1.jpg HTTP/1.1" 200 54321',
        ]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def tearDown(self):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig)

    def test_tree_photos_and_rejections(self):
        t = self.client.get("/api/tree").json()
        a = t["sessions"][0]["albums"][0]
        self.assertEqual(a["photos_n"], 2)             # followup included
        p0, p1 = a["photos"]
        self.assertTrue(p0["rejected"])               # "Skipping" marks the photo
        self.assertEqual(p0["state"], "rejected")
        self.assertFalse(p0["dl"])
        self.assertTrue(p1["rejected"])               # the multi-line anti-bot msg
        self.assertTrue(p1["dl"])                     # recovered on the retry
        self.assertEqual(p1["state"], "rejected")     # rejected mark kept
        self.assertEqual(a["rejections"], 3)           # Skipping + 429 wait + blocked
        self.assertEqual(a["rejections_unresolved"], 1)  # photo 100 never recovered
        self.assertEqual(a["status"], "banned")       # -> album stays banned
        self.assertEqual(a["errors"], 3)
        # 1.32.21 unquoted "Failed to download" still sets the raw status
        self.assertEqual(p1["status"], "downloaded")  # later CDN payload wins

    def test_wait_units_and_wake(self):
        ls = self.client.get("/api/stats").json()["last_sleep"]
        self.assertEqual(ls["seconds"], 7500.0)       # 2h 5min
        self.assertEqual(ls["reason"], "rate limit")
        self.assertEqual(ls["at"], "2026-12-01 10:16:00")
        wake = self.client.get("/api/summary").json()["wake_epoch"]
        self.assertAlmostEqual(wake, datetime(2026, 12, 1, 10, 16,
                                              tzinfo=timezone.utc).timestamp() + 7500)

    def test_counts_not_polluted_by_continuations(self):
        st = self.client.get("/api/stats").json()
        # the 4 continuation lines of the multi-line messages are not downloads
        self.assertEqual(st["downloads"], 0)
        self.assertEqual(st["errors"], 3)
        self.assertEqual(st["warnings"], 1)           # the "Retrying in" warning
        self.assertEqual(st["rejections"], 3)
        self.assertEqual(st["files"], 1)              # the CDN payload


class AlbumDoneByReorderedQueryTests(unittest.TestCase):
    """albums.txt `#` entries and logged `Starting DownloadJob` URLs can carry
    the same query params in a different order; canonical keying must still
    mark the album done (tree status, overview, summary)."""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        self._orig_log = open(LOG_FILE, encoding="utf-8").read()
        self._orig_albums = open(ALBUMS_FILE, encoding="utf-8").read()
        reordered = "https://www.facebook.com/photo/?fbid=28894425153536711&vanity=user42&set=a." + SET_ID
        canonical = "https://www.facebook.com/photo/?fbid=28894425153536711&set=a." + SET_ID + "&vanity=user42"
        lines = [
            "[2026-12-02 10:00:00] [gallery-dl][debug] Version 1.32.21",
            "[2026-12-02 10:00:01] [gallery-dl][debug] Starting DownloadJob for '" + reordered + "'",
        ]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write("# " + canonical + "\n")

    def tearDown(self):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig_log)
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig_albums)

    def test_done_across_views(self):
        t = self.client.get("/api/tree").json()
        a = t["sessions"][0]["albums"][0]
        self.assertEqual(a["status"], "done")
        albums = self.client.get("/api/albums").json()
        self.assertEqual([al["status"] for al in albums["albums"]], ["done"])
        # the reordered `Starting DownloadJob` URL must match the file entry
        self.assertTrue(albums["albums"][0]["in_logs"])
        self.assertEqual(albums["counts"]["not_in_file"], 0)
        ov = self.client.get("/api/albums_overview").json()
        self.assertEqual(ov["albums"][0]["status"], "done")
        sm = self.client.get("/api/summary").json()["albums"]
        self.assertEqual(sm["done"], 1)
        self.assertEqual(sm["unfinished"], 0)


class FreshnessAndDoneFallbackTests(unittest.TestCase):
    """Three robustness guarantees: (1) editing albums.txt (e.g. the download
    script writing '#') invalidates the tree cache even when the log is
    unchanged, (2) '#' detection falls back to the album's set id when host/
    scheme/extra params differ from the logged URL, (3) explicit tree
    renders re-scan the disk so files written since the last call no longer
    wait out FILES_CACHE_TTL."""

    def setUp(self):
        from fastapi.testclient import TestClient
        self.client = TestClient(app)
        self._orig_log = open(LOG_FILE, encoding="utf-8").read()
        self._orig_albums = open(ALBUMS_FILE, encoding="utf-8").read()

    def tearDown(self):
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig_log)
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write(self._orig_albums)
        clear_caches()

    def _write_log_started(self, url):
        lines = [
            "[2026-12-03 10:00:00] [gallery-dl][debug] Version 1.32.21",
            "[2026-12-03 10:00:01] [gallery-dl][debug] Starting DownloadJob for '" + url + "'",
        ]
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

    def test_albums_txt_edit_invalidates_tree_cache(self):
        self._write_log_started(SET_URL)
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write(SET_URL + "\n")          # not done yet
        self.assertEqual(self.client.get("/api/tree").json()["sessions"][0]
                         ["albums"][0]["status"], "incomplete")
        # the download script marks it done; the LOG is not touched
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write("# " + SET_URL + "\n")
        self.assertEqual(self.client.get("/api/tree").json()["sessions"][0]
                         ["albums"][0]["status"], "done")

    def test_done_matches_by_set_id_despite_url_variants(self):
        logged = (f"https://www.facebook.com/photo/?fbid=1&set=a.{SET_ID}"
                  "&vanity=user42&ref=page_internal")
        marked = f"http://m.facebook.com/photo/?fbid=1&set=a.{SET_ID}"
        self._write_log_started(logged)
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write("# " + marked + "\n")
        t = self.client.get("/api/tree").json()
        self.assertEqual(t["sessions"][0]["albums"][0]["status"], "done")
        ov = self.client.get("/api/albums_overview").json()
        self.assertEqual(ov["albums"][0]["status"], "done")

    def test_tree_render_uses_fresh_disk_scan(self):
        t = self.client.get("/api/tree").json()
        before = t["sessions"][0]["albums"][0]["states"].get("foreign", 0)
        new = os.path.join(ALBUM_DIR, "20260101_000000_111222333444555.jpg")
        from PIL import Image
        Image.new("RGB", (4, 4), (7, 7, 7)).save(new, "JPEG")
        try:
            t2 = self.client.get("/api/tree").json()
            after = t2["sessions"][0]["albums"][0]["states"].get("foreign", 0)
            self.assertEqual(after, before + 1)   # no TTL wait, no cache clear
        finally:
            os.remove(new)

    def test_non_url_comment_entry_warns(self):
        with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
            f.write("# that album is finished\n" + SET_URL + "\n")
        with self.assertLogs("fb-dl", level="WARNING"):
            self.client.get("/api/albums")
        # the comment line is not treated as a done album URL
        sm = self.client.get("/api/summary").json()["albums"]
        self.assertEqual(sm["total"], 1)


class FilesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_files_index_skips_logs_dir(self):
        d = self.client.get("/api/files").json()
        self.assertEqual(d["count"], 4)
        names = {f["name"] for f in d["files"]}
        self.assertEqual(names, {SYNCED_JPG, NOEXIF_JPG, MISMATCH_JPG, FOREIGN_JPG})
        expected_dates = {SYNCED_JPG: "2025-05-06 08:28:02", NOEXIF_JPG: "2025-05-06 08:28:03",
                          MISMATCH_JPG: "2025-05-06 08:28:05",
                          FOREIGN_JPG: "2025-05-06 08:28:06"}
        for f in d["files"]:
            self.assertEqual(f["album_set"], f"a.{SET_ID}")
            self.assertEqual(f["name_date"], expected_dates[f["name"]])
            self.assertRegex(f["fbid"], r"^\d{6,}$")
        # log-vs-disk: only the fbid with a CDN payload in the logs is "in_log" (dl)
        in_log = {f["name"]: f["in_log"] for f in d["files"]}
        self.assertEqual(in_log, {SYNCED_JPG: True, NOEXIF_JPG: False,
                                  MISMATCH_JPG: False, FOREIGN_JPG: False})
        self.assertEqual(d["in_log_count"], 1)
        # per-file state (file IS on disk): full / already / foreign
        states = {f["name"]: f["state"] for f in d["files"]}
        self.assertEqual(states, {SYNCED_JPG: "full", NOEXIF_JPG: "already",
                                  MISMATCH_JPG: "already", FOREIGN_JPG: "foreign"})

    def test_files_refresh(self):
        d = self.client.get("/api/files", params={"refresh": 1}).json()
        self.assertEqual(d["count"], 4)
        self.assertFalse(d["truncated"])

    def test_files_cache_ttl_and_cap(self):
        from PIL import Image as _Img
        extra = os.path.join(ALBUM_DIR, "20250506_082804_777666555444333.jpg")
        try:
            self.client.get("/api/files", params={"refresh": 1})  # populate cache (4 files)
            _Img.new("RGB", (8, 8)).save(extra, "JPEG")
            d = self.client.get("/api/files").json()             # cached: still 4
            self.assertEqual(d["count"], 4)
            old_ttl = app_module.FILES_CACHE_TTL
            app_module.FILES_CACHE_TTL = 0                        # TTL=0 -> rescan
            try:
                d = self.client.get("/api/files").json()
                self.assertEqual(d["count"], 5)                   # new file visible
                old_max = app_module.FILES_MAX
                app_module.FILES_MAX = 1
                try:
                    d = self.client.get("/api/files").json()
                    self.assertEqual(d["count"], 1)
                    self.assertTrue(d["truncated"])
                finally:
                    app_module.FILES_MAX = old_max
            finally:
                app_module.FILES_CACHE_TTL = old_ttl
        finally:
            os.remove(extra)
            self.client.get("/api/files", params={"refresh": 1})  # restore cache (count 4)

    def test_file_serving_and_traversal(self):
        r = self.client.get("/api/file", params={"path": SYNCED_REL})
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/jpeg", r.headers["content-type"])
        self.assertEqual(self.client.get("/api/file", params={"path": "../app.py"}).status_code, 404)
        self.assertEqual(self.client.get("/api/file", params={"path": "logs/fb-dl.log"}).status_code, 404)
        self.assertEqual(self.client.get("/api/file", params={"path": "nope.jpg"}).status_code, 404)

    def test_thumb_endpoint(self):
        r = self.client.get("/api/thumb", params={"path": SYNCED_REL, "w": 50})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "image/jpeg")
        from PIL import Image
        import io as _io
        with Image.open(_io.BytesIO(r.content)) as im:
            self.assertLessEqual(max(im.size), 50)
        self.assertLess(len(r.content), 20000)  # much smaller than the source
        self.assertEqual(self.client.get("/api/thumb", params={"path": "../app.py"}).status_code, 404)
        self.assertEqual(self.client.get("/api/thumb", params={"path": SYNCED_REL, "w": 10}).status_code, 422)
        self.assertEqual(self.client.get("/api/thumb", params={"path": "logs/fb-dl.log"}).status_code, 404)

    def test_exif_states(self):
        # EXIF interpreted in DISPLAY_TZ (Europe/Paris), not UTC: mtime was set
        # from the EXIF in local time -> synced (a UTC interpretation would be
        # off by exactly the tz offset -> the "everything is mismatch" bug)
        d = self.client.get("/api/exif", params={"path": SYNCED_REL}).json()
        self.assertEqual(d["state"], "synced")
        self.assertEqual(d["exif"], "2025:05:06 08:28:02")
        self.assertEqual(d["tz"], "Europe/Paris")
        self.assertEqual(d["name_date"], "2025-05-06 08:28:02")
        self.assertIsNotNone(d["diff_s"])
        self.assertLessEqual(abs(d["diff_s"]), 2)
        self.assertIsNotNone(d["exif_epoch"])
        mismatch_rel = MISMATCH_REL
        d = self.client.get("/api/exif", params={"path": mismatch_rel}).json()
        self.assertEqual(d["state"], "mismatch")
        self.assertAlmostEqual(d["diff_s"], 3600, places=1)
        d = self.client.get("/api/exif", params={"path": NOEXIF_REL}).json()
        self.assertEqual(d["state"], "no_exif")  # script did not run on this file
        self.assertIsNone(d["exif"])
        self.assertIsNone(d["diff_s"])
        self.assertEqual(self.client.get("/api/exif", params={"path": "../app.py"}).status_code, 404)

    def test_exif_stats_precompute(self):
        # force one full precompute pass (the background thread does the same
        # progressively in production)
        self.client.get("/api/files", params={"refresh": 1})
        with app_module._exif_lock:
            app_module._exif_cache.clear()
        app_module._exif_scan_pass(batch=10000)
        d = self.client.get("/api/exif_stats").json()
        self.assertEqual(d["tz"], "Europe/Paris")
        self.assertEqual(d["total"], 4)
        self.assertEqual(d["scanned"], 4)
        self.assertTrue(d["done"])
        self.assertEqual(d["global"], {"synced": 1, "mismatch": 1, "no_exif": 2})
        self.assertEqual(d["by_album"],
                         {f"a.{SET_ID}": {"synced": 1, "mismatch": 1, "no_exif": 2}})

    def test_export_json_attachment(self):
        r = self.client.get("/api/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        d = r.json()
        self.assertEqual(d["tree"]["runs"], 2)
        self.assertEqual(d["files"]["count"], 4)
        self.assertEqual(len(d["albums_overview"]["albums"]), 1)


if __name__ == "__main__":
    unittest.main()
