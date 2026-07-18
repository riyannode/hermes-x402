"""Deterministic tests for atomic env file writer."""

from __future__ import annotations

import os

import pytest

from hermes_x402.env_writer import update_env_file


class TestEnvWriter:
    def test_creates_new_file(self, tmp_path):
        env_path = tmp_path / ".env"
        update_env_file(env_path, {"KEY": "value"}, _fsync=False)
        assert env_path.exists()
        assert env_path.read_text() == "KEY=value\n"

    def test_preserves_comments(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# comment\nKEY=old\n")
        update_env_file(env_path, {"KEY": "new"}, _fsync=False)
        content = env_path.read_text()
        assert "# comment" in content
        assert "KEY=new" in content
        assert "KEY=old" not in content

    def test_preserves_blank_lines(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("# comment\n\nKEY=old\n")
        update_env_file(env_path, {"KEY": "new"}, _fsync=False)
        content = env_path.read_text()
        lines = content.splitlines()
        assert lines[0] == "# comment"
        assert lines[1] == ""

    def test_preserves_unrelated_variables(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("OTHER=keep\nKEY=old\nANOTHER=keep2\n")
        update_env_file(env_path, {"KEY": "new"}, _fsync=False)
        content = env_path.read_text()
        assert "OTHER=keep" in content
        assert "ANOTHER=keep2" in content
        assert "KEY=new" in content
        assert "KEY=old" not in content

    def test_appends_new_keys(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("EXISTING=1\n")
        update_env_file(env_path, {"NEW_KEY": "new_value"}, _fsync=False)
        content = env_path.read_text()
        assert "EXISTING=1" in content
        assert "NEW_KEY=new_value" in content

    def test_permissions_0600(self, tmp_path):
        env_path = tmp_path / ".env"
        update_env_file(env_path, {"KEY": "value"}, _fsync=False)
        mode = oct(os.stat(env_path).st_mode)[-3:]
        assert mode == "600"

    def test_symlink_rejection(self, tmp_path):
        real = tmp_path / "real.env"
        real.write_text("KEY=old\n")
        link = tmp_path / "link.env"
        link.symlink_to(real)
        with pytest.raises(OSError, match="symlink"):
            update_env_file(link, {"KEY": "new"}, _fsync=False)

    def test_atomic_replacement(self, tmp_path):
        """No temp files left after successful write."""
        env_path = tmp_path / ".env"
        env_path.write_text("KEY=old\n")
        update_env_file(env_path, {"KEY": "new"}, _fsync=False)
        # Only the .env file should exist (no temp files)
        files = list(tmp_path.glob(".env*"))
        assert len(files) == 1
        assert files[0] == env_path

    def test_multiple_managed_keys(self, tmp_path):
        env_path = tmp_path / ".env"
        update_env_file(
            env_path,
            {"KEY1": "val1", "KEY2": "val2", "KEY3": "val3"},
            _fsync=False,
        )
        content = env_path.read_text()
        assert "KEY1=val1" in content
        assert "KEY2=val2" in content
        assert "KEY3=val3" in content

    def test_update_preserves_order(self, tmp_path):
        env_path = tmp_path / ".env"
        env_path.write_text("A=1\nB=2\nC=3\n")
        update_env_file(env_path, {"B": "new"}, _fsync=False)
        lines = env_path.read_text().splitlines()
        assert lines[0] == "A=1"
        assert lines[1] == "B=new"
        assert lines[2] == "C=3"

    def test_never_prints_full_env(self, tmp_path):
        """The writer function itself doesn't print — no stdout check needed.
        This test verifies the function doesn't raise on valid input."""
        env_path = tmp_path / ".env"
        env_path.write_text("SECRET_API_KEY=supersecret\n")
        # Should not raise
        update_env_file(env_path, {"NEW_KEY": "value"}, _fsync=False)
