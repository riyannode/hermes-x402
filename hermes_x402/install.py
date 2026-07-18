"""Production installer for hermes-x402.

Detects the Hermes executable, identifies the Python environment,
builds a wheel, installs it, enables the plugin, and verifies
14 tools + 1 pre_tool_call hook are registered.

Usage:
    python -m hermes_x402.install            # full install + verify
    python -m hermes_x402.install --check    # verify only (no install)
    python -m hermes_x402.install --live-test # run live Arc Testnet test
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS = 14
_EXPECTED_HOOKS = 1
_PLUGIN_NAME = "hermes-x402"
_ENV_DEBUG = "HERMES_PLUGINS_DEBUG"


def _find_hermes_executable() -> Path:
    """Locate the hermes CLI executable."""
    candidate = shutil.which("hermes")
    if candidate:
        return Path(candidate)
    raise RuntimeError(
        "Cannot find 'hermes' on PATH. "
        "Install Hermes Agent first: https://github.com/NousResearch/hermes-agent"
    )


def _detect_python_env(hermes_exe: Path) -> Path:
    """Detect the Python interpreter used by the Hermes venv."""
    # Read the shebang from the hermes script
    first_line = hermes_exe.read_text().splitlines()[0]
    if first_line.startswith("#!"):
        python_path = Path(first_line[2:].strip())
        if python_path.exists():
            return python_path
    # Fallback: assume venv/bin/python3 relative to hermes_exe
    venv_python = hermes_exe.parent / "python3"
    if venv_python.exists():
        return venv_python
    raise RuntimeError(
        f"Cannot detect Python environment for Hermes at {hermes_exe}"
    )


def _build_wheel(repo_root: Path) -> Path:
    """Build a wheel from the repository. Returns the wheel path."""
    dist_dir = repo_root / "dist"
    # Clean previous builds
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)

    subprocess.check_call(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist_dir)],
        cwd=str(repo_root),
    )
    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("Wheel build produced no output in dist/")
    return wheels[0]


def _install_wheel(python: Path, wheel: Path) -> None:
    """Install the wheel into the Hermes Python environment."""
    subprocess.check_call(
        [str(python), "-m", "pip", "install", "--force-reinstall", str(wheel)],
    )


def _enable_plugin(hermes_exe: Path) -> None:
    """Enable the hermes-x402 plugin."""
    subprocess.check_call(
        [str(hermes_exe), "plugins", "enable", _PLUGIN_NAME, "--no-allow-tool-override"],
    )


def _verify_plugin(hermes_exe: Path) -> dict:
    """Run HERMES_PLUGINS_DEBUG=1 hermes plugins list and verify."""
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"

    result = subprocess.run(
        [str(hermes_exe), "plugins", "list"],
        capture_output=True,
        text=True,
        env=env,
    )
    output = result.stdout + result.stderr

    # Parse tool count and hook count from debug output
    tool_count = 0
    hook_count = 0
    for line in output.splitlines():
        m = re.search(r"registered\s+(\d+)\s+tools?", line, re.IGNORECASE)
        if m:
            tool_count = max(tool_count, int(m.group(1)))
        m = re.search(r"registered\s+(\d+)\s+hooks?", line, re.IGNORECASE)
        if m:
            hook_count = max(hook_count, int(m.group(1)))

    # Also try counting individual tool registrations
    if tool_count == 0:
        tool_count = output.lower().count("x402_")

    return {
        "tool_count": tool_count,
        "hook_count": hook_count,
        "output": output,
        "plugin_enabled": _PLUGIN_NAME in output and "enabled" in output,
    }


def _get_package_version() -> str:
    """Get installed package version."""
    try:
        from hermes_x402.hermes_plugin.runtime import _VERSION
        return _VERSION
    except Exception:
        return "unknown"


def _get_commit_sha(repo_root: Path) -> str:
    """Get current git commit SHA."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


def run_install(repo_root: Path | None = None, check_only: bool = False) -> dict:
    """Run the installer. Returns a status dict.

    If check_only=True, skips build/install and only verifies.
    """
    if repo_root is None:
        repo_root = Path(__file__).resolve().parent.parent

    report: dict = {
        "repo_root": str(repo_root),
        "check_only": check_only,
        "success": False,
        "errors": [],
        "warnings": [],
    }

    try:
        # 1. Find hermes executable
        hermes_exe = _find_hermes_executable()
        report["hermes_exe"] = str(hermes_exe)
        print(f"[install] Hermes executable: {hermes_exe}")

        # 2. Detect Python environment
        python_exe = _detect_python_env(hermes_exe)
        report["python_env"] = str(python_exe)
        print(f"[install] Python environment: {python_exe}")

        # 3. Get version info
        commit_sha = _get_commit_sha(repo_root)
        report["commit_sha"] = commit_sha
        print(f"[install] Commit SHA: {commit_sha}")

        if not check_only:
            # 4. Build wheel
            wheel_path = _build_wheel(repo_root)
            report["wheel"] = str(wheel_path)
            print(f"[install] Built wheel: {wheel_path}")

            # 5. Install wheel
            _install_wheel(python_exe, wheel_path)
            print("[install] Wheel installed successfully")

            # 6. Enable plugin
            _enable_plugin(hermes_exe)
            print(f"[install] Plugin '{_PLUGIN_NAME}' enabled")

        # 7. Verify plugin
        verification = _verify_plugin(hermes_exe)
        report["verification"] = verification
        print(f"[install] Plugin enabled: {verification['plugin_enabled']}")
        print(f"[install] Tools registered: {verification['tool_count']}")
        print(f"[install] Hooks registered: {verification['hook_count']}")

        # 8. Validate counts
        if not verification["plugin_enabled"]:
            report["errors"].append("Plugin is not enabled")
        if verification["tool_count"] != _EXPECTED_TOOLS:
            report["errors"].append(
                f"Expected {_EXPECTED_TOOLS} tools, got {verification['tool_count']}"
            )
        if verification["hook_count"] != _EXPECTED_HOOKS:
            report["errors"].append(
                f"Expected {_EXPECTED_HOOKS} hook, got {verification['hook_count']}"
            )

        # 9. Package version
        version = _get_package_version()
        report["version"] = version
        print(f"[install] Package version: {version}")

        # Final verdict
        if not report["errors"]:
            report["success"] = True
            print("[install] ✅ All checks passed")
        else:
            for err in report["errors"]:
                print(f"[install] ❌ {err}")

    except Exception as exc:
        report["errors"].append(str(exc))
        print(f"[install] ❌ Installation failed: {exc}", file=sys.stderr)

    # Never print secrets
    for key in ("entity_secret", "api_key", "private_key", "otp", "password"):
        if key in report:
            del report[key]

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install and verify hermes-x402 plugin"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify only — skip build/install",
    )
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Run live Arc Testnet acceptance test",
    )
    args = parser.parse_args()

    if args.live_test:
        from hermes_x402.live_test import run_live_test
        sys.exit(0 if run_live_test() else 1)

    report = run_install(check_only=args.check)
    sys.exit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
