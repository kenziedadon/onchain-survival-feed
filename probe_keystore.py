#!/usr/bin/env python3
"""Probe keystore — gpg-symmetric encrypted ETH key file, decrypt-at-use.

WO7. The decided secret-store form for the probe lane:

  * The encrypted key file lives OUTSIDE any repo, at a locked path
    (dir 0700, file 0600).
  * GENERATION pipes the key straight into gpg encryption: the plaintext
    key is written to gpg's stdin and never touches disk.
  * DECRYPT-AT-USE reads the passphrase from an interactive terminal prompt
    only (getpass -> /dev/tty). The passphrase never transits argv (invisible
    to ps), never an env var (invisible to /proc/<pid>/environ), and never a
    config file. gpg receives it on a dedicated fd via --passphrase-fd.
  * The decrypted key lives in memory only. It is never written to disk and
    never logged.
  * FAIL-CLOSED everywhere: a wrong passphrase or ANY error means NO key is
    returned and the caller must keep the lane disarmed. There is no plaintext
    fallback anywhere in this module.

WO8 adds the IMPORT path: an EXISTING key (the main probe wallet's owner
key) is typed at a hidden local terminal prompt and validated against an
expected address BEFORE anything is written — fail-closed on mismatch,
wrong-key refusal, no-overwrite, zeroize-after-handoff.

This module performs NO arming, NO posture change, and NO funding. It only
holds/returns key bytes in memory. Whether to act on a returned key is the
caller's decision, gated elsewhere.

Dependency: the `gpg` binary (GnuPG). Verified present: 2.4.4.
"""

import logging
import os
import subprocess
import sys

log = logging.getLogger("probe_keystore")

# Locked, out-of-repo location. ~/.probe-keystore is not inside any git repo.
KEYSTORE_DIR = os.path.expanduser("~/.probe-keystore")
KEYSTORE_FILE = os.path.join(KEYSTORE_DIR, "probe_key.gpg")

# gpg symmetric cipher. AES256 is gpg's default and strongest here.
_CIPHER = "AES256"


class KeystoreError(Exception):
    """Fail-closed: any condition that prevents returning a usable key."""


def _require_gpg() -> str:
    """Return the gpg binary path or raise. No silent fallback."""
    from shutil import which
    gpg = which("gpg")
    if not gpg:
        raise KeystoreError("gpg binary not found on PATH; cannot use keystore")
    return gpg


def _gpg_with_passphrase_fd(args, passphrase: bytes, stdin_data=None):
    """Run gpg with the passphrase on a dedicated fd, never on argv.

    The passphrase is written to a pipe and passed via --passphrase-fd, so it
    never appears in the process command line (ps) and never in the
    environment. Returns the CompletedProcess; caller checks returncode.
    """
    gpg = _require_gpg()
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, passphrase + b"\n")
    finally:
        os.close(write_fd)
    try:
        proc = subprocess.run(
            [gpg, "--batch", "--yes", "--pinentry-mode", "loopback",
             "--passphrase-fd", str(read_fd)] + args,
            input=stdin_data,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=[read_fd],
        )
    finally:
        os.close(read_fd)
    return proc


def _zeroize(buf) -> None:
    """Best-effort wipe of key bytes in memory.

    Python bytes are immutable, so this only helps when the caller passes a
    mutable bytearray (the recommended form for holding key material). It
    overwrites in place and drops the reference. It is best-effort: the
    interpreter may retain copies it does not expose. This is the keystore's
    contribution to the "zeroize after use" requirement; the CONSUMER is
    responsible for calling it once the key is no longer needed.
    """
    if isinstance(buf, bytearray):
        for i in range(len(buf)):
            buf[i] = 0
    # bytes: nothing safe to do in pure Python; caller should use bytearray.


