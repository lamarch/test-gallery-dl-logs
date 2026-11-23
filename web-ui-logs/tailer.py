import os


def read_tail(path, max_lines=500):
    """Return (lines, offset): the last `max_lines` complete lines of `path`
    (the whole file is scanned) and the byte offset right after the last
    complete line."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    if size == 0:
        return [], 0
    with open(path, "rb") as f:
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], 0
    offset = last_nl + 1
    complete = data[:last_nl]
    lines = complete.decode("utf-8", "replace").split("\n")
    return lines[-max_lines:], offset


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
