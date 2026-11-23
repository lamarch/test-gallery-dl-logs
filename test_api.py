import os
import tempfile
import unittest
from datetime import datetime, timezone

BASE = tempfile.mkdtemp()
LOG_FILE = os.path.join(BASE, "fb-dl.log")
ALBUMS_FILE = os.path.join(BASE, "albums.txt")
DOWNLOADS_DIR = os.path.join(BASE, "downloads")

SET_URL = "https://www.facebook.com/media/set?vanity=61580320494056&set=a.29493527130293174"
SET_ID = "29493527130293174"
ALBUM_DIR = os.path.join(DOWNLOADS_DIR, "facebook", "user42", f"album (a.{SET_ID})")
SYNCED_JPG = "20250506_082802_29493495376963016.jpg"
NOEXIF_JPG = "20250506_082803_29493523296960224.jpg"
SYNCED_REL = f"facebook/user42/album (a.{SET_ID})/{SYNCED_JPG}"
NOEXIF_REL = f"facebook/user42/album (a.{SET_ID})/{NOEXIF_JPG}"

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

_exif_ts = datetime(2025, 5, 6, 8, 28, 2, tzinfo=timezone.utc).timestamp()
_im = Image.new("RGB", (16, 16), (200, 100, 50))
_ex = Image.Exif()
_ex.get_ifd(0x8769)[36867] = "2025:05:06 08:28:02"  # DateTimeOriginal (Exif sub-IFD)
_im.save(os.path.join(ALBUM_DIR, SYNCED_JPG), "JPEG", exif=_ex)
os.utime(os.path.join(ALBUM_DIR, SYNCED_JPG), (_exif_ts, _exif_ts))  # script synced mtime
Image.new("RGB", (16, 16), (1, 2, 3)).save(os.path.join(ALBUM_DIR, NOEXIF_JPG), "JPEG")

os.environ["LOG_FILE"] = LOG_FILE
os.environ["ALBUMS_FILE"] = ALBUMS_FILE
os.environ["DOWNLOADS_DIR"] = DOWNLOADS_DIR
os.environ["DISPLAY_TZ"] = "Europe/Paris"

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
from app import REJECTION_RE, app  # noqa: E402


