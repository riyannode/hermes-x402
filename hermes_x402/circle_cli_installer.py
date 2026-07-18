"""Circle CLI detection, version validation, and optional bootstrap.

Used by the installer when --with-circle-cli is passed.

Never installs Bun or Node. Never logs in, accepts Terms, creates wallets,
funds wallets, pays, or deposits. Never prints credentials.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_CLI_VERSION = "0.0.6"
_INSTALL_TIMEOUT = 120  # seconds for bun add -g
_VERSION_TIMEOUT = 15  # seconds for circle --version


@dataclass
class CircleCliReport:
    """Structured report for Circle CLI bootstrap status."""

    requested: bool = False
    available: bool = False
    installed: bool = False
    already_present: bool = False
    version: str | None = None
    executable: str | None = None
    package_manager: str | None = None
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "circle_cli": {
                "requested": self.requested,
                "available": self.available,
                "installed": self.installed,
                "already_present": self.already_present,
                "version": self.version,
                "executable": self.executable,
                "package_manager": self.package_manager,
                "errors": list(self.errors),
            }
        }


def _find_existing_cli() -> Path | None:
    """Find an existing circle binary on PATH."""
    found = shutil.which("circle")
    if found:
        return Path(found).resolve()
    return None


def _query_cli_version(executable: Path) -> str | None:
    """Query circle --version. Returns version string or None on failure."""
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
        )
        if result.returncode != 0:
            return None
        # Parse version from output like "0.0.6" or "circle/0.0.6 ..."
        output = result.stdout.strip()
        match = re.search(r"(\d+\.\d+\.\d+)", output)
        if match:
            return match.group(1)
        return output if output else None
    except (subprocess.TimeoutExpired, OSError):
        return None


def _check_bun_available() -> Path | None:
    """Check if bun is available on PATH."""
    found = shutil.which("bun")
    if found:
        return Path(found).resolve()
    return None


def _install_cli_via_bun(bun: Path) -> tuple[Path | None, str | None]:
    """Install @circle-fin/cli@0.0.6 via bun global.

    Returns (executable_path, error_message).
    """
    try:
        result = subprocess.run(
            [str(bun), "add", "-g", f"@circle-fin/cli@{SUPPORTED_CLI_VERSION}"],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT,
            shell=False,
        )
        if result.returncode != 0:
            stderr = result.stderr[:500] if result.stderr else ""
            return None, f"bun add failed (rc={result.returncode}): {stderr}"
    except subprocess.TimeoutExpired:
        return None, f"bun add timed out after {_INSTALL_TIMEOUT}s"
    except OSError as exc:
        return None, f"bun add failed: {exc}"

    # Resolve the installed binary
    circle_path = _find_existing_cli()
    if circle_path is None:
        return None, "circle binary not found after bun install"

    # Verify version
    version = _query_cli_version(circle_path)
    if version != SUPPORTED_CLI_VERSION:
        return None, (
            f"Installed circle version {version} does not match "
            f"expected {SUPPORTED_CLI_VERSION}"
        )

    return circle_path, None


def run_circle_cli_bootstrap(
    with_circle_cli: bool = False,
) -> CircleCliReport:
    """Detect or install Circle CLI.

    Args:
        with_circle_cli: Whether --with-circle-cli was passed.

    Returns:
        CircleCliReport with structured status.
    """
    report = CircleCliReport(requested=with_circle_cli)

    # Step 1: Check for existing installation
    existing = _find_existing_cli()
    if existing is not None:
        report.available = True
        report.executable = str(existing)
        version = _query_cli_version(existing)
        report.version = version

        if version == SUPPORTED_CLI_VERSION:
            report.installed = True
            report.already_present = True
            report.package_manager = "bun"  # Circle CLI is always installed via npm/bun
            return report

        if version is not None:
            # Version mismatch — do not replace
            report.errors.append(
                f"circle_cli_version_mismatch: found v{version}, "
                f"expected v{SUPPORTED_CLI_VERSION}. "
                f"Manual remediation: bun add -g @circle-fin/cli@{SUPPORTED_CLI_VERSION}"
            )
            return report

        # Version check failed but binary exists
        report.errors.append("circle_cli_version_check_failed")
        return report

    # Step 2: CLI not found
    if not with_circle_cli:
        # Not requested — this is fine, just report absent
        return report

    # Step 3: --with-circle-cli requested, CLI absent
    bun = _check_bun_available()
    if bun is None:
        report.errors.append(
            "bun_not_found: Bun is required to install Circle CLI. "
            "Install Bun first: https://bun.sh/docs/installation"
        )
        return report

    # Step 4: Install via bun
    circle_path, error = _install_cli_via_bun(bun)
    if error:
        report.errors.append(error)
        return report

    # Success
    report.available = True
    report.installed = True
    report.already_present = False
    report.executable = str(circle_path)
    report.version = SUPPORTED_CLI_VERSION
    report.package_manager = "bun"
    return report
