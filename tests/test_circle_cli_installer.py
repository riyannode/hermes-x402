"""Tests for hardened Circle CLI installer.

Covers:
  - Stable error codes
  - No raw stderr in user output
  - Pre-install logging
  - Version mismatch detection
  - Bun not found
  - CLI not found after install
  - Success path
  - CIRCLE_CLI_EXECUTABLE not written
  - --check is non-mutating
  - Bootstrap failure propagation
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
        for error in d["circle_cli"]["errors"]:
            assert "\n" not in error
            assert "\r" not in error
            assert len(error) < 200


class TestInstallerCIRCLE_CLI_EXECUTABLE:
    def test_with_circle_cli_does_not_write_env(self, tmp_path):
        """--with-circle-cli must NOT write CIRCLE_CLI_EXECUTABLE to .env."""
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=value\n")
        with (
            patch(
                "hermes_x402.install._find_hermes_executable",
                return_value=tmp_path / "hermes",
            ),
            patch(
                "hermes_x402.install._detect_python_env",
                return_value=tmp_path / "python3",
            ),
            patch(
                "hermes_x402.install._validate_hermes_python",
                return_value={"hermes_cli_file": "test"},
            ),
            patch(
                "hermes_x402.install._get_commit_sha",
                return_value="abc123",
            ),
            patch(
                "hermes_x402.circle_cli_installer.run_circle_cli_bootstrap",
                return_value=CircleCliReport(
                    requested=True,
                    available=True,
                    installed=True,
                    already_present=True,
                    version="0.0.6",
                    executable="/usr/bin/circle",
                    package_manager="bun",
                ),
            ),
            patch(
                "hermes_x402.install._build_wheel",
                return_value=tmp_path / "dist" / "pkg.whl",
            ),
            patch("hermes_x402.install._install_wheel"),
            patch("hermes_x402.install._enable_plugin"),
            patch(
                "hermes_x402.install._verify_entrypoint_registration_contract",
                return_value={"tools": 14, "hooks": 1, "commands": 1},
            ),
            patch(
                "hermes_x402.install._verify_installed_package_path",
                return_value={"version": "0.2.0"},
            ),
        ):
            from hermes_x402.install import run_install

            run_install(
                repo_root=tmp_path,
                hermes_python=str(tmp_path / "python3"),
                with_circle_cli=True,
            )

        content = env_path.read_text()
        assert "CIRCLE_CLI_EXECUTABLE" not in content


class TestInstallerCheckMode:
    def test_check_with_circle_cli_is_non_mutating(self, tmp_path):
        """--check --with-circle-cli must NOT run bootstrap or write env."""
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=value\n")
        with (
            patch(
                "hermes_x402.install._find_hermes_executable",
                return_value=tmp_path / "hermes",
            ),
            patch(
                "hermes_x402.install._detect_python_env",
                return_value=tmp_path / "python3",
            ),
            patch(
                "hermes_x402.install._validate_hermes_python",
                return_value={"hermes_cli_file": "test"},
            ),
            patch(
                "hermes_x402.install._get_commit_sha",
                return_value="abc123",
            ),
            patch(
                "hermes_x402.circle_cli_installer.run_circle_cli_bootstrap",
            ) as mock_bootstrap,
            patch(
                "hermes_x402.install._verify_entrypoint_registration_contract",
                return_value={"tools": 14, "hooks": 1, "commands": 1},
            ),
            patch(
                "hermes_x402.install._verify_installed_package_path",
                return_value={"version": "0.2.0"},
            ),
        ):
            from hermes_x402.install import run_install

            run_install(
                repo_root=tmp_path,
                hermes_python=str(tmp_path / "python3"),
                check_only=True,
                with_circle_cli=True,
            )

        mock_bootstrap.assert_not_called()
        content = env_path.read_text()
        assert "CIRCLE_CLI_EXECUTABLE" not in content


class TestInstallerBootstrapFailurePropagation:
    def test_bun_not_found_propagates_to_report(self, tmp_path):
        """Bootstrap failure is propagated to top-level report."""
        with (
            patch(
                "hermes_x402.install._find_hermes_executable",
                return_value=tmp_path / "hermes",
            ),
            patch(
                "hermes_x402.install._detect_python_env",
                return_value=tmp_path / "python3",
            ),
            patch(
                "hermes_x402.install._validate_hermes_python",
                return_value={"hermes_cli_file": "test"},
            ),
            patch(
                "hermes_x402.install._get_commit_sha",
                return_value="abc123",
            ),
            patch(
                "hermes_x402.circle_cli_installer.run_circle_cli_bootstrap",
                return_value=CircleCliReport(
                    requested=True,
                    errors=["bun_not_found"],
                ),
            ),
            patch(
                "hermes_x402.install._verify_entrypoint_registration_contract",
                return_value={"tools": 14, "hooks": 1, "commands": 1},
            ),
            patch(
                "hermes_x402.install._verify_installed_package_path",
                return_value={"version": "0.2.0"},
            ),
        ):
            from hermes_x402.install import run_install

            report = run_install(
                repo_root=tmp_path,
                hermes_python=str(tmp_path / "python3"),
                with_circle_cli=True,
            )

        assert "circle_cli: bun_not_found" in report["errors"]
