#!/usr/bin/env python3
"""Tests for probe_keystore (WO7). Throwaway key only — destroyed at teardown.

Run: /home/kenzodadon/base-trend-bot/.venv/bin/python test_probe_keystore.py
     (venv required: generation uses eth_keys for address derivation)

Covers the junctions the spec demands:
  1. Generation returns ONLY the public address (never the private key) and
     writes an encrypted file.
  2. Round-trip: generated key decrypts with the right passphrase to the
     exact 32 bytes; the decrypted key's derived address equals the printed
     address.
  3. Wrong passphrase -> KeystoreError, NO key returned (fail-closed).
  4. Empty passphrase refused at generate.
  5. Plaintext never on disk: the keystore file and every file in the
     keystore dir contain neither the raw key nor its hex (grep proof).
  6. Locked permissions: dir 0700, file 0600.
  7. Missing keystore file -> KeystoreError (fail-closed).
  8. decrypt_at_use never prints the key to stdout.
  9. _zeroize wipes a bytearray in place.

Uses a temporary KEYSTORE_DIR by monkeypatching the module globals so the
real ~/.probe-keystore is never touched. The generated throwaway key is
discarded and the temp dir removed at teardown.
"""

import contextlib
import io
import os
import shutil
import stat
import sys
import tempfile

sys.path.insert(0, "/home/kenzodadon/base-trend-bot")

import probe_keystore as pk
from eth_keys import keys as _eth_keys

THROWAWAY_PASS = b"throwaway-pass-phrase-wo7"
WRONG_PASS = b"definitely-the-wrong-passphrase"


def _use_tmp_keystore():
    """Point the module at a temp dir; return (tmpdir, orig globals)."""
    tmp = tempfile.mkdtemp(prefix="probe_ks_test_")
    orig = (pk.KEYSTORE_DIR, pk.KEYSTORE_FILE)
    pk.KEYSTORE_DIR = tmp
    pk.KEYSTORE_FILE = os.path.join(tmp, "probe_key.gpg")
    return tmp, orig


def _restore(orig, tmp):
    pk.KEYSTORE_DIR, pk.KEYSTORE_FILE = orig
    shutil.rmtree(tmp, ignore_errors=True)


def test_generation_returns_only_address():
    tmp, orig = _use_tmp_keystore()
    try:
        address, path = pk.generate_and_encrypt(THROWAWAY_PASS)
        assert path == pk.KEYSTORE_FILE
        assert os.path.exists(path), "keystore file created"
        assert isinstance(address, str) and address.startswith("0x"), (
            "returns a public address"
        )
        assert len(address) == 42, f"address length {len(address)} != 42"
        # The private key is NOT returned anywhere — only (address, path).
        print("PASS generation returns only public address, file written")
    finally:
        _restore(orig, tmp)


def test_roundtrip():
    tmp, orig = _use_tmp_keystore()
    try:
        address, _ = pk.generate_and_encrypt(THROWAWAY_PASS)
        key = pk.decrypt_at_use(THROWAWAY_PASS)
        assert len(key) == 32, "decrypted key is 32 bytes"
        # The decrypted key must derive the SAME address that was printed.
        derived = _eth_keys.PrivateKey(key).public_key.to_checksum_address()
        assert derived == address, "decrypted key matches printed address"
        print("PASS round-trip: decrypt -> derived address == printed address")
    finally:
        _restore(orig, tmp)


def test_wrong_passphrase_fails_closed():
    tmp, orig = _use_tmp_keystore()
    try:
        pk.generate_and_encrypt(THROWAWAY_PASS)
        raised = False
        try:
            pk.decrypt_at_use(WRONG_PASS)
        except pk.KeystoreError:
            raised = True
        assert raised, "wrong passphrase must raise KeystoreError"
        print("PASS wrong passphrase: fail-closed, no key returned")
    finally:
        _restore(orig, tmp)


