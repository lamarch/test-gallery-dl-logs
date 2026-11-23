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

    def test_logs_tail_and_offset(self):
        r = self.client.get("/api/logs", params={"tail": 2})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["path"], LOG_FILE)
        self.assertEqual(len(data["lines"]), 2)
        self.assertIn("AuthRequired", data["lines"][-1])
        self.assertEqual(data["offset"], os.path.getsize(LOG_FILE))
        self.assertTrue(data["eof"])
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
        self.assertEqual(len(data2["lines"]), 3)
        self.assertTrue(data2["eof"])

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
        by_url = {a["url"]: a["status"] for a in data["albums"]}
        self.assertEqual(by_url["https://example.com/done"], "done")
        self.assertEqual(by_url["https://example.com"], "incomplete")
        self.assertEqual(by_url["https://example.com/never"], "not_started")

    def test_stats_counts(self):
        data = self.client.get("/api/stats").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["lines"], 5)
        self.assertEqual(data["errors"], 3)
        self.assertEqual(data["warnings"], 0)
        self.assertEqual(data["jobs"], 1)
        self.assertEqual(data["downloads"], 1)
        self.assertEqual(data["skips"], 0)
        self.assertEqual(data["rejections"], 2)

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
                if events >= 5:
                    break
        self.assertEqual(events, 5)


if __name__ == "__main__":
    unittest.main()
