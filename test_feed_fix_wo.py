"""Tests for the feed-fix WO (discovered.jsonl corruption fix).

Covers:
  1. Corruption fixture: old reader fails with production JSONDecodeError;
     fixed tolerant reader skips bad line + logs loudly.
  2. Non-empty file with zero valid lines = loud failure.
  3. Writer interruption test (atomic write resilience).
"""

import json
import logging
import os
import tempfile
import unittest
import warnings

import _atomic_write
import _tolerant_read

# Capture logger output for tests
_test_handler = logging.StreamHandler()
_test_handler.setLevel(logging.WARNING)
_tolerant_read.logger.addHandler(_test_handler)
_tolerant_read.logger.setLevel(logging.WARNING)


class TestTolerantRead(unittest.TestCase):
    """Reader tests — corrupt lines are skipped + logged loudly."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "discovered.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _write_valid(self, entries):
        with open(self.path, "w") as f:
            for e in entries:
                json.dump(e, f)
                f.write("\n")

    def test_corrupt_line_skipped_and_logged(self):
        """A bad line must be skipped and logged loudly; valid lines preserved."""
        valid = [{"ts": "2026-10-19T00:00:00+00:00", "coins": [{"name": "A", "score": 10}]}]
        bad_line = '{"ts": "2026-10-19T00:00:00+00:00", "coins": [broken json'
        with open(self.path, "w") as f:
            json.dump(valid[0], f)
            f.write("\n")
            f.write(bad_line)
            f.write("\n")

        # Capture log records
        with self.assertLogs(_tolerant_read.logger, level="WARNING") as cm:
            entries = list(_tolerant_read.iter_jsonl_entries(self.path))

        # Should have logged at least one corrupt-line warning
        corrupt_logs = [r for r in cm.output if "corrupt_discovered_line" in r]
        self.assertTrue(len(corrupt_logs) >= 1, f"No corrupt line logged; got: {cm.output}")
        # Valid line preserved
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["coins"][0]["name"], "A")

    def test_non_empty_file_zero_valid_raises_loud(self):
        """A non-empty file with zero valid lines must raise loudly, not silently []."""
        with open(self.path, "w") as f:
            f.write('not json\n')
            f.write('also not json\n')

        with self.assertRaises(_tolerant_read.CorruptLineWarning) as cm:
            list(_tolerant_read.iter_jsonl_entries(self.path))
        self.assertIn("zero valid lines", str(cm.exception))

    def test_empty_file_returns_empty(self):
        """An empty file yields no entries, no exception."""
        open(self.path, "w").close()
        entries = list(_tolerant_read.iter_jsonl_entries(self.path))
        self.assertEqual(entries, [])

    def test_missing_file_returns_empty(self):
        """A missing file yields no entries, no exception."""
        missing = os.path.join(self.tmpdir, "missing.jsonl")
        entries = list(_tolerant_read.iter_jsonl_entries(missing))
        self.assertEqual(entries, [])

    def test_byte_offset_tracking(self):
        """The reader must log byte offsets for corrupt lines."""
        with open(self.path, "w") as f:
            f.write(json.dumps({"ts": "2026-01-01", "coins": []}) + "\n")
            f.write("BAD LINE" + "\n")
            f.write(json.dumps({"ts": "2026-01-02", "coins": []}) + "\n")

        with self.assertLogs(_tolerant_read.logger, level="WARNING") as cm:
            list(_tolerant_read.iter_jsonl_entries(self.path))
        corrupt_logs = [r for r in cm.output if "corrupt_discovered_line" in r]
        self.assertTrue(len(corrupt_logs) >= 1, f"No corrupt line logged; got: {cm.output}")
        msg = corrupt_logs[0]
        self.assertIn("offset=", msg)

    def test_reader_minors(self):
        """Reader minors: skip blank lines, handle trailing newline, decode errors."""
        # Valid line + blank line + valid line; should return only valid entries
        with open(self.path, "w") as f:
            f.write(json.dumps({"ts": "t1", "coins": []}) + "\n")
            f.write("\n")
            f.write(json.dumps({"ts": "t2", "coins": []}) + "\n\n")
        entries = list(_tolerant_read.iter_jsonl_entries(self.path))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["ts"], "t1")
        self.assertEqual(entries[1]["ts"], "t2")

        # Binary garbage in one line must not crash; other lines still parse
        with open(self.path, "w") as f:
            f.write(json.dumps({"ts": "t1", "coins": []}) + "\n")
            f.write(b"\x00\x01\x02BAD\xfe\xfc".decode("latin-1") + "\n")
            f.write(json.dumps({"ts": "t2", "coins": []}) + "\n")
        with self.assertLogs(_tolerant_read.logger, level="WARNING") as cm:
            entries = list(_tolerant_read.iter_jsonl_entries(self.path))
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["ts"], "t1")
        self.assertEqual(entries[1]["ts"], "t2")


class TestAtomicWrite(unittest.TestCase):
    """Writer tests — atomic writes survive interruption."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "discovered.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_on_nul_corrupted_fixture_does_not_crash(self):
        """Appending to the NUL-corrupted production fixture must NOT crash.

        This is the exact input the feed-fix WO exists for: valid line + 56 NUL bytes.
        The tolerant read must skip the NUL line and continue.
        """
        # Reproduce the NUL corruption
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ts": "2026-10-19T19:36:36.807052+00:00", "coins": []}, f)
            f.write("\n")
            f.write("\x00" * 56)

        # Previous state: 1 valid line + NUL corruption
        before = self.path
        _atomic_write.atomic_jsonl_append(before, {"ts": "t_after", "coins": []})

        with open(before) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        # Should have 2 valid lines: the original + the appended
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["ts"], "2026-10-19T19:36:36.807052+00:00")
        self.assertEqual(lines[1]["ts"], "t_after")
        # The appended record must appear as the last line
        self.assertEqual(lines[-1]["coins"], [])

    def test_append_records_preserves_existing(self):
        """append_records reads and preserves existing records.

        t1 + t2 appended; then t3; result must be [t1, t2, t3] — not overwritten.
        """
        _atomic_write.atomic_jsonl_append(self.path, {"ts": "t1", "coins": []})
        _atomic_write.atomic_jsonl_append_records(self.path, [{"ts": "t2", "coins": []}, {"ts": "t3", "coins": []}])

        with open(self.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0]["ts"], "t1")
        self.assertEqual(lines[1]["ts"], "t2")
        self.assertEqual(lines[2]["ts"], "t3")

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "discovered.jsonl")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_append_preserves_existing(self):
        """Appending a second record leaves the first record intact."""
        _atomic_write.atomic_jsonl_append(self.path, {"ts": "t1", "coins": []})
        _atomic_write.atomic_jsonl_append(self.path, {"ts": "t2", "coins": []})
        with open(self.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0]["ts"], "t1")
        self.assertEqual(lines[1]["ts"], "t2")

    def test_write_replaces_entire_file(self):
        """atomic_jsonl_write replaces the whole file atomically."""
        with open(self.path, "w") as f:
            f.write("old line\n")
        _atomic_write.atomic_jsonl_write(self.path, [{"ts": "t1", "coins": []}])
        with open(self.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ts"], "t1")

    def test_interrupt_does_not_corrupt(self):
        """A crash during write leaves either old or new, never partial."""
        _atomic_write.atomic_jsonl_write(self.path, [{"ts": "t1", "coins": []}])
        # Simulate an interruption during a write by calling the write
        # function that raises mid-way (proxy: fake FS that fails).
        import _atomic_write as aw
        original_replace = aw.os.replace
        fail = False
        def failing_replace(src, dst):
            nonlocal fail
            fail = True
            raise OSError("simulated crash")
        aw.os.replace = failing_replace
        try:
            # Second write should not corrupt the first (old) file.
            aw.atomic_jsonl_append(self.path, {"ts": "t2", "coins": []})
        except OSError:
            pass  # expected — the test simulates a crash
        finally:
            aw.os.replace = original_replace
        # File should still contain only the first entry (old data, no partial).
        with open(self.path) as f:
            lines = [json.loads(l) for l in f if l.strip()]
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["ts"], "t1")


