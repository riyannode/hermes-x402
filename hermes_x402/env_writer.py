"""Atomic, fail-closed environment file writer for hermes-x402.

Writes managed key-value pairs to $HERMES_HOME/.env with:
  - temporary file in the same directory
  - fsync on temp file
  - os.replace for atomic swap
  - chmod 0600 on final file
  - fsync on parent directory
  - symlink target rejection
  - preservation of comments, blank lines, and unrelated variables
  - never prints the full env file contents
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _resolve_hermes_home() -> Path:
    """Resolve HERMES_HOME from environment or default."""
    home = os.environ.get("HERMES_HOME", "")
    if home:
        return Path(home).resolve()
    return Path.home().resolve() / ".hermes"


def _is_symlink_target(path: Path) -> bool:
    """Check if path itself is a symlink or any ancestor is a symlink."""
    try:
        return path.is_symlink() or path.resolve() != path.absolute()
    except OSError:
        return True  # fail-closed


def update_env_file(
    env_path: Path,
    managed_keys: dict[str, str],
    *,
    _fsync: bool = True,
) -> None:
    """Atomically update managed keys in an env file.

    Rules:
      - reject if env_path is a symlink
      - preserve comments, blank lines, and unrelated variables
      - update only keys present in managed_keys
      - write temp file in same directory, fsync, os.replace
      - chmod 0600 on the final file
      - fsync parent directory
      - never raise on missing file (create if absent)

    Args:
        env_path: Path to the .env file.
        managed_keys: Dict of key=value pairs to write/update.
        _fsync: Whether to actually fsync (disable for tests).
    """
    env_path = Path(env_path)

    # Reject symlinks
    if _is_symlink_target(env_path):
        raise OSError(f"Refusing to write to symlink: {env_path}")

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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
            if _fsync:
                f.flush()
                os.fsync(f.fileno())

        # Atomic replace
        os.replace(tmp_path, str(env_path))

        # Set permissions
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
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
