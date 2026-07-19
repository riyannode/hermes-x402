"""Atomic, fail-closed environment file writer for hermes-x402.

Writes managed key-value pairs to $HERMES_HOME/.env with:
  - key name validation: ^[A-Z_][A-Z0-9_]*$
  - value rejection: newline, carriage return, NUL
  - target rejection: symlink, parent symlink, directory, FIFO, socket, device
  - temporary file in the same directory, mode 0600 before content is written
  - fsync on temp file
  - os.replace for atomic swap
  - chmod 0600 on final file
  - fsync on parent directory
  - revalidation of target and parent immediately before os.replace
  - preservation of comments, blank lines, and unrelated variables
  - never prints the full env file contents, secrets, or old values
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

# Key name pattern: must start with letter or underscore, then alphanumeric or underscore.
_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")

# Forbidden bytes in values: \x00 (NUL), \n (LF), \r (CR)
_FORBIDDEN_IN_VALUE = re.compile(r"[\x00\n\r]")


def _resolve_hermes_home() -> Path:
    """Resolve HERMES_HOME from environment or default."""
    home = os.environ.get("HERMES_HOME", "")
    if home:
        return Path(home).resolve()
    return Path.home().resolve() / ".hermes"


def _validate_key(key: str) -> None:
    """Raise OSError if key name is invalid."""
    if not isinstance(key, str) or not _KEY_RE.match(key):
        raise OSError(f"Invalid env key name: {key!r}")


def _validate_value(value: str, key: str) -> None:
    """Raise OSError if value contains forbidden characters."""
    if _FORBIDDEN_IN_VALUE.search(value):
        raise OSError(f"Env value for {key} contains forbidden characters (newline/CR/NUL)")


def _lstat_check(path: Path, label: str) -> None:
    """Check that path exists and is a regular file using lstat (no symlink follow)."""
    try:
        st = path.lstat()
    except OSError:
        return  # path doesn't exist yet — OK
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"Refusing to write to symlink {label}: {path}")
    if stat.S_ISDIR(st.st_mode):
        raise OSError(f"Refusing to write to directory {label}: {path}")
    if stat.S_ISFIFO(st.st_mode):
        raise OSError(f"Refusing to write to FIFO {label}: {path}")
    if stat.S_ISSOCK(st.st_mode):
        raise OSError(f"Refusing to write to socket {label}: {path}")
    if stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode):
        raise OSError(f"Refusing to write to device {label}: {path}")
    if not stat.S_ISREG(st.st_mode):
        raise OSError(f"Refusing to write to non-regular file {label}: {path}")


def _check_target_safety(env_path: Path) -> None:
    """Comprehensive safety check on the target path using lstat.

    Checks performed:
    - target itself is not a symlink or non-regular file
    - parent directory is not a symlink
    - target itself is not a directory, FIFO, socket, or device
    """
    # Check target itself
    _lstat_check(env_path, "target")

    # Check parent directory is not a symlink
    parent = env_path.parent
    try:
        parent_st = parent.lstat()
    except OSError:
        # Parent doesn't exist — will be created later; that's fine.
        return
    if stat.S_ISLNK(parent_st.st_mode):
        raise OSError(f"Refusing to write to directory under symlink parent: {parent}")

    # If target exists, verify it's a regular file (not dir/FIFO/socket/device)
    if env_path.exists():
        try:
            target_st = env_path.lstat()
        except OSError:
            return
        if stat.S_ISDIR(target_st.st_mode):
            raise OSError(f"Refusing to write to directory: {env_path}")
        if stat.S_ISFIFO(target_st.st_mode):
            raise OSError(f"Refusing to write to FIFO: {env_path}")
        if stat.S_ISSOCK(target_st.st_mode):
            raise OSError(f"Refusing to write to socket: {env_path}")
        if stat.S_ISBLK(target_st.st_mode) or stat.S_ISCHR(target_st.st_mode):
            raise OSError(f"Refusing to write to device: {env_path}")


def update_env_file(
    env_path: Path,
    managed_keys: dict[str, str],
    *,
    _fsync: bool = True,
) -> None:
    """Atomically update managed keys in an env file.

    Rules:
      - validate all key names: ^[A-Z_][A-Z0-9_]*$
      - reject values containing newline, carriage return, or NUL
      - reject if env_path or parent is a symlink
      - reject non-regular-file targets (directory, FIFO, socket, device)
      - preserve comments, blank lines, and unrelated variables
      - update only keys present in managed_keys
      - write temp file in same directory with mode 0600 BEFORE writing content
      - fsync, os.replace for atomic swap
      - revalidate target and parent immediately before os.replace
      - chmod 0600 on the final file
      - fsync parent directory
      - never raise on missing file (create if absent)
      - never print full env contents, secrets, wallet addresses, or old values

    Args:
        env_path: Path to the .env file.
        managed_keys: Dict of key=value pairs to write/update.
        _fsync: Whether to actually fsync (disable for tests).
    """
    env_path = Path(env_path)

    # Validate all keys before doing any work
    for key, value in managed_keys.items():
        _validate_key(key)
        _validate_value(value, key)

    # Comprehensive target safety check using lstat
    _check_target_safety(env_path)

    parent = env_path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Read existing content (empty if file doesn't exist)
    existing_lines: list[str] = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)

    # Parse existing keys and their line indices
    existing_key_indices: dict[str, int] = {}
    for i, line in enumerate(existing_lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key:
                existing_key_indices[key] = i

    # Build new lines: update existing keys, append new ones
    new_lines: list[str] = list(existing_lines)
    appended_keys: set[str] = set()

    for key, value in managed_keys.items():
        if key in existing_key_indices:
            idx = existing_key_indices[key]
            new_lines[idx] = f"{key}={value}\n"
        else:
            appended_keys.add(key)

    # Append new keys at the end
    for key in appended_keys:
        value = managed_keys[key]
        new_lines.append(f"{key}={value}\n")

    # Write to temp file in same directory
    fd, tmp_path = tempfile.mkstemp(
        dir=str(parent),
        prefix=".env.tmp.",
        suffix=".tmp",
    )
    try:
        # Set mode 0600 BEFORE writing any sensitive content
        os.chmod(tmp_path, 0o600)

        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            if _fsync:
                f.flush()
                os.fsync(f.fileno())

        # Revalidate target and parent immediately before atomic replace
        _check_target_safety(env_path)

        # Atomic replace
        os.replace(tmp_path, str(env_path))

        # Set permissions (belt-and-suspenders after replace)
        os.chmod(str(env_path), 0o600)

        # Fsync parent directory
        if _fsync:
            dir_fd = os.open(str(parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except BaseException:
        # Clean up temp file on failure
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
