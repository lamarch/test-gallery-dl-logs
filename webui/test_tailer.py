import os
import shutil
import tempfile
import unittest

from tailer import Follower, read_tail


class ReadTailTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file(self):
        self.assertEqual(read_tail(self.path, 10), ([], 0))

    def test_empty_file(self):
        self.write("")
        self.assertEqual(read_tail(self.path, 10), ([], 0))

    def test_tail_and_offset(self):
        self.write("aaa\nbbb\nccc\n")
        lines, offset = read_tail(self.path, 2)
        self.assertEqual(lines, ["bbb", "ccc"])
        self.assertEqual(offset, os.path.getsize(self.path))

    def test_no_trailing_newline(self):
        self.write("aaa\nbbb\nccc")
        lines, offset = read_tail(self.path, 10)
        self.assertEqual(lines, ["aaa", "bbb"])
        self.assertEqual(offset, 8)

    def test_no_newline_at_all(self):
        self.write("onlypartial")
        self.assertEqual(read_tail(self.path, 10), ([], 0))

    def test_large_file_window(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(2000):
                f.write(f"line {i}\n")
        lines, offset = read_tail(self.path, 500)
        self.assertEqual(len(lines), 500)
        self.assertEqual(lines[0], "line 1500")
        self.assertEqual(lines[-1], "line 1999")
        self.assertEqual(offset, os.path.getsize(self.path))


class FollowerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.log")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("one\ntwo\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_poll_appends(self):
        f = Follower(self.path, offset=os.path.getsize(self.path))
        self.assertEqual(f.poll(), [])
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write("three\n")
        self.assertEqual(f.poll(), ["three"])

    def test_partial_line(self):
        f = Follower(self.path, offset=os.path.getsize(self.path))
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write("part")
        self.assertEqual(f.poll(), [])
        with open(self.path, "a", encoding="utf-8") as fp:
            fp.write("ial\n")
        self.assertEqual(f.poll(), ["partial"])

    def test_truncation_resets(self):
        f = Follower(self.path, offset=os.path.getsize(self.path))
        with open(self.path, "w", encoding="utf-8") as fp:
            fp.write("fresh\n")
        self.assertEqual(f.poll(), ["fresh"])

    def test_missing_file(self):
        os.remove(self.path)
        f = Follower(self.path, offset=100)
        self.assertEqual(f.poll(), [])


if __name__ == "__main__":
    unittest.main()
