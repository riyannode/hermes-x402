"""Asynchronous, allowlisted Circle CLI process runner."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from hermes_x402.circle_cli.errors import (
    CircleCliNotInstalledError,
    CircleCliOutputError,
    CircleCliTimeoutError,
    CircleCliUnsupportedCapabilityError,
)
from hermes_x402.circle_cli.models import CircleCliResult, Operation

_MAX_OUTPUT_BYTES = 256 * 1024
_TERMINATE_GRACE_SECONDS = 3
_SAFE_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE")


class CircleCliRunner:
    """Run only the Circle CLI operations implemented by :class:`CircleCliClient`.

    This is intentionally not a generic command executor. Arguments are passed as
    an argv vector to ``create_subprocess_exec`` and the narrow command validator
    rejects wallet mutation, Terms, login, and arbitrary process execution.
    """

    def __init__(
        self,
        *,
        executable: str = "circle",
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        read_timeout_seconds: float = 30,
        payment_timeout_seconds: float = 120,
    ):
        self.executable = executable
        self.cwd = cwd
        self.env = dict(env or {})
        self.read_timeout_seconds = read_timeout_seconds
        self.payment_timeout_seconds = payment_timeout_seconds

    @staticmethod
    def _validate_args(args: Sequence[str]) -> tuple[str, ...]:
        argv = tuple(args)
        allowed = (
            ("--version",),
            ("blockchain", "list"),
            ("wallet", "status"),
            ("wallet", "list"),
            ("wallet", "balance"),
            ("wallet", "login"),
            ("services", "search"),
            ("services", "inspect"),
            ("services", "pay"),
            ("gateway", "balance"),
            ("gateway", "deposit"),
        )
        if not argv or not any(argv[: len(prefix)] == prefix for prefix in allowed):
            raise CircleCliUnsupportedCapabilityError("Circle CLI operation is not allowlisted")
        if any(value in {"terms", "transfer", "execute"} for value in argv):
            raise CircleCliUnsupportedCapabilityError("Circle CLI mutation is not allowlisted")
        return argv

    @staticmethod
    def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        """Return a copy of *argv* with any ``--otp`` value replaced by [REDACTED].

        The real OTP is still passed to the subprocess; this redacted form is
        stored in :class:`CircleCliResult` so it never leaks via logs, repr,
        or exception messages.
        """
        redacted: list[str] = []
        skip_next = False
        for token in argv:
            if skip_next:
                redacted.append("[REDACTED]")
                skip_next = False
                continue
            redacted.append(token)
            if token == "--otp":
                skip_next = True
        return tuple(redacted)

    def _environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        environment.update({key: value for key, value in self.env.items() if key in _SAFE_ENV_KEYS})
        return environment

    @staticmethod
    async def _read_limited(stream: asyncio.StreamReader) -> str:
        collected = bytearray()
        while chunk := await stream.read(64 * 1024):
            collected.extend(chunk)
            if len(collected) > _MAX_OUTPUT_BYTES:
                raise CircleCliOutputError("Circle CLI output exceeded the safe diagnostic limit")
        return collected.decode("utf-8", errors="replace")

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=_TERMINATE_GRACE_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        operation: Operation,
        parse_json: bool,
    ) -> CircleCliResult:
        argv = self._validate_args(args)
        _redacted_argv = self._redact_argv(argv)
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *argv,
                cwd=str(self.cwd) if self.cwd else None,
                env=self._environment(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CircleCliNotInstalledError(
                "Circle CLI is not installed or the configured executable is unavailable"
            ) from exc
        except OSError as exc:
            raise CircleCliNotInstalledError("Circle CLI process could not be started") from exc

        stdout_task = asyncio.create_task(self._read_limited(process.stdout))
        stderr_task = asyncio.create_task(self._read_limited(process.stderr))
        exit_task = asyncio.create_task(process.wait())
        try:
            # Readers and process exit share one deadline. In particular, closed pipes
            # do not imply that the CLI (or a child it left behind) has exited.
            stdout, stderr, _ = await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task, exit_task), timeout=timeout_seconds
            )
        except asyncio.TimeoutError as exc:
            await self._stop(process)
            stdout_task.cancel()
            stderr_task.cancel()
            exit_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, exit_task, return_exceptions=True)
            error_type = "payment" if operation == "payment" else "read"
            raise CircleCliTimeoutError(f"Circle CLI {error_type} operation timed out") from exc
        except CircleCliOutputError:
            await self._stop(process)
            stdout_task.cancel()
            stderr_task.cancel()
            exit_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, exit_task, return_exceptions=True)
            raise

        parsed: dict | list | None = None
        if parse_json:
            try:
                candidate = json.loads(stdout)
            except json.JSONDecodeError as exc:
                if process.returncode == 0:
                    raise CircleCliOutputError("Circle CLI returned malformed JSON output") from exc
            else:
                if not isinstance(candidate, (dict, list)):
                    raise CircleCliOutputError("Circle CLI JSON output must be an object or array")
                parsed = candidate
        return CircleCliResult(
            argv=_redacted_argv,
            exit_code=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            parsed=parsed,
        )

    async def run_json(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        operation: Operation,
    ) -> CircleCliResult:
        """Run a documented JSON-capable command. The client owns argument construction."""
        return await self._run(
            args, timeout_seconds=timeout_seconds, operation=operation, parse_json=True
        )

    async def run_text(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        operation: Literal["read", "auth"],
    ) -> CircleCliResult:
        """Run the documented ``circle --version`` text command only."""
        return await self._run(
            args, timeout_seconds=timeout_seconds, operation=operation, parse_json=False
        )
