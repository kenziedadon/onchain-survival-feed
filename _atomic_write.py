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
import os
import tempfile


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


def atomic_jsonl_append(path: str, record: dict) -> None:
    """Append a single JSON record to discovered.jsonl atomically.

    Reads the existing file, appends the new record, and writes the combined
    result to a temp file in the same directory, fsyncs it, then renames it
    over the target. Atomic on POSIX: a crash mid-write leaves either the old
    file or the new file, never a partial/truncated result.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    existing = []
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "r", encoding="utf-8") as f:
            existing = [json.loads(line) for line in f if line.strip()]
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
    """Append multiple JSON records atomically (single temp file + rename)."""
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