class TestCorruptionFixture(unittest.TestCase):
    """Production JSONDecodeError must be raised by old reader, fixed reader skips."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, "discovered.jsonl")
        # Reproduce the NUL-corruption incident: valid line + NUL bytes
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"ts": "2026-10-19T19:36:36.807052+00:00", "coins": []}, f)
            f.write("\n")
            f.write("\x00" * 56)  # the 56-byte NUL run from Oct 19

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_old_reader_still_fails_with_json_decoding_error(self):
        """The OLD reader (json.loads on raw lines) raises JSONDecodeError."""
        with open(self.path, "r") as f:
            lines = f.readlines()
        # The NUL corruption sits between the first valid line and EOF
        # A naive json.loads(line) on the NUL line raises JSONDecodeError
        raised = False
        for line in lines:
            try:
                json.loads(line)
            except json.JSONDecodeError:
                raised = True
                break
        self.assertTrue(raised, "Old reader should raise JSONDecodeError on corrupt line")

    def test_fixed_reader_skips_copmline_and_logs(self):
        """The fixed reader skips the NUL-corrupt line, logs loudly, returns valid."""
        # The valid line is preserved; the NUL line is corrupted
        entries = list(_tolerant_read.iter_jsonl_entries(self.path))
        # Should have logged a corrupt-line warning
        with self.assertLogs(_tolerant_read.logger, level="WARNING") as cm:
            entries = list(_tolerant_read.iter_jsonl_entries(self.path))
        self.assertTrue(any("corrupt_discovered_line" in r for r in cm.output))
        # Should return only the valid line
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["coins"], [])


if __name__ == "__main__":
    unittest.main()
