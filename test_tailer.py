import os
import shutil
import tempfile
import unittest

from tailer import Follower, read_range, read_tail


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

    def test_large_file_whole_scan(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(2000):
                f.write(f"line {i}\n")
        lines, offset = read_tail(self.path, 500)
        self.assertEqual(len(lines), 500)
        self.assertEqual(lines[0], "line 1500")
        self.assertEqual(lines[-1], "line 1999")
        self.assertEqual(offset, os.path.getsize(self.path))


class ReadRangeTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.log")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, text):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def test_missing_file(self):
        self.assertEqual(read_range(self.path, 0, 10), ([], 0, True))

    def test_offset_beyond_eof(self):
        self.write("a\nb\n")
        size = os.path.getsize(self.path)
        self.assertEqual(read_range(self.path, 10**9, 10), ([], size, True))

    def test_chunked_reading(self):
        self.write("".join(f"l{i}\n" for i in range(10)))
        size = os.path.getsize(self.path)
        lines, off, eof = read_range(self.path, 0, 4)
        self.assertEqual(lines, ["l0", "l1", "l2", "l3"])
        self.assertEqual(off, 12)
        self.assertFalse(eof)
        lines, off, eof = read_range(self.path, off, 4)
        self.assertEqual(lines, ["l4", "l5", "l6", "l7"])
        self.assertFalse(eof)
        lines, off, eof = read_range(self.path, off, 4)
        self.assertEqual(lines, ["l8", "l9"])
        self.assertEqual(off, size)
        self.assertTrue(eof)

    def test_trailing_partial_line_included_at_eof(self):
        self.write("a\nb\npartial")
        lines, off, eof = read_range(self.path, 0, 100)
        self.assertEqual(lines, ["a", "b", "partial"])
        self.assertEqual(off, os.path.getsize(self.path))
        self.assertTrue(eof)

    def test_partial_line_not_returned_before_eof(self):
        self.write("abc")
        lines, off, eof = read_range(self.path, 0, 100)
        self.assertEqual(lines, ["abc"])
        self.assertTrue(eof)


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
