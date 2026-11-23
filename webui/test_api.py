import os
import tempfile
import unittest

LOG_DIR = tempfile.mkdtemp()
LOG_FILE = os.path.join(LOG_DIR, "fb-dl.log")
with open(LOG_FILE, "w", encoding="utf-8") as f:
    f.write("[2026-11-22 18:40:12] [gallery-dl][debug] Starting DownloadJob for 'https://example.com'\n")
    f.write("[2026-11-22 18:40:44] [gallery-dl][error] Failed to download 'foo.jpg'\n")
    f.write("[%(asctime)s][%(levelname)s] %(name)s: %(message)s\n")

os.environ["LOG_FILE"] = LOG_FILE

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
        self.assertEqual(data["lines"][-1],
                         "[%(asctime)s][%(levelname)s] %(name)s: %(message)s")
        self.assertEqual(data["offset"], os.path.getsize(LOG_FILE))

    def test_stats_counts(self):
        data = self.client.get("/api/stats").json()
        self.assertTrue(data["exists"])
        self.assertEqual(data["lines"], 3)
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["warnings"], 0)
        self.assertEqual(data["jobs"], 1)
        self.assertEqual(data["downloads"], 1)
        self.assertEqual(data["skips"], 0)

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
                if events >= 3:
                    break
        self.assertEqual(events, 3)


if __name__ == "__main__":
    unittest.main()
