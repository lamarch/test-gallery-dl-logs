import os
import tempfile
import unittest

LOG_DIR = tempfile.mkdtemp()
LOG_FILE = os.path.join(LOG_DIR, "fb-dl.log")
with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write("[2026-11-22 18:40:12] [gallery-dl][debug] Starting DownloadJob for 'https://example.com'\n")
    f.write("[2026-11-22 18:40:44] [gallery-dl][error] Failed to download 'foo.jpg'\n")
    f.write("[%(asctime)s][%(levelname)s] %(name)s: %(message)s\n")
    f.write("[2026-11-22 19:00:00] [facebook][error] You've been temporarily blocked from viewing images.\n")
    f.write("[2026-11-22 19:00:01] [facebook][error] AuthRequired: cookies needed to access this profile\n")
    f.write("[2026-11-22 19:40:33] [facebook][debug] Sleeping 7.52 seconds (download)\n")
    f.write("[2026-11-22 19:40:41] [downloader.http][debug] Adjusting filename extension of 'a.avif' to 'jpg'\n")
    f.write("[2026-11-22 19:40:42] [downloader.http][debug] Adjusting filename extension of 'b.avif' to 'jpg'\n")

ALBUMS_FILE = os.path.join(LOG_DIR, "albums.txt")
with open(ALBUMS_FILE, "w", encoding="utf-8") as f:
    f.write("# https://example.com/done\n")
    f.write("https://example.com\n")
    f.write("https://example.com/never\n")

os.environ["LOG_FILE"] = LOG_FILE
os.environ["ALBUMS_FILE"] = ALBUMS_FILE

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_index_serves_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn(b"<title>", r.content)

    def test_favicon_served(self):
        r = self.client.get("/static/favicon.svg")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<svg", r.content)

    def test_logs_tail_and_offsets(self):
        r = self.client.get("/api/logs", params={"tail": 2})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["path"], LOG_FILE)
        self.assertEqual(len(data["lines"]), 2)
        self.assertIn("b.avif", data["lines"][-1])
        self.assertEqual(data["offset"], os.path.getsize(LOG_FILE))
        self.assertTrue(data["eof"])
        self.assertFalse(data["has_more"])
        self.assertEqual(data["start_offset"], 0)
        self.assertIn("max_render", data)

    def test_logs_range_from_beginning(self):
        r = self.client.get("/api/logs", params={"offset": 0, "limit": 2})
        data = r.json()
        self.assertEqual(data["lines"][0],
                         "[2026-11-22 18:40:12] [gallery-dl][debug] Starting DownloadJob for 'https://example.com'")
        self.assertEqual(len(data["lines"]), 2)
        self.assertFalse(data["eof"])
        r2 = self.client.get("/api/logs", params={"offset": data["offset"], "limit": 10})
        data2 = r2.json()
        self.assertEqual(len(data2["lines"]), 6)
        self.assertTrue(data2["eof"])

    def test_logs_before(self):
        size = os.path.getsize(LOG_FILE)
        r = self.client.get("/api/logs", params={"before": size, "limit": 3})
        data = r.json()
        self.assertEqual(len(data["lines"]), 3)
        self.assertIn("Sleeping", data["lines"][0])
        self.assertIn("b.avif", data["lines"][-1])
        self.assertTrue(data["has_more"])
        self.assertLess(data["start_offset"], size)
        r2 = self.client.get("/api/logs", params={"before": data["start_offset"], "limit": 100})
        data2 = r2.json()
        self.assertEqual(len(data2["lines"]), 5)
        self.assertFalse(data2["has_more"])
        self.assertEqual(data2["start_offset"], 0)

    def test_search_whole_file(self):
        data = self.client.get("/api/search", params={"q": "failed"}).json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["shown"], 1)
        self.assertIn("Failed to download", data["lines"][0])
        data = self.client.get("/api/search", params={"q": ""}).json()
        self.assertEqual(data["count"], 0)

    def test_albums_status(self):
        data = self.client.get("/api/albums").json()
        self.assertTrue(data["available"])
        self.assertEqual(data["counts"],
                         {"done": 1, "incomplete": 1, "not_started": 1, "total": 3})
        self.assertEqual(data["current"], "https://example.com")
        by_url = {a["url"]: a["status"] for a in data["albums"]}
        self.assertEqual(by_url["https://example.com/done"], "done")
        self.assertEqual(by_url["https://example.com"], "incomplete")
        self.assertEqual(by_url["https://example.com/never"], "not_started")

    def test_stats_counts(self):
        data = self.client.get("/api/stats").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["lines"], 8)
        self.assertEqual(data["errors"], 3)
        self.assertEqual(data["warnings"], 0)
        self.assertEqual(data["jobs"], 1)
        self.assertEqual(data["downloads"], 1)
        self.assertEqual(data["skips"], 0)
        self.assertEqual(data["rejections"], 2)
        self.assertEqual(data["files"], 2)
        self.assertEqual(data["current_album"], "https://example.com")
        self.assertEqual(data["current_album_files"], 2)
        self.assertEqual(data["last_sleep"],
                         {"seconds": 7.52, "reason": "download", "at": "2026-11-22 19:40:33"})
        self.assertFalse(data["truncated"])

    def test_stats_scan_cap(self):
        import app as app_module
        old = app_module.SCAN_MAX_BYTES
        app_module._stats_cache.update(key=None, value=None)
        app_module.SCAN_MAX_BYTES = 60
        try:
            data = self.client.get("/api/stats").json()
            self.assertTrue(data["truncated"])
            self.assertLess(data["scanned_bytes"], os.path.getsize(LOG_FILE))
            self.assertLess(data["lines"], 8)
        finally:
            app_module.SCAN_MAX_BYTES = old
            app_module._stats_cache.update(key=None, value=None)

    def test_status(self):
        data = self.client.get("/api/status").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["size"], os.path.getsize(LOG_FILE))

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
                if events >= 8:
                    break
        self.assertEqual(events, 8)


if __name__ == "__main__":
    unittest.main()
