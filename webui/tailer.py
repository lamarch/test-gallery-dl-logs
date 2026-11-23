import os


def read_tail(path, max_lines=500):
    """Return (lines, offset): the last `max_lines` complete lines of `path`
    and the byte offset right after the last complete line."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return [], 0
    if size == 0:
        return [], 0
    window = 1024 * 1024
    start = max(0, size - window)
    with open(path, "rb") as f:
        f.seek(start)
        data = f.read()
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], 0
    offset = start + last_nl + 1
    complete = data[:last_nl]
    if start > 0:
        first_nl = complete.find(b"\n")
        complete = complete[first_nl + 1:] if first_nl != -1 else b""
    lines = complete.decode("utf-8", "replace").split("\n")
    return lines[-max_lines:], offset


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