def clear_caches():
    for c in (app_module._stats_cache, app_module._search_cache,
              app_module._started_cache, app_module._tree_cache):
        c.update(key=None, value=None)


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
        self.assertIn("29493523083626912", data["lines"][-1])
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
        self.assertIn("29493523083626912", data["lines"][0])  # last 3 lines of the fixture
        self.assertIn("Adjusting filename", data["lines"][-1])
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
        self.assertEqual(data["count"], 3)
        data = self.client.get("/api/search", params={"q": ""}).json()
        self.assertEqual(data["count"], 0)

    def test_albums_status(self):
        data = self.client.get("/api/albums").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["counts"],
                         {"done": 1, "incomplete": 1, "not_started": 1, "total": 3})
        self.assertEqual(data["current"], SET_URL)
        by_url = {a["url"]: a["status"] for a in data["albums"]}
        self.assertEqual(by_url["https://example.com/done"], "done")
        self.assertEqual(by_url[SET_URL], "incomplete")
        self.assertEqual(by_url["https://example.com/never"], "not_started")

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
        self.assertFalse(data["truncated"])

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
        self.assertEqual(s0["albums"][0]["set_id"], SET_ID)
        self.assertEqual(s0["albums"][0]["vanity"], "61580320494056")
        self.assertEqual(s0["albums"][0]["link"],
                         f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")
        a0 = s0["albums"][0]
        self.assertEqual(a0["photos_n"], 1)
        self.assertEqual(a0["files"], 1)
        self.assertEqual(a0["bytes"], 392546)
        self.assertAlmostEqual(a0["sleeps"], 3607.52)
        self.assertEqual(a0["status"], "incomplete")
        p = a0["photos"][0]
        self.assertEqual(p["fbid"], "29493495376963016")
        self.assertEqual(p["status"], "downloaded")
        self.assertEqual(p["file"], "20250506_082802_29493495376963016.avif")
        self.assertEqual(p["bytes"], 392546)
        self.assertAlmostEqual(p["sleep"], 7.52)
        a1 = s1["albums"][0]
        self.assertEqual(a1["photos_n"], 2)
        self.assertEqual(a1["status"], "banned")
        self.assertEqual(a1["rejections"], 1)
        self.assertEqual(a1["errors"], 2)
        self.assertEqual(a1["photos"][0]["status"], "failed")
        # photo 1: downloaded via the CDN .jpg payload (200 + bytes), no Adjusting
        # line needed; the later Adjusting line only sets the filename (no double count)
        self.assertEqual(a1["photos"][1]["status"], "downloaded")
        self.assertEqual(a1["photos"][1]["bytes"], 54321)
        self.assertEqual(a1["photos"][1]["file"], "20250506_082802_29493523083626912.avif")
        self.assertEqual(a1["files"], 1)   # 1 CDN payload, not 2 "Adjusting" lines
        self.assertEqual(a1["bytes"], 54321)
        self.assertEqual(s1["files"], 1)

    def test_album_detail_merges_disk(self):
        d = self.client.get("/api/album/0/0").json()
        self.assertEqual(d["photos_n"], 1)
        self.assertEqual(len(d["photos"]), 1)
        p = d["photos"][0]
        self.assertTrue(p["on_disk"])
        self.assertEqual(p["path"], SYNCED_REL)
        self.assertEqual(p["name_date"], "2025-05-06 08:28:02")
        self.assertGreater(p["size"], 0)
        self.assertEqual(p["link_photo"],
                         f"https://www.facebook.com/photo/?fbid=29493495376963016&set=a.{SET_ID}")
        d1 = self.client.get("/api/album/1/0").json()
        self.assertEqual(d1["photos_n"], 2)
        by_fbid = {p["fbid"]: p for p in d1["photos"]}
        self.assertTrue(by_fbid["29493523296960224"]["on_disk"])   # noexif jpg exists on disk
        self.assertFalse(by_fbid["29493523083626912"]["on_disk"])  # not downloaded to disk

    def test_album_detail_pagination(self):
        d = self.client.get("/api/album/1/0", params={"offset": 0, "limit": 1}).json()
        self.assertEqual(len(d["photos"]), 1)
        self.assertEqual(d["photos_n"], 2)
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
            self.assertEqual(total_seen, 3)   # all photos still counted
            self.assertEqual(stored, 1)       # but only 1 materialized (RAM cap)
            self.assertTrue(any(a["photos_truncated"]
                                for s in t["sessions"] for a in s["albums"]))
            # album detail still returns ALL photos of a truncated album
            # (re-parsed from its log span)
            d = self.client.get("/api/album/1/0").json()
            self.assertEqual(d["photos_n"], 2)
            self.assertEqual(len(d["photos"]), 2)
            self.assertEqual({p["fbid"] for p in d["photos"]},
                             {"29493523296960224", "29493523083626912"})
            # the fallback also registers per-photo log spans
            r = self.client.get("/api/photo/1/0/0/logs").json()
            self.assertIn("fbid=29493523296960224", r["lines"][0])
        finally:
            app_module.TREE_MAX_PHOTOS = old
            clear_caches()
        d = self.client.get("/api/album/1/0").json()
        self.assertEqual(len(d["photos"]), 2)

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
        # status recomputed from aggregated counters: the album saw a rejection
        self.assertEqual(a["status"], "banned")
        # per-session details: one entry per session, with album_idx + photos
        sds = {d["session"]: d for d in a["session_details"]}
        self.assertEqual(sorted(sds), [0, 1])
        self.assertEqual(sds[0]["album_idx"], 0)
        self.assertEqual(sds[0]["photos_n"], 1)
        self.assertEqual(sds[0]["status"], "incomplete")
        self.assertEqual(sds[1]["photos_n"], 2)
        self.assertEqual(sds[1]["files"], 1)
        self.assertEqual(sds[1]["status"], "banned")
        self.assertIn("session_start", sds[0])
        self.assertIn("start", sds[1])
        self.assertEqual(a["sessions"], [0, 1])
        self.assertTrue(a["retried"])
        self.assertEqual(a["photos"], 3)
        self.assertEqual(a["files"], 2)            # 2 CDN payloads (L6 + L18)
        self.assertEqual(a["bytes"], 392546 + 54321)
        self.assertEqual(a["errors"], 2)
        self.assertEqual(a["rejections"], 1)
        self.assertEqual(a["on_disk"], 2)
        self.assertEqual(a["link"], f"https://www.facebook.com/media/set/?set=a.{SET_ID}&type=3")


class FilesApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_files_index_skips_logs_dir(self):
        d = self.client.get("/api/files").json()
        self.assertEqual(d["count"], 2)
        names = {f["name"] for f in d["files"]}
        self.assertEqual(names, {SYNCED_JPG, NOEXIF_JPG})
        expected_dates = {SYNCED_JPG: "2025-05-06 08:28:02", NOEXIF_JPG: "2025-05-06 08:28:03"}
        for f in d["files"]:
            self.assertEqual(f["album_set"], f"a.{SET_ID}")
            self.assertEqual(f["name_date"], expected_dates[f["name"]])
            self.assertRegex(f["fbid"], r"^\d{6,}$")

    def test_files_refresh(self):
        d = self.client.get("/api/files", params={"refresh": 1}).json()
        self.assertEqual(d["count"], 2)
        self.assertFalse(d["truncated"])

    def test_files_cache_ttl_and_cap(self):
        from PIL import Image as _Img
        extra = os.path.join(ALBUM_DIR, "20250506_082804_777666555444333.jpg")
        try:
            self.client.get("/api/files", params={"refresh": 1})  # populate cache (2 files)
            _Img.new("RGB", (8, 8)).save(extra, "JPEG")
            d = self.client.get("/api/files").json()             # cached: still 2
            self.assertEqual(d["count"], 2)
            old_ttl = app_module.FILES_CACHE_TTL
            app_module.FILES_CACHE_TTL = 0                        # TTL=0 -> rescan
            try:
                d = self.client.get("/api/files").json()
                self.assertEqual(d["count"], 3)                   # new file visible
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
            self.client.get("/api/files", params={"refresh": 1})  # restore cache (count 2)

    def test_file_serving_and_traversal(self):
        r = self.client.get("/api/file", params={"path": SYNCED_REL})
        self.assertEqual(r.status_code, 200)
        self.assertIn("image/jpeg", r.headers["content-type"])
        self.assertEqual(self.client.get("/api/file", params={"path": "../app.py"}).status_code, 404)
        self.assertEqual(self.client.get("/api/file", params={"path": "logs/fb-dl.log"}).status_code, 404)
        self.assertEqual(self.client.get("/api/file", params={"path": "nope.jpg"}).status_code, 404)

    def test_exif_synced_and_no_exif(self):
        d = self.client.get("/api/exif", params={"path": SYNCED_REL}).json()
        self.assertEqual(d["state"], "synced")
        self.assertEqual(d["exif"], "2025:05:06 08:28:02")
        self.assertEqual(d["name_date"], "2025-05-06 08:28:02")
        self.assertIsNotNone(d["diff_s"])
        self.assertLessEqual(abs(d["diff_s"]), 2)
        d = self.client.get("/api/exif", params={"path": NOEXIF_REL}).json()
        self.assertEqual(d["state"], "no_exif")  # script did not run on this file
        self.assertIsNone(d["exif"])
        self.assertIsNone(d["diff_s"])
        self.assertEqual(self.client.get("/api/exif", params={"path": "../app.py"}).status_code, 404)

    def test_export_json_attachment(self):
        r = self.client.get("/api/export")
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r.headers["content-disposition"])
        d = r.json()
        self.assertEqual(d["tree"]["runs"], 2)
        self.assertEqual(d["files"]["count"], 2)
        self.assertEqual(len(d["albums_overview"]["albums"]), 1)


if __name__ == "__main__":
    unittest.main()
