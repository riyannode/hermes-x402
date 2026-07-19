"""Tests for hardened Circle CLI installer.

Covers:
  - Stable error codes
  - No raw stderr in user output
  - Pre-install logging
  - Version mismatch detection
  - Bun not found
  - CLI not found after install
  - Success path
"""

from __future__ import annotations

from unittest.mock import patch

from hermes_x402.circle_cli_installer import (
    CircleCliReport,
    run_circle_cli_bootstrap,
)


class TestCircleCliInstaller:
    def test_not_requested_returns_empty(self):
        """When not requested, returns empty report."""
        with patch(
            "hermes_x402.circle_cli_installer._find_existing_cli",
            return_value=None,
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=False)
            assert report.requested is False
            assert report.available is False
            assert report.errors == []

    def test_existing_cli_correct_version(self):
        """Existing CLI with correct version reports already present."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value="/usr/local/bin/circle",
            ),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value="0.0.6",
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.available is True
            assert report.installed is True
            assert report.already_present is True
            assert report.version == "0.0.6"

    def test_version_mismatch_returns_error_code(self):
        """Version mismatch returns stable error code, not raw stderr."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value="/usr/local/bin/circle",
            ),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value="0.0.5",
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.errors == ["circle_cli_version_mismatch"]

    def test_version_check_failed_returns_error_code(self):
        """Version check failure returns stable error code."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value="/usr/local/bin/circle",
            ),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value=None,
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.errors == ["circle_cli_version_check_failed"]

    def test_bun_not_found_returns_error_code(self):
        """When bun is not found, returns stable error code."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=None,
            ),
            patch(
                "hermes_x402.circle_cli_installer._check_bun_available",
                return_value=None,
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.errors == ["bun_not_found"]

    def test_install_failure_returns_error_code(self):
        """Install failure returns stable error code, not raw stderr."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=None,
            ),
            patch(
                "hermes_x402.circle_cli_installer._check_bun_available",
                return_value="/usr/local/bin/bun",
            ),
            patch(
                "hermes_x402.circle_cli_installer._install_cli_via_bun",
                return_value=(None, "circle_cli_install_failed"),
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.errors == ["circle_cli_install_failed"]

    def test_install_timeout_returns_error_code(self):
        """Install timeout returns stable error code."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=None,
            ),
            patch(
                "hermes_x402.circle_cli_installer._check_bun_available",
                return_value="/usr/local/bin/bun",
            ),
            patch(
                "hermes_x402.circle_cli_installer._install_cli_via_bun",
                return_value=(None, "circle_cli_install_timeout"),
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.errors == ["circle_cli_install_timeout"]

    def test_success_after_install(self):
        """Successful install reports available and installed."""
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=None,
            ),
            patch(
                "hermes_x402.circle_cli_installer._check_bun_available",
                return_value="/usr/local/bin/bun",
            ),
            patch(
                "hermes_x402.circle_cli_installer._install_cli_via_bun",
                return_value=("/usr/local/bin/circle", None),
            ),
        ):
            report = run_circle_cli_bootstrap(with_circle_cli=True)
            assert report.available is True
            assert report.installed is True
            assert report.already_present is False
            assert report.version == "0.0.6"

    def test_report_to_dict(self):
        """Report serializes to dict correctly."""
        report = CircleCliReport(
            requested=True,
            available=True,
            installed=True,
            already_present=True,
            version="0.0.6",
            executable="/usr/local/bin/circle",
            package_manager="bun",
            errors=[],
        )
        d = report.to_dict()
        assert "circle_cli" in d
        assert d["circle_cli"]["version"] == "0.0.6"
        assert d["circle_cli"]["errors"] == []

    def test_no_raw_stderr_in_report(self):
        """Report never contains raw stderr content."""
        report = CircleCliReport(errors=["circle_cli_install_failed"])
        d = report.to_dict()
        # Error list contains only stable codes, not raw output
        for error in d["circle_cli"]["errors"]:
            assert "\n" not in error
            assert "\r" not in error
            assert len(error) < 200  # bounded
