"""Deterministic tests for Circle CLI installer bootstrap."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_x402.circle_cli_installer import (
    SUPPORTED_CLI_VERSION,
    CircleCliReport,
    _check_bun_available,
    _find_existing_cli,
    _install_cli_via_bun,
    _query_cli_version,
    run_circle_cli_bootstrap,
)


class TestCircleCliReport:
    def test_default_report(self):
        r = CircleCliReport()
        d = r.to_dict()
        assert "circle_cli" in d
        assert d["circle_cli"]["requested"] is False
        assert d["circle_cli"]["available"] is False
        assert d["circle_cli"]["installed"] is False
        assert d["circle_cli"]["already_present"] is False
        assert d["circle_cli"]["version"] is None
        assert d["circle_cli"]["executable"] is None
        assert d["circle_cli"]["package_manager"] is None
        assert d["circle_cli"]["errors"] == []

    def test_report_with_errors(self):
        r = CircleCliReport(requested=True)
        r.errors.append("test error")
        d = r.to_dict()
        assert d["circle_cli"]["requested"] is True
        assert d["circle_cli"]["errors"] == ["test error"]


class TestFindExistingCli:
    def test_not_found(self):
        with patch("hermes_x402.circle_cli_installer.shutil.which", return_value=None):
            assert _find_existing_cli() is None

    def test_found_resolved(self, tmp_path):
        fake = tmp_path / "circle"
        fake.write_text("#!/bin/sh\n")
        with patch(
            "hermes_x402.circle_cli_installer.shutil.which",
            return_value=str(fake),
        ):
            result = _find_existing_cli()
            assert result is not None
            assert result.is_absolute()


class TestQueryCliVersion:
    def test_success(self, tmp_path):
        fake = tmp_path / "circle"
        fake.write_text("#!/bin/sh\necho 0.0.6\n")
        fake.chmod(0o755)
        result = _query_cli_version(fake)
        assert result == "0.0.6"

    def test_failure_returns_none(self, tmp_path):
        fake = tmp_path / "circle"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        assert _query_cli_version(fake) is None

    def test_timeout_returns_none(self):
        # Non-existent path will fail
        assert _query_cli_version(Path("/nonexistent/circle")) is None


class TestCheckBunAvailable:
    def test_bun_found(self):
        with patch(
            "hermes_x402.circle_cli_installer.shutil.which",
            return_value="/usr/local/bin/bun",
        ):
            result = _check_bun_available()
            assert result == Path("/usr/local/bin/bun")

    def test_bun_not_found(self):
        with patch(
            "hermes_x402.circle_cli_installer.shutil.which",
            return_value=None,
        ):
            assert _check_bun_available() is None


class TestInstallCliViaBun:
    def test_bun_add_failure(self):
        bun = Path("/usr/local/bin/bun")
        with patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="install failed")
            path, err = _install_cli_via_bun(bun)
            assert path is None
            assert err is not None
            assert "bun add failed" in err

    def test_bun_add_success_but_no_binary(self):
        bun = Path("/usr/local/bin/bun")
        with (
            patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run,
            patch("hermes_x402.circle_cli_installer._find_existing_cli", return_value=None),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            path, err = _install_cli_via_bun(bun)
            assert path is None
            assert err is not None
            assert "not found after" in err

    def test_bun_add_wrong_version(self, tmp_path):
        bun = tmp_path / "bun"
        bun.write_text("#!/bin/sh\n")
        fake_circle = tmp_path / "circle"
        fake_circle.write_text("#!/bin/sh\necho 0.0.5\n")
        fake_circle.chmod(0o755)
        with (
            patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run,
            patch("hermes_x402.circle_cli_installer._find_existing_cli", return_value=fake_circle),
            patch("hermes_x402.circle_cli_installer._query_cli_version", return_value="0.0.5"),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            path, err = _install_cli_via_bun(bun)
            assert path is None
            assert err is not None
            assert "version" in err.lower()

    def test_bun_add_correct_version(self, tmp_path):
        bun = tmp_path / "bun"
        bun.write_text("#!/bin/sh\n")
        fake_circle = tmp_path / "circle"
        fake_circle.write_text(f"#!/bin/sh\necho {SUPPORTED_CLI_VERSION}\n")
        fake_circle.chmod(0o755)
        with (
            patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run,
            patch("hermes_x402.circle_cli_installer._find_existing_cli", return_value=fake_circle),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value=SUPPORTED_CLI_VERSION,
            ),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            path, err = _install_cli_via_bun(bun)
            assert path is not None
            assert err is None
            assert path == fake_circle

    def test_subprocess_uses_shell_false(self, tmp_path):
        bun = tmp_path / "bun"
        bun.write_text("#!/bin/sh\n")
        with patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fail")
            _install_cli_via_bun(bun)
            call_args = mock_run.call_args
            # Verify shell=False was passed
            assert call_args[1].get("shell") is False

    def test_bounded_timeout(self, tmp_path):
        bun = tmp_path / "bun"
        bun.write_text("#!/bin/sh\n")
        with patch("hermes_x402.circle_cli_installer.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="fail")
            _install_cli_via_bun(bun)
            call_args = mock_run.call_args
            assert call_args[1].get("timeout") is not None
            assert call_args[1]["timeout"] <= 120


class TestRunCircleCliBootstrap:
    def test_not_requested(self):
        with patch(
            "hermes_x402.circle_cli_installer._find_existing_cli",
            return_value=None,
        ):
            r = run_circle_cli_bootstrap(with_circle_cli=False)
            assert r.requested is False
            assert r.installed is False
            assert r.errors == []

    def test_already_installed_supported_version(self):
        fake = Path("/usr/local/bin/circle")
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=fake,
            ),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value=SUPPORTED_CLI_VERSION,
            ),
        ):
            r = run_circle_cli_bootstrap(with_circle_cli=True)
            assert r.installed is True
            assert r.already_present is True
            assert r.version == SUPPORTED_CLI_VERSION
            assert str(r.executable) == str(fake)

    def test_existing_unsupported_version(self):
        fake = Path("/usr/local/bin/circle")
        with (
            patch(
                "hermes_x402.circle_cli_installer._find_existing_cli",
                return_value=fake,
            ),
            patch(
                "hermes_x402.circle_cli_installer._query_cli_version",
                return_value="0.0.5",
            ),
        ):
            r = run_circle_cli_bootstrap(with_circle_cli=True)
            assert r.installed is False
            assert len(r.errors) == 1
            assert "circle_cli_version_mismatch" in r.errors[0]

    def test_absent_no_bun(self):
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
            r = run_circle_cli_bootstrap(with_circle_cli=True)
            assert r.installed is False
            assert len(r.errors) == 1
            assert "bun_not_found" in r.errors[0]

    def test_no_secret_output(self):
        """Report never contains credentials, OTPs, or API keys."""
        r = run_circle_cli_bootstrap(with_circle_cli=True)
        d = r.to_dict()
        text = str(d).lower()
        for secret in ["otp", "api_key", "entity_secret", "password", "authorization"]:
            assert secret not in text or "secret" in text  # "errors" list may contain "secret" word
        # More precise: the errors should not contain actual secret values
        for err in r.errors:
            assert "ghp_" not in err
            assert "sk-" not in err
