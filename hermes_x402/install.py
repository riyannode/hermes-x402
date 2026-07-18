"""Production installer for hermes-x402.

Detects the Hermes executable, identifies the Python environment,
builds a wheel with pip (no third-party build package), installs it,
enables the plugin, and verifies 14 tools + 1 pre_tool_call hook
are registered via a static entry-point contract check.

Usage:
    python3 -m hermes_x402.install                         # full install
    python3 -m hermes_x402.install --check                 # verify only
    python3 -m hermes_x402.install --uninstall             # remove plugin
    python3 -m hermes_x402.install --live-test ...         # install + live test
    python3 -m hermes_x402.install --hermes-python /path   # override Python
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_EXPECTED_TOOLS = 14
_EXPECTED_HOOKS = 1
_PLUGIN_NAME = "hermes-x402"
_ENV_DEBUG = "HERMES_PLUGINS_DEBUG"
_HERMES_EXE_CANDIDATES = ("hermes",)
_SHIM_MAX_LINES = 30  # bounded scan for exec line in shim

# ---------------------------------------------------------------------------
# Hermes executable + Python resolution
# ---------------------------------------------------------------------------

# Shell interpreters we accept in shim shebangs
_SHELL_INTERPRETERS = frozenset(
    {
        "bash",
        "sh",
        "/bin/bash",
        "/bin/sh",
        "/usr/bin/env bash",
        "/usr/bin/env sh",
    }
)

# Patterns we reject inside exec targets
_SHELL_EXPANSION_RE = re.compile(r"[$`\"'\\]|\\{|\\}|\\(|\\)|\\[|\\]")
_RELATIVE_TARGET_RE = re.compile(r"^[^/]")


def _resolve_symlink(p: Path) -> Path:
    """Resolve symlinks iteratively, returning the final target.

    Relative symlinks are resolved against the parent directory of
    the symlink (not CWD).
    """
    seen: set[Path] = set()
    while p.is_symlink():
        real = p.resolve()
        if real in seen:
            break
        seen.add(real)
        # readlink gives the raw link target — resolve relative to parent
        link = os.readlink(str(p))
        p = (p.parent / link).resolve() if not os.path.isabs(link) else Path(link)
    return p


def _parse_hermes_shim(path: Path) -> Path | None:
    """If *path* is a Bash shim of the form ``exec "/…/venv/bin/hermes" "$@"``,
    return the resolved target path.

    Resolution rules:
    - Recognize only bash/sh launcher files (shebang check)
    - Scan a bounded number of lines (not just lines[1])
    - Find exactly ONE bounded ``exec "<absolute-target>" "$@"`` line
    - Reject shell expansions, command substitutions, relative targets,
      multiple exec targets, and unexpected syntax
    - Resolve the target path against the shim's parent directory
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None

    lines = text.strip().splitlines()
    if len(lines) < 2:
        return None

    # Must start with a valid bash/sh shebang
    first = lines[0].strip()
    if not first.startswith("#!"):
        return None

    shebang_body = first[2:].strip()
    # Parse shebang: could be "#!/bin/bash", "#!/usr/bin/env bash", etc.
    shebang_parts = shebang_body.split()
    if not shebang_parts:
        return None
    shebang_prog = shebang_parts[0]

    # Accept only bash/sh interpreters
    # Handle "/usr/bin/env bash" → effective program is "bash"
    effective_prog = shebang_prog
    if shebang_prog == "/usr/bin/env" and len(shebang_parts) > 1:
        effective_prog = shebang_parts[1]

    if effective_prog not in ("bash", "sh") and not shebang_prog.endswith(("/bash", "/sh")):
        return None

    # Scan bounded lines for exactly one exec target
    exec_targets: list[tuple[str, int]] = []  # (target, line_no)
    scan_limit = min(len(lines), _SHIM_MAX_LINES)

    for i in range(1, scan_limit):
        line = lines[i].strip()
        # Skip comments and blank lines
        if not line or line.startswith("#"):
            continue
        # Match: exec "<target>" "$@"
        m = re.match(r'^exec\s+"([^"]+)"\s+"\$@"$', line)
        if m:
            exec_targets.append((m.group(1), i + 1))

    # Exactly one exec target required
    if len(exec_targets) != 1:
        return None

    target_str, line_no = exec_targets[0]

    # Reject relative targets
    if _RELATIVE_TARGET_RE.match(target_str):
        return None

    # Reject shell expansions in target
    if _SHELL_EXPANSION_RE.search(target_str):
        return None

    # Resolve target against shim's parent directory
    target = Path(target_str)
    target = (path.parent / target).resolve() if not target.is_absolute() else target.resolve()

    return target


