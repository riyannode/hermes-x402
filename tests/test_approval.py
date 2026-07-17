"""Tests for new-host approval (hermes_x402.buyer.approval)."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from hermes_x402.buyer.approval import (
    TrustedHostStore,
    check_approval_required,
    is_host_trusted,
    list_trusted_hosts,
    parse_approval_config,
    trust_host,
    untrust_host,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tmp_store(tmp_path: Path) -> TrustedHostStore:
    return TrustedHostStore(path=tmp_path / "trusted.json")


# ---------------------------------------------------------------------------
# TrustedHostStore.is_trusted
# ---------------------------------------------------------------------------


class TestTrustedHostStore:
    def test_empty_store_not_trusted(self, tmp_path):
        store = _tmp_store(tmp_path)
        assert store.is_trusted("example.com") is False

    def test_trust_adds_host(self, tmp_path):
        store = _tmp_store(tmp_path)
        store.trust("example.com")
        assert store.is_trusted("example.com") is True

    def test_trust_case_insensitive(self, tmp_path):
        store = _tmp_store(tmp_path)
        store.trust("Example.COM")
        assert store.is_trusted("example.com") is True
        assert store.is_trusted("EXAMPLE.COM") is True

    def test_untrust_removes_host(self, tmp_path):
        store = _tmp_store(tmp_path)
        store.trust("example.com")
        assert store.is_trusted("example.com") is True
        store.untrust("example.com")
        assert store.is_trusted("example.com") is False

    def test_untrust_nonexistent_is_noop(self, tmp_path):
        store = _tmp_store(tmp_path)
        store.untrust("never-trusted.com")
        assert store.is_trusted("never-trusted.com") is False

    def test_list_trusted_returns_sorted(self, tmp_path):
        store = _tmp_store(tmp_path)
        store.trust("z.com")
        store.trust("a.com")
        store.trust("m.com")
        result = store.list_trusted()
        assert result == ["a.com", "m.com", "z.com"]


# ---------------------------------------------------------------------------
# File-based persistence
# ---------------------------------------------------------------------------


class TestFilePersistence:
    def test_trust_persists_to_file(self, tmp_path):
        path = tmp_path / "trusted.json"
        store = TrustedHostStore(path=path)
        store.trust("example.com")

        # Read file directly
        data = json.loads(path.read_text())
        assert "trusted_hosts" in data
        assert "example.com" in data["trusted_hosts"]

    def test_load_from_existing_file(self, tmp_path):
        path = tmp_path / "trusted.json"
        path.write_text(json.dumps({"trusted_hosts": ["existing.com"]}))

        store = TrustedHostStore(path=path)
        assert store.is_trusted("existing.com") is True

    def test_load_from_list_format(self, tmp_path):
        path = tmp_path / "trusted.json"
        path.write_text(json.dumps(["old-format.com"]))

        store = TrustedHostStore(path=path)
        assert store.is_trusted("old-format.com") is True

    def test_missing_file_is_empty(self, tmp_path):
        path = tmp_path / "nonexistent.json"
        store = TrustedHostStore(path=path)
        assert store.is_trusted("anything.com") is False

    def test_corrupt_file_is_empty(self, tmp_path):
        path = tmp_path / "corrupt.json"
        path.write_text("not json{{{")

        store = TrustedHostStore(path=path)
        assert store.is_trusted("anything.com") is False

    def test_untrust_persists(self, tmp_path):
        path = tmp_path / "trusted.json"
        path.write_text(json.dumps({"trusted_hosts": ["example.com"]}))

        store = TrustedHostStore(path=path)
        store.untrust("example.com")

        data = json.loads(path.read_text())
        assert "example.com" not in data["trusted_hosts"]


# ---------------------------------------------------------------------------
# Module-level functions
# ---------------------------------------------------------------------------


class TestModuleFunctions:
    def test_trust_host(self, tmp_path):
        with patch(
            "hermes_x402.buyer.approval._TRUSTED_HOSTS_FILE",
            tmp_path / "trusted.json",
        ):
            # Reset singleton
            import hermes_x402.buyer.approval as mod

            mod._store = None

            trust_host("test-host.com")
            assert is_host_trusted("test-host.com") is True
            assert "test-host.com" in list_trusted_hosts()

    def test_untrust_host(self, tmp_path):
        with patch(
            "hermes_x402.buyer.approval._TRUSTED_HOSTS_FILE",
            tmp_path / "trusted.json",
        ):
            import hermes_x402.buyer.approval as mod

            mod._store = None

            trust_host("test-host.com")
            untrust_host("test-host.com")
            assert is_host_trusted("test-host.com") is False


# ---------------------------------------------------------------------------
# check_approval_required
# ---------------------------------------------------------------------------


class TestCheckApprovalRequired:
    def test_returns_none_when_disabled(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "false")
        result = check_approval_required("https://example.com/data")
        assert result is None

    def test_returns_none_when_not_set(self, monkeypatch):
        monkeypatch.delenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", raising=False)
        result = check_approval_required("https://example.com/data")
        assert result is None

    def test_returns_dict_for_untrusted_host(self, tmp_path):
        path = tmp_path / "trusted.json"
        with patch("hermes_x402.buyer.approval._TRUSTED_HOSTS_FILE", path):
            import hermes_x402.buyer.approval as mod

            mod._store = None

            with patch.dict("os.environ", {"X402_REQUIRE_APPROVAL_FOR_NEW_HOST": "true"}):
                result = check_approval_required("https://untrusted.com/data")

        assert result is not None
        assert result["error"] == "approval_required"
        assert result["host"] == "untrusted.com"
        assert result["new_host"] is True

    def test_returns_none_for_trusted_host(self, tmp_path):
        path = tmp_path / "trusted.json"
        with patch("hermes_x402.buyer.approval._TRUSTED_HOSTS_FILE", path):
            import hermes_x402.buyer.approval as mod

            mod._store = None

            trust_host("trusted.com")

            with patch.dict("os.environ", {"X402_REQUIRE_APPROVAL_FOR_NEW_HOST": "true"}):
                result = check_approval_required("https://trusted.com/data")

        assert result is None

    def test_config_overrides_env(self, tmp_path):
        path = tmp_path / "trusted.json"
        with patch("hermes_x402.buyer.approval._TRUSTED_HOSTS_FILE", path):
            import hermes_x402.buyer.approval as mod

            mod._store = None

            config = MagicMock()
            config.require_approval = False

            with patch.dict("os.environ", {"X402_REQUIRE_APPROVAL_FOR_NEW_HOST": "true"}):
                result = check_approval_required("https://example.com/data", config=config)

        assert result is None


# ---------------------------------------------------------------------------
# parse_approval_config
# ---------------------------------------------------------------------------


class TestParseApprovalConfig:
    def test_true(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "true")
        cfg = parse_approval_config()
        assert cfg["require_approval"] is True

    def test_false(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "false")
        cfg = parse_approval_config()
        assert cfg["require_approval"] is False

    def test_absent(self, monkeypatch):
        monkeypatch.delenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", raising=False)
        cfg = parse_approval_config()
        assert cfg["require_approval"] is False

    def test_one(self, monkeypatch):
        monkeypatch.setenv("X402_REQUIRE_APPROVAL_FOR_NEW_HOST", "1")
        cfg = parse_approval_config()
        assert cfg["require_approval"] is True


# ---------------------------------------------------------------------------
# Thread safety (basic)
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_trust(self, tmp_path):
        store = TrustedHostStore(path=tmp_path / "trusted.json")
        errors: list[Exception] = []

        def add_host(i: int):
            try:
                store.trust(f"host{i}.com")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=add_host, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(store.list_trusted()) == 20
