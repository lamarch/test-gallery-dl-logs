import os

DEFAULT_WINDOW = 4 * 1024 * 1024


def read_tail(path, max_lines=500, window=DEFAULT_WINDOW):
    """Return (lines, end_offset, start_offset): the last `max_lines` complete
    lines of `path` (scanning at most the last `window` bytes), the byte offset
    right after the last complete line, and the byte offset of the first
    returned line (0 when the whole file fit in the window)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0, 0
    if size == 0:
        return [], 0, 0
    start = max(0, size - window)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], 0, 0
    end_offset = start + last_nl + 1
    complete = data[:last_nl]
    start_offset = start
    if start > 0:
        first_nl = complete.find(b"\n")
        if first_nl == -1:
            return [], 0, 0
        complete = complete[first_nl + 1:]
        start_offset = start + first_nl + 1
    lines = complete.decode("utf-8", "replace").split("\n")
    return lines[-max_lines:], end_offset, start_offset


def read_range(path, offset=0, max_lines=5000, max_bytes=4 * 1024 * 1024):
    """Read up to `max_lines` complete lines starting at byte `offset`.

    Return (lines, new_offset, eof). A trailing partial line is only
    returned when the end of file is reached."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0, True
    if offset >= size:
        return [], size, True
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(max_bytes)
    at_file_end = offset + len(data) >= size
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        if at_file_end and data:
            return [data.decode("utf-8", "replace")], size, True
        return [], offset, False
    raw = data[:last_nl].split(b"\n")
    if len(raw) > max_lines:
        raw = raw[:max_lines]
        new_offset = offset + sum(len(b) + 1 for b in raw)
        eof = new_offset >= size
    else:
        new_offset = offset + last_nl + 1
        eof = at_file_end
        if eof and last_nl < len(data) - 1:
            raw.append(data[last_nl + 1:])
            new_offset = size
    lines = [b.decode("utf-8", "replace") for b in raw]
    return lines, new_offset, eof


def read_range_back(path, end_offset, max_lines=2000, window=DEFAULT_WINDOW):
    """Read up to `max_lines` complete lines ending at byte `end_offset`.

    Return (lines, start_offset, has_more)."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0, False
    end = min(end_offset, size)
    start = max(0, end - window)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read(end - start)
    if not data:
        return [], start, False
    if start > 0:
        nl = data.find(b"\n")
        if nl == -1:
            return [], start, False
        data = data[nl + 1:]
        start += nl + 1
    raw = data.split(b"\n")
    if raw and raw[-1] == b"":
        raw.pop()
    if len(raw) > max_lines:
        raw = raw[-max_lines:]
        start_offset = end - sum(len(b) + 1 for b in raw)
    else:
        start_offset = start
    lines = [b.decode("utf-8", "replace") for b in raw]
    return lines, start_offset, start_offset > 0


class Follower:
    """Incrementally read complete lines appended to a file, starting at
    a byte offset. Handles truncation/rotation (file shrinking)."""

    def __init__(self, path, offset=0):
        self.path = path
        self.pos = offset
        self.pending = ""

    def poll(self):
        """Return the list of complete lines appended since the last call."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if size < self.pos:
            self.pos = 0
            self.pending = ""
        if size <= self.pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.pos)
            chunk = f.read()
        self.pos += len(chunk)
        text = self.pending + chunk.decode("utf-8", "replace")
        parts = text.split("\n")
        self.pending = parts.pop()
        return parts