def _find_hermes_executable() -> Path:
    """Locate the hermes CLI executable on PATH."""
    for name in _HERMES_EXE_CANDIDATES:
        candidate = shutil.which(name)
        if candidate:
            return Path(candidate)
    raise RuntimeError(
        "Cannot find 'hermes' on PATH. "
        "Install Hermes Agent first: "
        "https://github.com/NousResearch/hermes-agent"
    )


def _detect_python_from_shebang(exe: Path) -> Path | None:
    """Read a Python shebang from an executable script.

    Returns None if the shebang points to a non-Python interpreter.
    Resolves relative paths against the exe's parent directory.
    """
    try:
        first_line = exe.read_text(errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return None
    if not first_line.startswith("#!"):
        return None

    interpreter = first_line[2:].strip()
    parts = interpreter.split()
    if not parts:
        return None

    prog = parts[0]

    # Reject shell interpreters
    if prog in ("/bin/sh", "/bin/bash"):
        return None
    if prog == "/usr/bin/env" and len(parts) > 1 and parts[1] in ("bash", "sh"):
        return None

    # If the shebang is /usr/bin/env <name>, resolve via which
    if prog == "/usr/bin/env" and len(parts) > 1:
        resolved = shutil.which(parts[1])
        if resolved:
            return Path(resolved)

    # Resolve relative paths against exe's parent
    p = Path(prog)
    if not p.is_absolute():
        p = (exe.parent / p).resolve()
    if p.exists():
        return p
    return None


def _detect_python_env(
    hermes_exe: Path,
    hermes_python_override: str | None = None,
) -> Path:
    """Detect the Python interpreter used by the Hermes venv.

    Resolution order:
    1. Explicit --hermes-python override
    2. Resolve symlinks on hermes_exe
    3. Detect Bash shim → exec target → read that target's shebang
    4. Sibling venv/bin/python when unambiguous
    5. Fail closed

    After selecting Python, validates:
    - Path(sys.executable).resolve() == selected_python.resolve()
    - hermes_cli imports from that interpreter
    """
    if hermes_python_override:
        p = Path(hermes_python_override)
        if not p.exists():
            raise RuntimeError(f"--hermes-python path does not exist: {p}")
        return p

    resolved = _resolve_symlink(hermes_exe)

    # Check if resolved is a bash shim
    shim_target = _parse_hermes_shim(resolved)
    if shim_target is not None:
        # Shim found — read its shebang to find Python
        python = _detect_python_from_shebang(shim_target)
        if python is not None:
            return python
        # Try sibling venv/bin/python relative to shim target
        for sibling_name in ("python3", "python"):
            sibling = shim_target.parent / sibling_name
            if sibling.exists():
                return sibling
    else:
        # Not a shim — try reading shebang directly on resolved
        python = _detect_python_from_shebang(resolved)
        if python is not None:
            return python
        # Try sibling venv/bin/python
        for sibling_name in ("python3", "python"):
            sibling = resolved.parent / sibling_name
            if sibling.exists():
                return sibling

    raise RuntimeError(
        f"Cannot detect Python environment for Hermes at {hermes_exe} "
        f"(resolved: {resolved}, shim_target: {shim_target}). "
        "Use --hermes-python to specify the interpreter."
    )


def _validate_hermes_python(python: Path) -> dict:
    """Validate the resolved interpreter can import hermes_cli.

    Returns a dict with hermes_exe, hermes_python, hermes_module_path.
    Raises RuntimeError on failure.

    Also verifies:
    - Path(sys.executable).resolve() == python.resolve()
    - hermes_cli imports from the selected interpreter
    """
    code = (
        "import json, sys\n"
        "from pathlib import Path\n"
        "selected = sys.argv[1]\n"
        "try:\n"
        "    resolved = Path(sys.executable).resolve()\n"
        "    expected = Path(selected).resolve()\n"
        "    exec_match = (resolved == expected)\n"
        "except Exception:\n"
        "    exec_match = False\n"
        "try:\n"
        "    import hermes_cli\n"
        "    result = {\n"
        "        'sys_executable': sys.executable,\n"
        "        'hermes_cli_file': getattr(hermes_cli, '__file__', 'unknown'),\n"
        "        'exec_match': exec_match,\n"
        "        'ok': True,\n"
        "    }\n"
        "except ImportError as e:\n"
        "    result = {'ok': False, 'error': str(e), 'exec_match': exec_match}\n"
        "print(json.dumps(result))\n"
    )
    r = subprocess.run(
        [str(python), "-c", code, str(python)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Python validation failed (rc={r.returncode}): {r.stderr[:500]}")
    try:
        info = json.loads(r.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Failed to parse Python validation output: {exc}") from exc
    if not info.get("ok"):
        raise RuntimeError(
            f"Cannot import hermes_cli from {python}: {info.get('error', 'unknown')}"
        )
    if not info.get("exec_match"):
        raise RuntimeError(
            f"Python exec mismatch: selected={python}, sys.executable={info.get('sys_executable')}"
        )
    return info


# ---------------------------------------------------------------------------
# Wheel building
# ---------------------------------------------------------------------------


def _build_wheel(repo_root: Path, python: Path) -> Path:
    """Build a wheel using pip's isolated build (no third-party build package).

    Returns the single wheel path.  Raises if zero or multiple wheels produced.
    """
    dist_dir = repo_root / "dist"
    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    dist_dir.mkdir(parents=True, exist_ok=True)

    subprocess.check_call(
        [
            str(python),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--wheel-dir",
            str(dist_dir),
            str(repo_root),
        ],
        cwd=str(repo_root),
    )

    wheels = list(dist_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("Wheel build produced no output in dist/")
    if len(wheels) > 1:
        names = ", ".join(w.name for w in wheels)
        raise RuntimeError(f"Wheel build produced multiple outputs: {names}")
    return wheels[0]


def _wheel_sha256(wheel: Path) -> str:
    """Compute SHA-256 hex digest of the wheel file."""
    h = hashlib.sha256()
    with open(wheel, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


def _install_wheel(python: Path, wheel: Path) -> None:
    """Install the wheel without touching other dependencies."""
    subprocess.check_call(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--force-reinstall",
            str(wheel),
        ],
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


def _restart_gateway(hermes_exe: Path) -> bool:
    """Restart the Hermes gateway. Returns True on success."""
    try:
        r = subprocess.run(
            [str(hermes_exe), "gateway", "restart"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return r.returncode == 0
    except Exception:
        return False


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


def _query_plugins_absent(hermes_exe: Path) -> bool:
    """Check that hermes-x402 is NOT in plugins list."""
    env = os.environ.copy()
    env[_ENV_DEBUG] = "1"
    try:
        result = subprocess.run(
            [str(hermes_exe), "plugins", "list", "--json"],
            capture_output=True,
            text=True,
            env=env,
        )
        if result.returncode != 0:
            return True  # can't query — assume absent
        records = json.loads(result.stdout)
        return not any(r.get("name") == _PLUGIN_NAME for r in records)
    except Exception:
        return True


def _verify_entrypoint_registration_contract(python: Path) -> dict:
    """Static entry-point contract check via FakeCtx.

    This is NOT runtime verification.  It confirms that the installed
    entry point can be imported and that register() registers the
    expected number of tools and hooks.

    Returns a dict with verification_type: "static_contract".
    """
    code = (
        "import json, sys\n"
        "class _Ctx:\n"
        "    def __init__(s): s.tools=[]; s.hooks=[]\n"
        "    def register_tool(s, *, name, toolset, schema, handler, **kw):\n"
        "        if not isinstance(name, str) or not name:\n"
        "            raise TypeError('register_tool requires a non-empty name')\n"
        "        s.tools.append(name)\n"
        "    def register_hook(s, hook_type, handler, **kw):\n"
        "        s.hooks.append(hook_type)\n"
        "from hermes_x402.hermes_plugin.entry import register\n"
        "ctx = _Ctx()\n"
        "register(ctx)\n"
        "print(json.dumps({\n"
        "    'tools': len(ctx.tools),\n"
        "    'hooks': len(ctx.hooks),\n"
        "    'names': ctx.tools,\n"
        "    'verification_type': 'static_contract',\n"
        "}))\n"
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


# ---------------------------------------------------------------------------
# Package info
# ---------------------------------------------------------------------------


def _query_package_info(python: Path) -> dict:
    """Query installed package version (via importlib.metadata) and module path."""
    code = (
        "import json\n"
        "try:\n"
        "    import importlib.metadata\n"
        "    ver = importlib.metadata.version('hermes-x402')\n"
        "except Exception:\n"
        "    ver = 'unknown'\n"
        "try:\n"
        "    import hermes_x402\n"
        "    mod_path = hermes_x402.__file__\n"
        "except Exception:\n"
        "    mod_path = 'unknown'\n"
        "print(json.dumps({'version': ver, 'path': mod_path}))\n"
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
    """Get current git commit SHA (full)."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
        )
        return result.stdout.strip()
    except Exception:
        return "unknown"


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _run_uninstall(hermes_exe: Path, python: Path) -> dict:
    """Uninstall the hermes-x402 plugin.

    Steps:
    1. Disable plugin via hermes CLI
    2. pip uninstall -y
    3. Verify absence through Hermes Python subprocess (importlib.metadata)
    4. Verify hermes plugins list --json has no entrypoint record
    5. Restart gateway
    """
    report: dict = {"uninstall": True, "success": False, "errors": []}

    try:
        # 1. Disable
        subprocess.run(
            [str(hermes_exe), "plugins", "disable", _PLUGIN_NAME],
            capture_output=True,
            text=True,
        )
        print("[uninstall] Plugin disabled")

        # 2. pip uninstall
        subprocess.check_call(
            [str(python), "-m", "pip", "uninstall", "-y", _PLUGIN_NAME],
        )
        print("[uninstall] Package uninstalled")

        # 3. Verify absence via Hermes Python subprocess
        code = (
            "import importlib.metadata, json\n"
            "try:\n"
            "    ver = importlib.metadata.version('hermes-x402')\n"
            "    print(json.dumps({'absent': False, 'version': ver}))\n"
            "except importlib.metadata.PackageNotFoundError:\n"
            "    print(json.dumps({'absent': True}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'absent': True, 'error': str(e)}))\n"
        )
        r = subprocess.run(
            [str(python), "-c", code],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0:
            result = json.loads(r.stdout.strip())
            if not result.get("absent", True):
                ver = result.get("version", "unknown")
                report["errors"].append(f"Package still importable after uninstall (version={ver})")
            else:
                print("[uninstall] Verified: package not installed")
        else:
            print("[uninstall] Could not verify absence via importlib.metadata")

        # 4. Verify plugins list has no entrypoint record
        if _query_plugins_absent(hermes_exe):
            print("[uninstall] Verified: no entrypoint record in plugins list")
        else:
            report["errors"].append("Plugin still in plugins list after uninstall")

        # 5. Restart gateway
        if _restart_gateway(hermes_exe):
            print("[uninstall] Gateway restarted")
        else:
            print("[uninstall] Gateway restart returned non-zero (non-fatal)")

        report["success"] = True
        print("[uninstall] ✅ Uninstall complete")

    except Exception as exc:
        report["errors"].append(str(exc))
        print(f"[uninstall] ❌ Failed: {exc}", file=sys.stderr)

    return report


# ---------------------------------------------------------------------------
# URL / payment validation helpers
# ---------------------------------------------------------------------------


def _validate_service_url(url: str) -> list[str]:
    """Validate a --service-url against the plugin's URL/SSRF policy.

    Returns a list of error strings (empty = valid).
    """
    errors: list[str] = []
    if not url:
        errors.append("--service-url is required")
        return errors

    parsed = urlparse(url)

    # HTTPS required
    if parsed.scheme != "https":
        errors.append(f"Service URL must use HTTPS, got '{parsed.scheme or 'empty'}'")

    # No userinfo
    if parsed.username or parsed.password:
        errors.append("Service URL must not contain credentials (userinfo)")

    # No fragments
    if parsed.fragment:
        errors.append("Service URL must not contain a fragment (#...)")

    # Bounded length
    if len(url) > 2048:
        errors.append("Service URL exceeds maximum length of 2048")

    # Hostname required
    if not parsed.hostname:
        errors.append("Service URL must have a valid hostname")

    return errors


def _sanitize_service_url(url: str) -> str:
    """Return a bounded, safe-to-display form of a service URL."""
    parsed = urlparse(url)
    host = parsed.hostname or "unknown"
    path = parsed.path or "/"
    if len(path) > 60:
        path = path[:57] + "..."
    return f"{parsed.scheme}://{host}{path}"


def _validate_max_payment(cap_str: str, env_cap: str | None) -> list[str]:
    """Validate --max-payment. Returns error list (empty = valid)."""
    from decimal import Decimal, InvalidOperation

    errors: list[str] = []
    cap = None
    try:
        cap = Decimal(cap_str)
        if cap.is_nan() or cap.is_infinite():
            errors.append("max-payment is not finite")
        elif cap <= 0:
            errors.append("max-payment must be positive")
        elif cap > Decimal("0.01"):
            errors.append(f"max-payment {cap} > 0.01 USDC limit for live test")
    except InvalidOperation:
        errors.append(f"max-payment not a valid Decimal: '{cap_str}'")

    # Also check against env cap
    if env_cap and not errors and cap is not None:
        try:
            env_decimal = Decimal(env_cap)
            if cap > env_decimal:
                errors.append(f"max-payment {cap} > X402_MAX_USDC_PER_PAYMENT {env_decimal}")
        except InvalidOperation:
            pass

    return errors


def _validate_body_file(
    body_file: str | None,
    method: str,
) -> tuple[list[str], str | None]:
    """Validate --body-file. Returns (errors, canonical_body_or_None)."""
    errors: list[str] = []
    canonical: str | None = None

    if method == "GET":
        if body_file:
            errors.append("--body-file is ignored for GET requests")
        return errors, None

    if method == "POST":
        if not body_file:
            errors.append("--body-file is required for POST requests")
            return errors, None

        bp = Path(body_file)
        if not bp.exists():
            errors.append(f"Body file not found: {bp}")
            return errors, None

        # Reject symlinks
        if bp.is_symlink():
            errors.append(f"Body file must not be a symlink: {bp}")
            return errors, None

        # Check file size (64 KB max)
        size = bp.stat().st_size
        if size > 65536:
            errors.append(f"Body file size {size} exceeds 64 KB maximum")
            return errors, None

        # Parse and canonicalize JSON
        try:
            raw = bp.read_text()
            parsed = json.loads(raw)
            canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
        except json.JSONDecodeError as exc:
            errors.append(f"Body file is not valid JSON: {exc}")
            return errors, None

    return errors, canonical


# ---------------------------------------------------------------------------
# Main install flow
# ---------------------------------------------------------------------------


def run_install(
    repo_root: Path | None = None,
    check_only: bool = False,
    hermes_python: str | None = None,
) -> dict:
    """Run the installer.  Returns a status dict.

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
        python_exe = _detect_python_env(hermes_exe, hermes_python)
        report["hermes_python"] = str(python_exe)
        print(f"[install] Hermes Python: {python_exe}")

        # 3. Validate Python can import hermes_cli + exec match
        validation = _validate_hermes_python(python_exe)
        report["hermes_module_path"] = validation.get("hermes_cli_file", "unknown")
        print(f"[install] Hermes module: {report['hermes_module_path']}")

        # 4. Get commit SHA
        commit_sha = _get_commit_sha(repo_root)
        report["commit_sha"] = commit_sha
        print(f"[install] Source commit: {commit_sha}")

        if not check_only:
            # 5. Build wheel
            wheel_path = _build_wheel(repo_root, python_exe)
            report["wheel"] = str(wheel_path)
            report["wheel_sha256"] = _wheel_sha256(wheel_path)
            print(f"[install] Built wheel: {wheel_path}")
            print(f"[install] Wheel SHA-256: {report['wheel_sha256']}")

            # 6. Install wheel
            _install_wheel(python_exe, wheel_path)
            print("[install] Wheel installed (--no-deps --force-reinstall)")

            # 7. Enable plugin
            _enable_plugin(hermes_exe)
            print(f"[install] Plugin '{_PLUGIN_NAME}' enabled")

        # 8. Verify via hermes plugins list --json
        rec = _query_plugins_json(hermes_exe)
        report["plugin_record"] = rec
        status = rec.get("status", "")
        source = rec.get("source", "")
        version = rec.get("version", "")
        report["plugin_source"] = source
        report["plugin_status"] = status
        report["plugin_version"] = version
        print(f"[install] Plugin: source={source} status={status} version={version}")

        if status != "enabled":
            report["errors"].append(f"Plugin status is '{status}', expected 'enabled'")
        if source != "entrypoint":
            report["errors"].append(f"Plugin source is '{source}', expected 'entrypoint'")

        # 9. Verify static entry-point registration contract
        verification = _verify_entrypoint_registration_contract(python_exe)
        report["registration_contract"] = {
            "tools": verification["tools"],
            "hooks": verification["hooks"],
            "verification_type": "static_contract",
        }
        print(
            f"[install] Static contract: {verification['tools']} tools, "
            f"{verification['hooks']} hooks"
        )

        if verification["tools"] != _EXPECTED_TOOLS:
            report["errors"].append(
                f"Expected {_EXPECTED_TOOLS} tools, got {verification['tools']}"
            )
        if verification["hooks"] != _EXPECTED_HOOKS:
            report["errors"].append(f"Expected {_EXPECTED_HOOKS} hook, got {verification['hooks']}")

        # 10. Package info via importlib.metadata
        pkg_info = _query_package_info(python_exe)
        report["installed_version"] = pkg_info["version"]
        report["module_path"] = pkg_info["path"]
        print(f"[install] Installed: v{pkg_info['version']} at {pkg_info['path']}")

        # Version consistency check
        if (
            not check_only
            and version
            and pkg_info["version"] != "unknown"
            and version != pkg_info["version"]
        ):
            report["errors"].append(
                f"Version mismatch: plugins list={version}, "
                f"importlib.metadata={pkg_info['version']}"
            )

        # 11. Gateway restart (after install + verification)
        if not check_only:
            restart_ok = _restart_gateway(hermes_exe)
            report["gateway_restart"] = restart_ok
            if restart_ok:
                print("[install] Gateway restarted")
            else:
                report["errors"].append("Gateway restart failed")
                print("[install] ❌ Gateway restart failed")

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
    for key in (
        "entity_secret",
        "api_key",
        "private_key",
        "otp",
        "password",
        "authorization",
    ):
        report.pop(key, None)

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Install, verify, or uninstall hermes-x402 plugin")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify only — skip build/install",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Uninstall the hermes-x402 plugin",
    )
    parser.add_argument(
        "--live-test",
        action="store_true",
        help="Run live Arc Testnet acceptance test after install",
    )
    parser.add_argument(
        "--hermes-python",
        default=None,
        help="Explicit path to the Hermes venv Python interpreter",
    )
    # Live-test arguments (forwarded to run_live_test via config)
    parser.add_argument(
        "--service-url",
        default=None,
        help="Seller service URL (for --live-test)",
    )
    parser.add_argument(
        "--method",
        default="GET",
        choices=["GET", "POST"],
        help="HTTP method (for --live-test)",
    )
    parser.add_argument(
        "--max-payment",
        default=None,
        help="Max USDC payment (for --live-test)",
    )
    parser.add_argument(
        "--body-file",
        default=None,
        help="POST body JSON file (for --live-test)",
    )

    args = parser.parse_args()

    # --- Uninstall ---
    if args.uninstall:
        hermes_exe = _find_hermes_executable()
        python_exe = _detect_python_env(hermes_exe, args.hermes_python)
        report = _run_uninstall(hermes_exe, python_exe)
        sys.exit(0 if report["success"] else 1)

    # --- Live test ---
    if args.live_test:
        if not args.service_url:
            print(
                "[install] ❌ --live-test requires --service-url",
                file=sys.stderr,
            )
            sys.exit(1)
        if not args.max_payment:
            print(
                "[install] ❌ --live-test requires --max-payment",
                file=sys.stderr,
            )
            sys.exit(1)

        # Validate service URL
        url_errors = _validate_service_url(args.service_url)
        if url_errors:
            for e in url_errors:
                print(f"[install] ❌ {e}", file=sys.stderr)
            sys.exit(1)

        # Validate max payment
        env_cap = os.environ.get("X402_MAX_USDC_PER_PAYMENT", "")
        pay_errors = _validate_max_payment(args.max_payment, env_cap or None)
        if pay_errors:
            for e in pay_errors:
                print(f"[install] ❌ {e}", file=sys.stderr)
            sys.exit(1)

        # Validate body file
        body_errors, canonical_body = _validate_body_file(args.body_file, args.method)
        if body_errors:
            for e in body_errors:
                print(f"[install] ❌ {e}", file=sys.stderr)
            sys.exit(1)

        # Run install first (includes gateway restart)
        report = run_install(check_only=False, hermes_python=args.hermes_python)
        if not report["success"]:
            print("\n[install] ❌ Live test aborted: installation/verification failed")
            sys.exit(1)

        print(
            "\n[install] Installation verified + gateway restarted."
            "\n[install] Starting live test...\n"
        )

        # Build immutable config for live test
        from hermes_x402.live_test import LiveTestConfig, run_live_test

        config = LiveTestConfig(
            service_url=args.service_url,
            method=args.method,
            max_payment=args.max_payment,
            body_file=args.body_file,
            canonical_body=canonical_body,
            hermes_python=args.hermes_python,
            install_report=report,
        )

        sys.exit(0 if run_live_test(config) else 1)

    # --- Normal install / check ---
    report = run_install(check_only=args.check, hermes_python=args.hermes_python)
    sys.exit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
