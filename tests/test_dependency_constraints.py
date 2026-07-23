"""Dependency constraint tests for hermes-x402.

Verifies:
  - dcw, all, dev extras contain cryptography>=42.0,<47
  - package version is 0.2.1
  - installer uses --no-deps for wheel installation
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
REQUIRED_CRYPTO_CONSTRAINT = "cryptography>=42.0,<47"
EXPECTED_VERSION = "0.2.1"


def _read_pyproject() -> str:
    return PYPROJECT.read_text()


def _parse_optional_deps(pyproject_text: str) -> dict[str, list[str]]:
    """Parse [project.optional-dependencies] from pyproject.toml text."""
    in_optional = False
    current_extra: str | None = None
    result: dict[str, list[str]] = {}

    for line in pyproject_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("[project.optional-dependencies"):
            in_optional = True
            continue

        if in_optional and stripped.startswith("["):
            # Hit the next section
            break

        if in_optional:
            m = re.match(r"^(\w[\w-]*)\s*=\s*\[", stripped)
            if m:
                current_extra = m.group(1)
                result[current_extra] = []
                continue

            if current_extra is not None:
                # Collect items from list lines
                dep_match = re.search(r'"([^"]+)"', stripped)
                if dep_match and current_extra is not None:
                    result[current_extra].append(dep_match.group(1))

    return result


# ---------------------------------------------------------------------------
# Test: cryptography constraint in extras
# ---------------------------------------------------------------------------


class TestCryptographyConstraint:
    """Verify all extras that include cryptography have the correct upper bound."""

    @pytest.fixture(autouse=True)
    def _load_pyproject(self):
        self.pyproject_text = _read_pyproject()
        self.deps = _parse_optional_deps(self.pyproject_text)

    def _assert_crypto_constraint(self, extra: str):
        assert extra in self.deps, f"Extra '{extra}' not found in pyproject.toml"
        crypto_deps = [d for d in self.deps[extra] if d.startswith("cryptography")]
        assert len(crypto_deps) == 1, (
            f"Expected exactly 1 cryptography dep in '{extra}', found {crypto_deps}"
        )
        assert crypto_deps[0] == REQUIRED_CRYPTO_CONSTRAINT, (
            f"Extra '{extra}': expected '{REQUIRED_CRYPTO_CONSTRAINT}', got '{crypto_deps[0]}'"
        )

    def test_dcw_extra_has_cryptography_constraint(self):
        self._assert_crypto_constraint("dcw")

    def test_all_extra_has_cryptography_constraint(self):
        self._assert_crypto_constraint("all")

    def test_dev_extra_has_cryptography_constraint(self):
        self._assert_crypto_constraint("dev")


# ---------------------------------------------------------------------------
# Test: package version
# ---------------------------------------------------------------------------


class TestPackageVersion:
    def test_version_matches_pyproject(self):
        """Installed package version should match pyproject.toml."""
        # Read from source since we're running from the repo
        text = _read_pyproject()
        m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
        assert m is not None, "version field not found in pyproject.toml"
        assert m.group(1) == EXPECTED_VERSION, (
            f"Expected version '{EXPECTED_VERSION}', got '{m.group(1)}'"
        )

    def test_init_version_matches(self):
        """__init__.py __version__ should match pyproject.toml."""
        init_path = Path(__file__).resolve().parent.parent / "hermes_x402" / "__init__.py"
        content = init_path.read_text()
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', content, re.MULTILINE)
        assert m is not None, "__version__ not found in __init__.py"
        assert m.group(1) == EXPECTED_VERSION, (
            f"Expected __version__ '{EXPECTED_VERSION}', got '{m.group(1)}'"
        )


# ---------------------------------------------------------------------------
# Test: installer uses --no-deps
# ---------------------------------------------------------------------------


class TestInstallerNoDeps:
    def test_install_wheel_uses_no_deps(self):
        """The _install_wheel function must use --no-deps --force-reinstall."""
        from hermes_x402.install import _install_wheel

        with patch("hermes_x402.install.subprocess.check_call") as mock_call:
            _install_wheel(
                Path("/usr/bin/python3"),
                Path("/tmp/hermes_x402-0.2.1-py3-none-any.whl"),
            )
            # Verify the call includes --no-deps
            args = mock_call.call_args[0][0]
            assert "--no-deps" in args, f"--no-deps not found in args: {args}"
            assert "--force-reinstall" in args, f"--force-reinstall not found in args: {args}"
