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
import json
import os
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
        "Install Hermes Agent first: "
        "https://github.com/NousResearch/hermes-agent"
    )


def _detect_python_env(hermes_exe: Path) -> Path:
    """Detect the Python interpreter used by the Hermes venv."""
    first_line = hermes_exe.read_text().splitlines()[0]
    if first_line.startswith("#!"):
        python_path = Path(first_line[2:].strip())
        if python_path.exists():
            return python_path
    venv_python = hermes_exe.parent / "python3"
    if venv_python.exists():
        return venv_python
    raise RuntimeError(f"Cannot detect Python environment for Hermes at {hermes_exe}")


def _build_wheel(repo_root: Path) -> Path:
    """Build a wheel from the repository. Returns the wheel path."""
    dist_dir = repo_root / "dist"
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
        [
            str(hermes_exe),
            "plugins",
            "enable",
            _PLUGIN_NAME,
            "--no-allow-tool-override",
        ]
    )


def _query_plugins_json(hermes_exe: Path) -> dict:
    """Run ``hermes plugins list --json`` and parse the hermes-x402 record.

    Returns the parsed record dict.  Raises on subprocess failure or
    missing/malformed record.
    """
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"

    result = subprocess.run(
        [str(hermes_exe), "plugins", "list", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"hermes plugins list --json failed (rc={result.returncode}): {result.stderr[:500]}"
        )

    try:
        records = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse hermes plugins list JSON: {exc}") from exc

    for rec in records:
        if rec.get("name") == _PLUGIN_NAME:
            return rec

    raise RuntimeError(f"Plugin '{_PLUGIN_NAME}' not found in plugins list")


def _verify_tools_and_hooks(python: Path) -> dict:
    """Verify 14 tools + 1 hook through the Hermes Python interpreter.

    Imports the plugin entry point and calls register() with a minimal
    context that counts registrations.
    """
    code = (
        "import json, sys\n"
        "class _Ctx:\n"
        "    def __init__(s): s.tools=[]; s.hooks=[]\n"
        "    def register_tool(s, n, ts, sc, h, **k):\n"
        "        s.tools.append(n)\n"
        "    def register_hook(s, ht, h):\n"
        "        s.hooks.append(ht)\n"
        "from hermes_x402.hermes_plugin.entry import register\n"
        "ctx = _Ctx()\n"
        "register(ctx)\n"
        "print(json.dumps({'tools': len(ctx.tools), "
        "'hooks': len(ctx.hooks), 'names': ctx.tools}))\n"
    )

    result = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Plugin verification failed: {result.stderr[:500]}")

    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse verification output: {exc}") from exc


def _query_package_info(python: Path) -> dict:
    """Query installed package version and module path via Hermes Python."""
    code = (
        "import json, hermes_x402\n"
        "print(json.dumps({\n"
        "    'version': getattr(hermes_x402, '__version__', 'unknown'),\n"
        "    'path': hermes_x402.__file__,\n"
        "}))\n"
    )
    result = subprocess.run(
        [str(python), "-c", code],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return {"version": "unknown", "path": "unknown"}
    try:
        return json.loads(result.stdout.strip())
    except json.JSONDecodeError:
        return {"version": "unknown", "path": "unknown"}


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

        # 3. Get commit SHA
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

        # 7. Verify via hermes plugins list --json
        rec = _query_plugins_json(hermes_exe)
        report["plugin_record"] = rec
        status = rec.get("status", "")
        version = rec.get("version", "")
        print(f"[install] Plugin status: {status} (v{version})")

        if status != "enabled":
            report["errors"].append(f"Plugin status is '{status}', expected 'enabled'")

        # 8. Verify tools + hooks through Hermes Python
        verification = _verify_tools_and_hooks(python_exe)
        report["verification"] = verification
        print(f"[install] Tools: {verification['tools']}, Hooks: {verification['hooks']}")

        if verification["tools"] != _EXPECTED_TOOLS:
            report["errors"].append(
                f"Expected {_EXPECTED_TOOLS} tools, got {verification['tools']}"
            )
        if verification["hooks"] != _EXPECTED_HOOKS:
            report["errors"].append(f"Expected {_EXPECTED_HOOKS} hook, got {verification['hooks']}")

        # 9. Package info via Hermes Python
        pkg_info = _query_package_info(python_exe)
        report["package"] = pkg_info
        print(f"[install] Package: v{pkg_info['version']} at {pkg_info['path']}")

        # Final verdict
        if not report["errors"]:
            report["success"] = True
            print("[install] ✅ All checks passed")
        else:
            for err in report["errors"]:
                print(f"[install] ❌ {err}")

    except Exception as exc:
        report["errors"].append(str(exc))
        print(f"[install] ❌ Failed: {exc}", file=sys.stderr)

    # Never print secrets
    for key in ("entity_secret", "api_key", "private_key", "otp", "password"):
        report.pop(key, None)

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Install and verify hermes-x402 plugin")
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
        # Live test requires a successful install first
        report = run_install(check_only=False)
        if not report["success"]:
            print("\n[install] ❌ Live test aborted: installation/verification failed")
            sys.exit(1)

        print("\n[install] Installation verified. Starting live test...\n")
        from hermes_x402.live_test import run_live_test

        sys.exit(0 if run_live_test() else 1)

    report = run_install(check_only=args.check)
    sys.exit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
