import os
import shutil
import tempfile
import unittest

from tailer import Follower, read_range, read_range_back, read_span, read_tail


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
        self.assertEqual(read_tail(self.path, 10), ([], 0, 0))

    def test_empty_file(self):
        self.write("")
        self.assertEqual(read_tail(self.path, 10), ([], 0, 0))

    def test_tail_and_offsets(self):
        self.write("aaa\nbbb\nccc\n")
        lines, end, start = read_tail(self.path, 2)
        self.assertEqual(lines, ["bbb", "ccc"])
        self.assertEqual(end, os.path.getsize(self.path))
        # truncated by max_lines: start_offset = first KEPT line, not 0
        self.assertEqual(start, 4)

    def test_no_trailing_newline(self):
        self.write("aaa\nbbb\nccc")
        lines, end, start = read_tail(self.path, 10)
        self.assertEqual(lines, ["aaa", "bbb"])
        self.assertEqual(end, 8)
        self.assertEqual(start, 0)

    def test_no_newline_at_all(self):
        self.write("onlypartial")
        self.assertEqual(read_tail(self.path, 10), ([], 0, 0))

    def test_window_limits_scan(self):
        self.write("aaa\nbbb\nccc\n")  # 12 bytes
        lines, end, start = read_tail(self.path, 5, window=10)
        self.assertEqual(lines, ["bbb", "ccc"])
        self.assertEqual(end, 12)
        self.assertEqual(start, 4)

    def test_large_file(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(2000):
                f.write(f"line {i}\n")
        lines, end, start = read_tail(self.path, 500)
        self.assertEqual(len(lines), 500)
        self.assertEqual(lines[0], "line 1500")
        self.assertEqual(lines[-1], "line 1999")
        self.assertEqual(end, os.path.getsize(self.path))
        # truncated by max_lines (whole file fits in the window):
        # start_offset must point at the first KEPT line, not at 0
        self.assertGreater(start, 0)
        self.assertEqual(start, end - sum(len(l) + 1 for l in lines))

    def test_truncated_tail_continuity(self):
        # "load earlier" pages backwards from start_offset; paging forward from
        # start_offset must resume exactly at the first tail line
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(2000):
                f.write(f"line {i}\n")
        lines, end, start = read_tail(self.path, 500)
        fwd, off, eof = read_range(self.path, start, 5)
        self.assertEqual(fwd[0], lines[0])
        self.assertEqual(fwd[:5], lines[:5])
        self.assertFalse(eof)

    def test_window_and_max_lines_truncation(self):
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(2000):
                f.write(f"line {i}\n")
        size = os.path.getsize(self.path)
        lines, end, start = read_tail(self.path, 100, window=4000)
        self.assertEqual(len(lines), 100)
        self.assertEqual(lines[-1], "line 1999")
        self.assertEqual(end, size)
        self.assertGreater(start, 0)
        self.assertEqual(start, end - sum(len(l) + 1 for l in lines))
        fwd, _, _ = read_range(self.path, start, 3)
        self.assertEqual(fwd, lines[:3])


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

    def test_giant_line_never_blocks_paging(self):
        # a single 2.5 MiB line without newline, then a normal line
        with open(self.path, "wb") as f:
            f.write(b"x" * (5 * 1024 * 1024 // 2) + b"\nafter\n")
        size = os.path.getsize(self.path)
        offset = 0
        seen_eof = False
        for _ in range(10):  # must terminate well before this
            lines, offset, eof = read_range(self.path, offset, 10, max_bytes=1024 * 1024)
            if eof:
                seen_eof = True
                break
            self.assertGreater(offset, 0)
        self.assertTrue(seen_eof)
        self.assertEqual(offset, size)
        self.assertEqual(lines[-1], "after")


class ReadRangeBackTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.log")
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(f"l{i}\n")  # 30 bytes total

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_file(self):
        self.assertEqual(read_range_back(self.dir + "/nope.log", 100, 10), ([], 0, False))

    def test_backwards_chunks(self):
        size = os.path.getsize(self.path)
        lines, start, has_more = read_range_back(self.path, size, 4)
        self.assertEqual(lines, ["l6", "l7", "l8", "l9"])
        self.assertEqual(start, 18)
        self.assertTrue(has_more)
        lines, start, has_more = read_range_back(self.path, start, 4)
        self.assertEqual(lines, ["l2", "l3", "l4", "l5"])
        self.assertEqual(start, 6)
        self.assertTrue(has_more)
        lines, start, has_more = read_range_back(self.path, start, 4)
        self.assertEqual(lines, ["l0", "l1"])
        self.assertEqual(start, 0)
        self.assertFalse(has_more)

    def test_end_beyond_size_is_clamped(self):
        lines, start, has_more = read_range_back(self.path, 10**9, 100)
        self.assertEqual(len(lines), 10)
        self.assertEqual(start, 0)
        self.assertFalse(has_more)


class ReadSpanTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "test.log")
        # 10 lines "l0".."l9", 3 bytes each = 30 bytes total
        with open(self.path, "w", encoding="utf-8") as f:
            for i in range(10):
                f.write(f"l{i}\n")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_file(self):
        self.assertEqual(read_span(self.dir + "/nope.log", 0, 100), [])

    def test_full_file(self):
        self.assertEqual(read_span(self.path, 0, 30),
                         [f"l{i}" for i in range(10)])

    def test_middle_span(self):
        # bytes 6..18 -> lines l2..l5 (spans are byte ranges at line boundaries)
        self.assertEqual(read_span(self.path, 6, 18), ["l2", "l3", "l4", "l5"])

    def test_clamped_to_size(self):
        self.assertEqual(read_span(self.path, 24, 10**9), ["l8", "l9"])
        self.assertEqual(read_span(self.path, 10**9, 10**9 + 5), [])

    def test_end_mid_line_includes_partial(self):
        # byte range ending inside line l5 ("l5\n" = bytes 15..17)
        self.assertEqual(read_span(self.path, 15, 17), ["l5"])
        self.assertEqual(read_span(self.path, 15, 18), ["l5"])  # trailing \n popped


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

    def test_rotation_by_rename_resets(self):
        # simulate logrotate rename: new file (new inode) LARGER than the old
        # offset — must reset to 0, not read misaligned from the old pos
        f = Follower(self.path, offset=os.path.getsize(self.path))
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fp:
            for i in range(50):
                fp.write(f"fresh-line-{i:02d}\n")
        os.replace(tmp, self.path)
        got = f.poll()
        self.assertEqual(got[0], "fresh-line-00")
        self.assertEqual(len(got), 50)


if __name__ == "__main__":
    unittest.main()