def test_empty_passphrase_refused():
    tmp, orig = _use_tmp_keystore()
    try:
        raised = False
        try:
            pk.generate_and_encrypt(b"")
        except pk.KeystoreError:
            raised = True
        assert raised, "generate must refuse empty passphrase"
        assert not os.path.exists(pk.KEYSTORE_FILE), "no file on refused input"
        print("PASS empty passphrase refused at generate, no file written")
    finally:
        _restore(orig, tmp)


def test_plaintext_never_on_disk():
    tmp, orig = _use_tmp_keystore()
    try:
        address, _ = pk.generate_and_encrypt(THROWAWAY_PASS)
        key = pk.decrypt_at_use(THROWAWAY_PASS)  # real bytes, to grep for
        key_hex = key.hex().encode()
        # 1. keystore file must not contain the raw key or its hex
        with open(pk.KEYSTORE_FILE, "rb") as f:
            blob = f.read()
        assert key not in blob, "raw private key leaked into ciphertext"
        assert key_hex not in blob, "key hex leaked into ciphertext"
        # 2. no stray plaintext file anywhere in the keystore dir
        for root, _dirs, files in os.walk(pk.KEYSTORE_DIR):
            for name in files:
                p = os.path.join(root, name)
                with open(p, "rb") as f:
                    data = f.read()
                assert key not in data, f"raw key found in {p}"
                assert key_hex not in data, f"key hex found in {p}"
        print("PASS plaintext never on disk (ciphertext only, grep proof)")
    finally:
        _restore(orig, tmp)


def test_locked_permissions():
    tmp, orig = _use_tmp_keystore()
    try:
        pk.generate_and_encrypt(THROWAWAY_PASS)
        dir_mode = stat.S_IMODE(os.stat(pk.KEYSTORE_DIR).st_mode)
        file_mode = stat.S_IMODE(os.stat(pk.KEYSTORE_FILE).st_mode)
        assert dir_mode == 0o700, f"dir mode {oct(dir_mode)} != 0700"
        assert file_mode == 0o600, f"file mode {oct(file_mode)} != 0600"
        print("PASS locked permissions: dir 0700, file 0600")
    finally:
        _restore(orig, tmp)


def test_missing_file_fails_closed():
    tmp, orig = _use_tmp_keystore()
    try:
        # no file generated
        raised = False
        try:
            pk.decrypt_at_use(THROWAWAY_PASS)
        except pk.KeystoreError:
            raised = True
        assert raised, "missing keystore file must fail closed"
        print("PASS missing keystore file: fail-closed")
    finally:
        _restore(orig, tmp)


def test_key_never_printed():
    tmp, orig = _use_tmp_keystore()
    try:
        pk.generate_and_encrypt(THROWAWAY_PASS)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            key = pk.decrypt_at_use(THROWAWAY_PASS)
        out = buf.getvalue()
        assert key.hex() not in out, "key hex printed to stdout"
        assert bytes(key) not in out.encode("utf-8", "ignore"), "key printed"
        print("PASS key never printed to stdout during decrypt")
    finally:
        _restore(orig, tmp)


def test_zeroize_wipes_bytearray():
    buf = bytearray(b"\xab" * 32)
    pk._zeroize(buf)
    assert buf == bytearray(32), "bytearray not zeroed"
    # immutable bytes: must not raise (best-effort no-op)
    pk._zeroize(b"\xab" * 32)
    print("PASS _zeroize wipes bytearray in place, tolerates immutable bytes")


if __name__ == "__main__":
    tests = [
        test_generation_returns_only_address,
        test_roundtrip,
        test_wrong_passphrase_fails_closed,
        test_empty_passphrase_refused,
        test_plaintext_never_on_disk,
        test_locked_permissions,
        test_missing_file_fails_closed,
        test_key_never_printed,
        test_zeroize_wipes_bytearray,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print(f"\n{'ALL %d TESTS PASSED' % len(tests) if not failed else '%d FAILED' % failed}")
    sys.exit(1 if failed else 0)
