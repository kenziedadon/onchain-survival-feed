"""_atomic_write.py — Atomic JSONL write utility for discovered.jsonl.

Root-cause of the 2026-10-19 NUL corruption:
  discovered.jsonl was written with a bare open(path, "a") append + f.write()
  nowhere near synchronous. When the discovery re-run was interrupted (kill -9
  / process crash / OOM), the kernel truncated the final write: the file
  retained the prior complete line plus a run of NUL padding where the partial
  payload was never flushed and the file offset was not returned to a
  consistent state.

  Evidence (file offset 3,270,564, 56 consecutive NUL bytes):
    {"ts":"2026-10-19T19:36:36.807052+00:00", "coins": []}\n
    \x00\x00\x00... (56 NUL bytes)

  Root cause: the writer does not use atomic rename; the writer did not fsync
  the parent directory; and the reader does not reject partial lines, so a
  single interrupted write silently poisons the feed.

Fix: every writer appends via a temp file in the same directory, fsyncs it, and
renames it over the target (atomic on POSIX). Readers tolerate a bad line,
skip it, and log loudly (line no., byte offset, error). A non-empty file that
yields zero valid lines is a loud failure, never a quiet empty result.
"""

import json
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

def _fsync_dir(directory: str) -> None:
    """Best-effort directory fsync (POSIX). Deliberately non-fatal."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception:
        pass


def _iter_jsonl_bytes_bytes(path: str):
    """Yield (line_no, byte_offset, raw_line_bytes) for each line in the file.

    byte_offset is the byte offset of the FIRST byte of the line (0-indexed).
    Fast byte-level iterator; no JSON parsing. Used by the tolerant read path.
    """
    offset = 0
    line_no = 0
    with open(path, "rb") as f:
        while True:
            line = f.readline()
            if not line:
                break
            line_no += 1
            yield line_no, offset, line
            offset += len(line)


def atomic_jsonl_append(path: str, record: dict) -> None:
    """Append a single JSON record to discovered.jsonl atomically.

    Tolerates a corrupted existing file: bad lines are skipped (logged loudly,
    one at a time) instead of crashing the whole run. Reads the existing file,
    appends the new record, and writes the combined result to a temp file in
    the same directory, fsyncs it, then renames it over the target. Atomic on
    POSIX: a crash mid-write leaves either the old file or the new file,
    never a partial/truncated result.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    existing = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        # Tolerant read: skip + log loudly on bad lines.
        for line_no, _offset, raw_line in _iter_jsonl_bytes_bytes(path):
            line_text = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line_text.strip():
                continue
            try:
                existing.append(json.loads(line_text))
            except json.JSONDecodeError as e:
                logger.warning("atomic_jsonl_append: corrupt_line_no=%d error=%s", line_no, e)
    existing.append(record)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".discovered-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in existing:
                json.dump(rec, f)
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_jsonl_append_records(path: str, records: list[dict]) -> None:
    """Append multiple JSON records atomically.

    Tolerates a corrupted existing file: bad lines are skipped (logged loudly,
    one at a time) instead of crashing the whole run. Reads the existing file,
    appends the new records, and writes the combined result to a temp file in
    the same directory, fsyncs it, then renames it over the target. Atomic on
    POSIX: a crash mid-write leaves either the old file or the new file,
    never a partial/truncated result. Unlike atomic_jsonl_write, this does
    NOT replace existing valid records — it appends.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    existing = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        # Tolerant read: skip + log loudly on bad lines.
        for line_no, _offset, raw_line in _iter_jsonl_bytes_bytes(path):
            line_text = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
            if not line_text.strip():
                continue
            try:
                existing.append(json.loads(line_text))
            except json.JSONDecodeError as e:
                logger.warning("atomic_jsonl_append_records: corrupt_line_no=%d error=%s", line_no, e)
    existing.extend(records)

    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".discovered-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in existing:
                json.dump(rec, f)
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_jsonl_write(path: str, records: list[dict]) -> None:
    """Atomically replace the entire discovered.jsonl with the given records."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".discovered-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for rec in records:
                json.dump(rec, f)
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_jsonl_strings_write(path: str, lines: list[str]) -> None:
    """Atomically replace a JSONL file with the given raw lines (strings)."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".discovered-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for line in lines:
                f.write(line + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