def generate_and_encrypt(passphrase: bytes) -> tuple:
    """Generate a fresh ETH key and encrypt it into the locked keystore.

    Returns (public_address, keystore_path). The PLAINTEXT private key is
    piped straight into gpg's stdin and NEVER written to disk. Only the
    public address is printed/returned — the private key is never logged,
    never printed, never returned to the caller.

    Used for BOTH the throwaway test key and (later, hands-on) the real
    probe key: the same safe path, plaintext never on disk either way.

    Requires eth_keys for address derivation (present in the project venv).
    """
    if not passphrase:
        raise KeystoreError("empty passphrase refused")
    try:
        from eth_keys import keys as _eth_keys
    except ImportError as e:
        raise KeystoreError(
            "eth_keys not available for address derivation; cannot generate"
        ) from e

    # Fresh 32-byte private key from the OS CSPRNG.
    raw = os.urandom(32)
    if len(raw) != 32:  # defensive; urandom(32) is always 32
        raise KeystoreError("key generation produced wrong length")

    # Derive the public address for display ONLY. The private key stays in
    # `raw` and is piped into gpg; it is never printed or returned.
    address = _eth_keys.PrivateKey(raw).public_key.to_checksum_address()

    os.makedirs(KEYSTORE_DIR, mode=0o700, exist_ok=True)
    os.chmod(KEYSTORE_DIR, 0o700)

    proc = _gpg_with_passphrase_fd(
        ["--symmetric", "--cipher-algo", _CIPHER, "--output", KEYSTORE_FILE],
        passphrase,
        stdin_data=raw,
    )
    # Wipe the in-memory plaintext immediately after handing it to gpg.
    raw = bytearray(raw)
    _zeroize(raw)
    if proc.returncode != 0:
        try:
            if os.path.exists(KEYSTORE_FILE):
                os.remove(KEYSTORE_FILE)
        except OSError:
            pass
        raise KeystoreError(
            f"gpg encryption failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    os.chmod(KEYSTORE_FILE, 0o600)
    log.info("keystore generated+encrypted: %s (mode 0600), addr=%s",
             KEYSTORE_FILE, address)
    return address, KEYSTORE_FILE


def import_and_encrypt(expected_address: str, passphrase: bytes = None) -> tuple:
    """IMPORT an EXISTING ETH key into the locked keystore (WO8).

    The probe operates from the MAIN wallet
    0xe7a16Be2aF1eCE8922d9d3aD55C03e29004e540F; its owner key is imported,
    not generated. Fail-closed at every junction:

      1. The private key is typed at a HIDDEN local terminal prompt
         (getpass -> /dev/tty, no echo). It never transits argv (invisible
         to ps), never an env var, never a file, and never chat.
      2. It must be valid hex of exactly 32 bytes, and the address derived
         via eth_keys must equal expected_address EXACTLY (case-insensitive
         hex identity; a malformed or mismatched key is refused and
         NOTHING is written).
      3. The passphrase arrives via the same fd mechanism as generation and
         must be non-empty.
      4. The key bytes are piped into gpg's stdin (plaintext never on disk)
         into the SAME store (~/.probe-keystore/probe_key.gpg, AES256,
         dir 0700 / file 0600). If the keystore file already EXISTS, this
         REFUSES: re-import is a deliberate operator delete first.
      5. The in-memory plaintext is zeroized right after the gpg handoff.

    Returns (public_address, keystore_path) — the same shape as
    generate_and_encrypt. The private key is never printed, never logged,
    never returned. Performs NO arming, NO posture change, NO wiring.
    """
    # Refuse BEFORE prompting: an existing keystore is never overwritten.
    if os.path.exists(KEYSTORE_FILE):
        raise KeystoreError(
            f"keystore already exists: {KEYSTORE_FILE} — refusing to "
            f"overwrite. Delete it deliberately first if a re-import is "
            f"intended; re-import is an operator action."
        )

    exp = expected_address.strip() if expected_address else ""
    if not (exp.startswith("0x") and len(exp) == 42
            and all(c in "0123456789abcdefABCDEF" for c in exp[2:])):
        raise KeystoreError(
            f"expected_address is not a valid 0x + 40-hex address: "
            f"{expected_address!r}"
        )

    # 1. Key from a hidden local prompt only.
    import getpass
    key_text = getpass.getpass(
        "Probe private key (hex, typed locally, never stored): "
    )
    raw = _parse_hex_key(key_text)

    # 2. Derive and validate against expected_address. Nothing is written
    #    on mismatch.
    try:
        from eth_keys import keys as _eth_keys
    except ImportError as e:
        raise KeystoreError(
            "eth_keys not available for address derivation; cannot import"
        ) from e
    try:
        address = _eth_keys.PrivateKey(raw).public_key.to_checksum_address()
    except Exception as e:  # noqa: BLE001 — any derivation failure fails closed
        raise KeystoreError(
            f"key does not derive a valid secp256k1 address: {e}"
        ) from e
    if address.lower() != exp.lower():
        raise KeystoreError(
            "derived address does not match expected_address — wrong key "
            "refused, nothing written. (lane stays disarmed)"
        )

    # 3. Passphrase via the same fd mechanism; empty refused.
    if passphrase is None:
        passphrase = getpass.getpass(
            "Probe keystore passphrase (typed locally, not stored): "
        ).encode("utf-8")
    if not passphrase:
        raise KeystoreError("empty passphrase refused")

    # 4. Pipe key bytes into the same gpg store; plaintext never on disk.
    os.makedirs(KEYSTORE_DIR, mode=0o700, exist_ok=True)
    os.chmod(KEYSTORE_DIR, 0o700)

    proc = _gpg_with_passphrase_fd(
        ["--symmetric", "--cipher-algo", _CIPHER, "--output", KEYSTORE_FILE],
        passphrase,
        stdin_data=raw,
    )
    # 5. Wipe the in-memory plaintext immediately after handing it to gpg.
    raw = bytearray(raw)
    _zeroize(raw)
    if proc.returncode != 0:
        try:
            if os.path.exists(KEYSTORE_FILE):
                os.remove(KEYSTORE_FILE)
        except OSError:
            pass
        raise KeystoreError(
            f"gpg encryption failed (rc={proc.returncode}): "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    os.chmod(KEYSTORE_FILE, 0o600)
    log.info("keystore imported+encrypted: %s (mode 0600), addr=%s",
             KEYSTORE_FILE, address)
    return address, KEYSTORE_FILE


def _parse_hex_key(text: str) -> bytes:
    """Validate hex private-key input: exactly 32 bytes, or raise.

    Accepts an optional 0x/0X prefix and surrounding whitespace. Anything
    else (non-hex, wrong length, empty) raises KeystoreError — fail-closed,
    nothing is written by the caller on this path.
    """
    s = text.strip()
    if s[:2] in ("0x", "0X"):
        s = s[2:]
    s = "".join(s.split())
    if not s:
        raise KeystoreError("empty key refused")
    if len(s) != 64:
        raise KeystoreError(
            f"key must be exactly 32 bytes (64 hex chars); got {len(s)} "
            f"hex chars — refusing"
        )
    try:
        raw = bytes.fromhex(s)
    except ValueError as e:
        raise KeystoreError(f"key is not valid hex: {e}") from e
    if len(raw) != 32:  # defensive; 64 hex chars always decodes to 32 bytes
        raise KeystoreError(f"key is {len(raw)} bytes, expected 32")
    return raw


def decrypt_at_use(passphrase: bytes = None) -> bytes:
    """Decrypt the keystore key into memory. Fail-closed on any error.

    If passphrase is None, prompt on the terminal (getpass -> /dev/tty) so the
    passphrase is typed locally and never transits chat, argv, env, or a file.
    Returns the 32-byte key on success. On wrong passphrase or ANY error,
    raises KeystoreError and returns nothing — the caller keeps the lane
    disarmed. No plaintext fallback exists.
    """
    if not os.path.exists(KEYSTORE_FILE):
        raise KeystoreError(f"keystore file not found: {KEYSTORE_FILE}")

    if passphrase is None:
        import getpass
        passphrase = getpass.getpass(
            "Probe keystore passphrase (typed locally, not stored): "
        ).encode("utf-8")
    if not passphrase:
        raise KeystoreError("empty passphrase refused")

    proc = _gpg_with_passphrase_fd(
        ["--decrypt", KEYSTORE_FILE], passphrase
    )
    if proc.returncode != 0:
        raise KeystoreError(
            f"gpg decryption failed (rc={proc.returncode}) — wrong passphrase "
            f"or corrupt file; lane stays disarmed. "
            f"{proc.stderr.decode(errors='replace').strip()}"
        )
    key = proc.stdout
    if len(key) != 32:
        raise KeystoreError(
            f"decrypted key is {len(key)} bytes, expected 32; refusing"
        )
    # Key is in memory only; never logged, never written to disk.
    log.info("keystore decrypted into memory (32 bytes); not persisted")
    return key


def main(argv=None) -> int:
    """CLI entry. Two subcommands, both fail-closed:

      probe_keystore.py import <expected_address>
          Import an EXISTING key. Prompt 1 takes the private key (hidden,
          no echo); prompt 2 sets the keystore passphrase. Refuses anything
          that does not derive to <expected_address> exactly; refuses to
          overwrite an existing keystore. On success prints ONLY the
          address and path. Exit 0 on success, 1 on fail-closed, 2 on
          usage error.

      probe_keystore.py
          Decrypt-at-use. Prompts for the passphrase, returns the key to
          memory, prints nothing sensitive. Exit 0/1.

    Performs NO arming, NO posture change, NO wiring.
    """
    logging.basicConfig(level=logging.INFO)
    argv = sys.argv[1:] if argv is None else argv

    if len(argv) >= 1 and argv[0] == "import":
        if len(argv) != 2:
            print("usage: probe_keystore.py import <expected_address>",
                  file=sys.stderr)
            return 2
        try:
            address, path = import_and_encrypt(argv[1])
        except KeystoreError as e:
            print(f"FAIL-CLOSED: {e}", file=sys.stderr)
            return 1
        # Deliberately print ONLY the address and path — never the key.
        print(f"IMPORTED: {address} {path}")
        return 0

    # Default: decrypt-at-use entry; prompt for passphrase.
    try:
        key = decrypt_at_use()
    except KeystoreError as e:
        print(f"FAIL-CLOSED: {e}", file=sys.stderr)
        return 1
    # Deliberately do NOT print the key. Confirm only length; the operator
    # verifies the derived address via their own tool.
    print(f"OK: key decrypted into memory ({len(key)} bytes); not printed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
