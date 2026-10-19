"""_tolerant_read.py — Tolerant, loud-failure JSONL reader for discovered.jsonl.

Read semantics required by the feed-fix WO:

  1. Per-line tolerant parse: each non-empty line is parsed independently.
     A malformed line is SKIPPED and LOGGED LOUDLY with (line_no, byte_offset,
     error). The bad line must never crash a discovery reader.

  2. Non-empty file yielding zero valid lines is a LOUD FAILURE, never a quiet
     empty result. This surfaces the NUL-corrupted-feed incident (2026-10-19)
     loudly instead of silently returning [].

  3. Byte offset tracking: the reader tracks the file offset of each line so
     the loud log can report the exact location of a corrupt line.

Usage:
    from _tolerant_read import iter_jsonl_entries
    for entry in iter_jsonl_entries("discovered.jsonl"):
        ...  # entry is a dict

    # Non-empty file with zero valid lines raises CorruptLineWarning.
"""

import json
import logging
import os

import _atomic_write

LOUD_FAIL_THRESHOLD = 1  # >=1 valid line = OK; 0 valid lines in non-empty file = loud fail

logger = logging.getLogger(__name__)


class CorruptLineWarning(RuntimeWarning):
    """Raised (or logged) when a line cannot be parsed as JSON."""


def iter_jsonl_bytes(path: str):
    """Yield (line_no, byte_offset, raw_line_bytes) for each line in the file.

    byte_offset is the byte offset of the FIRST byte of the line.
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


def iter_jsonl_entries(path: str):
    """Parse discovered.jsonl line-by-line.

    Yields only valid JSON objects (dicts). Bad lines are logged loudly and
    skipped. A non-empty file that yields zero valid lines raises CorruptLineWarning
    (loud failure).
    """
    if not os.path.exists(path):
        return
    file_size = os.path.getsize(path)
    if file_size == 0:
        return

    valid_count = 0
    last_error = None
    for line_no, byte_offset, raw_line in iter_jsonl_bytes(path):
        line_text = raw_line.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not line_text.strip():
            continue
        try:
            entry = json.loads(line_text)
        except json.JSONDecodeError as e:
            last_error = e
            # Loud, identifiable log — skip and continue.
            logger.warning(
                "corrupt_discovered_line line=%d offset=%d error=%s",
                line_no, byte_offset, e,
            )
            continue
        if isinstance(entry, dict):
            valid_count += 1
            yield entry

    # Non-empty file with zero valid lines = loud failure.
    if valid_count == 0 and file_size > 0:
        raise CorruptLineWarning(
            f"discovered.jsonl corrupted: non-empty file ({file_size} bytes) "
            f"yielded zero valid lines; last error: {last_error}"
        )
