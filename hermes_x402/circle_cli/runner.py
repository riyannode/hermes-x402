"""Asynchronous, allowlisted Circle CLI process runner."""

from __future__ import annotations

import asyncio
import heapq
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Literal

from hermes_x402.circle_cli.errors import (
    CircleCliExecutableNotFoundError,
    CircleCliInvalidJsonOutputError,
    CircleCliNotInstalledError,
    CircleCliOutputError,
    CircleCliTimeoutAfterPaymentLogError,
    CircleCliTimeoutBeforePaymentLogError,
    CircleCliTimeoutError,
    CircleCliUnsupportedCapabilityError,
)
from hermes_x402.circle_cli.models import CircleCliDiagnostics, CircleCliResult, Operation

_MAX_OUTPUT_BYTES = 256 * 1024
_TERMINATE_GRACE_SECONDS = 3
_SAFE_ENV_KEYS = ("HOME", "PATH", "LANG", "LC_ALL", "LC_CTYPE")
_DIAGNOSTIC_TAIL_CHARS = 4000
# Keep payment-log diagnostics bounded even for long-lived wallets.
_MAX_PAYMENT_LOG_SNAPSHOT = 200
_PAYMENT_TIMEOUT_GRACE_SECONDS = 30
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "payment-signature",
        "x-payment",
    }
)
_SECRET_PATTERNS = (
    re.compile(
        r"(?i)((?:authorization|proxy-authorization|cookie|set-cookie|x-api-key|payment-signature|x-payment)\s*[:=]\s*)([^\r\n]+)"
    ),
    re.compile(r"(?i)(payment-signature\s*[:=]\s*)([^\s,'\"}]+)"),
    re.compile(r"(?i)(x-payment\s*[:=]\s*)([^\s,'\"}]+)"),
    re.compile(r'(?i)("paymentHeader"\s*:\s*")([^"]+)(")'),
    re.compile(r'(?i)("signature"\s*:\s*")0x[a-f0-9]{120,}(")'),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
)


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
        cli_payment_timeout_seconds: float | None = None,
        outer_payment_timeout_seconds: float | None = None,
        payment_timeout_grace_seconds: float = _PAYMENT_TIMEOUT_GRACE_SECONDS,
    ):
        self.executable = executable
        self.cwd = cwd
        self.env = dict(env or {})
        self.read_timeout_seconds = read_timeout_seconds
        cli_timeout = (
            payment_timeout_seconds
            if cli_payment_timeout_seconds is None
            else cli_payment_timeout_seconds
        )
        outer_timeout = (
            cli_timeout + payment_timeout_grace_seconds
            if outer_payment_timeout_seconds is None
            else outer_payment_timeout_seconds
        )
        if cli_timeout <= 0:
            raise ValueError("Circle CLI payment timeout must be positive")
        if payment_timeout_grace_seconds < 0:
            raise ValueError("Circle CLI payment timeout grace must be non-negative")
        if outer_timeout <= cli_timeout:
            raise ValueError(
                "Outer Circle CLI payment deadline must be greater than the CLI --timeout"
            )
        self.cli_payment_timeout_seconds = cli_timeout
        self.payment_timeout_seconds = outer_timeout
        self.payment_timeout_grace_seconds = payment_timeout_grace_seconds
        self.last_diagnostics: CircleCliDiagnostics | None = None

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
    def _redact_header_value(value: str) -> str:
        header = value.strip()
        if ":" in header:
            name, _raw_value = header.split(":", 1)
        elif "=" in header:
            name, _raw_value = header.split("=", 1)
        else:
            return "[REDACTED]"
        safe_name = CircleCliRunner._sanitize_text(name.strip())
        if not safe_name:
            return "[REDACTED]"
        # Header argv diagnostics should never retain values. Keeping only the
        # name is enough to debug CLI shape without leaking auth/payment proofs.
        return f"{safe_name}: [REDACTED]"

    @staticmethod
    def _redact_argv(argv: tuple[str, ...]) -> tuple[str, ...]:
        """Return a copy of *argv* with sensitive values replaced by [REDACTED]."""
        redacted: list[str] = []
        redact_next_for = {"--otp"}
        header_flags = {"-H", "--header"}
        skip_next_sensitive = False
        skip_next_header = False
        for token in argv:
            if skip_next_sensitive:
                redacted.append("[REDACTED]")
                skip_next_sensitive = False
                continue
            if skip_next_header:
                redacted.append(CircleCliRunner._redact_header_value(token))
                skip_next_header = False
                continue

            if token in header_flags:
                redacted.append(token)
                skip_next_header = True
                continue
            if token.startswith("-H="):
                redacted.append("-H=" + CircleCliRunner._redact_header_value(token[3:]))
                continue
            if token.startswith("--header="):
                redacted.append("--header=" + CircleCliRunner._redact_header_value(token[9:]))
                continue

            redacted.append(CircleCliRunner._sanitize_text(token))
            if token in redact_next_for:
                skip_next_sensitive = True
        return tuple(redacted)

    @staticmethod
    def _sanitize_text(text: str) -> str:
        value = text
        for pattern in _SECRET_PATTERNS:
            if pattern.groups >= 3:
                value = pattern.sub(r"\1[REDACTED]\3", value)
            elif pattern.groups >= 2:
                value = pattern.sub(r"\1[REDACTED]", value)
            else:
                value = pattern.sub("[REDACTED]", value)
        return value

    @classmethod
    def _safe_tail(cls, text: str) -> str:
        return cls._sanitize_text(text[-_DIAGNOSTIC_TAIL_CHARS:])

    def _environment(self) -> dict[str, str]:
        environment = {key: os.environ[key] for key in _SAFE_ENV_KEYS if key in os.environ}
        environment.update({key: value for key, value in self.env.items() if key in _SAFE_ENV_KEYS})
        return environment

    @staticmethod
    def _decode_buffer(collected: bytearray) -> str:
        return bytes(collected).decode("utf-8", errors="replace")

    @staticmethod
    async def _read_limited(
        stream: asyncio.StreamReader, collected: bytearray | None = None
    ) -> str:
        if collected is None:
            collected = bytearray()
        while chunk := await stream.read(64 * 1024):
            collected.extend(chunk)
            if len(collected) > _MAX_OUTPUT_BYTES:
                del collected[:-_MAX_OUTPUT_BYTES]
                raise CircleCliOutputError("Circle CLI output exceeded the safe diagnostic limit")
        return CircleCliRunner._decode_buffer(collected)

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

    @staticmethod
    def _payment_log_dir(env: Mapping[str, str]) -> Path:
        home = env.get("HOME") or str(Path.home())
        return Path(home) / ".circle-cli" / "payments"

    @classmethod
    def _payment_logs(cls, env: Mapping[str, str], operation: Operation) -> tuple[str, ...]:
        """Return a bounded deterministic snapshot of newest payment log names.

        The Circle CLI payment directory can contain thousands of historical
        entries. Diagnostics only need enough recent names to detect whether
        this process created a new log, so retain at most
        ``_MAX_PAYMENT_LOG_SNAPSHOT`` newest ``payment-*.json`` filenames by
        ``(mtime_ns, name)`` without reading file contents. Filesystem errors
        fail closed to an empty snapshot so diagnostics never block payment
        error handling.
        """
        if operation != "payment":
            return ()
        log_dir = cls._payment_log_dir(env)
        try:
            with os.scandir(log_dir) as entries:
                newest = heapq.nlargest(
                    _MAX_PAYMENT_LOG_SNAPSHOT,
                    (
                        (entry.stat().st_mtime_ns, entry.name)
                        for entry in entries
                        if entry.is_file()
                        and entry.name.startswith("payment-")
                        and entry.name.endswith(".json")
                    ),
                )
        except OSError:
            return ()
        return tuple(name for _mtime, name in sorted(newest))

    def _diagnostics(
        self,
        *,
        stage: str,
        start: float,
        argv: tuple[str, ...],
        env: Mapping[str, str],
        operation: Operation,
        pid: int | None = None,
        return_code: int | None = None,
        timed_out: bool = False,
        stdout: str = "",
        stderr: str = "",
        json_parse_ok: bool | None = None,
        json_parse_error: str | None = None,
        logs_before: tuple[str, ...] = (),
    ) -> CircleCliDiagnostics:
        logs_after = self._payment_logs(env, operation)
        new_logs = tuple(name for name in logs_after if name not in set(logs_before))
        signal = -return_code if isinstance(return_code, int) and return_code < 0 else None
        diag = CircleCliDiagnostics(
            stage=stage,
            monotonic_start=start,
            elapsed_seconds=time.monotonic() - start,
            executable=self.executable,
            cwd=str(self.cwd) if self.cwd else None,
            sanitized_argv=self._redact_argv(argv),
            pid=pid,
            return_code=return_code,
            termination_signal=signal,
            timed_out=timed_out,
            stdout_tail=self._safe_tail(stdout),
            stderr_tail=self._safe_tail(stderr),
            json_parse_ok=json_parse_ok,
            json_parse_error=json_parse_error,
            payment_log_dir=(str(self._payment_log_dir(env)) if operation == "payment" else None),
            payment_logs_before=logs_before,
            payment_logs_after=logs_after,
            new_payment_logs=new_logs,
        )
        self.last_diagnostics = diag
        return diag

    async def _run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: float,
        operation: Operation,
        parse_json: bool,
    ) -> CircleCliResult:
        argv = self._validate_args(args)
        env = self._environment()
        logs_before = self._payment_logs(env, operation)
        start = time.monotonic()
        self.last_diagnostics = self._diagnostics(
            stage="cli_spawn_start",
            start=start,
            argv=argv,
            env=env,
            operation=operation,
            logs_before=logs_before,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                self.executable,
                *argv,
                cwd=str(self.cwd) if self.cwd else None,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            self.last_diagnostics = self._diagnostics(
                stage="cli_not_spawned",
                start=start,
                argv=argv,
                env=env,
                operation=operation,
                logs_before=logs_before,
            )
            raise CircleCliExecutableNotFoundError(
                "Circle CLI executable was not found; command was not spawned"
            ) from exc
        except OSError as exc:
            self.last_diagnostics = self._diagnostics(
                stage="cli_not_spawned",
                start=start,
                argv=argv,
                env=env,
                operation=operation,
                logs_before=logs_before,
            )
            raise CircleCliNotInstalledError("Circle CLI process could not be started") from exc

        self.last_diagnostics = self._diagnostics(
            stage="cli_spawned",
            start=start,
            argv=argv,
            env=env,
            operation=operation,
            pid=getattr(process, "pid", None),
            logs_before=logs_before,
        )
        stdout_buffer = bytearray()
        stderr_buffer = bytearray()
        stdout_task = asyncio.create_task(self._read_limited(process.stdout, stdout_buffer))
        stderr_task = asyncio.create_task(self._read_limited(process.stderr, stderr_buffer))
        exit_task = asyncio.create_task(process.wait())
        try:
            # Readers and process exit share one deadline. Use asyncio.wait()
            # instead of wait_for(gather(...)) so a timeout does not cancel the
            # pipe readers before their bounded buffers can be used in diagnostics.
            _done, pending = await asyncio.wait(
                {stdout_task, stderr_task, exit_task},
                timeout=timeout_seconds,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for completed in _done:
                completed.result()
            if pending:
                raise asyncio.TimeoutError
            stdout = stdout_task.result()
            stderr = stderr_task.result()
            exit_task.result()
        except asyncio.TimeoutError as exc:
            await self._stop(process)
            try:
                await asyncio.wait_for(
                    asyncio.gather(stdout_task, stderr_task, return_exceptions=True),
                    timeout=_TERMINATE_GRACE_SECONDS,
                )
            except asyncio.TimeoutError:
                stdout_task.cancel()
                stderr_task.cancel()
                await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            exit_task.cancel()
            await asyncio.gather(exit_task, return_exceptions=True)
            stdout = self._decode_buffer(stdout_buffer)
            stderr = self._decode_buffer(stderr_buffer)
            diag = self._diagnostics(
                stage="cli_timed_out",
                start=start,
                argv=argv,
                env=env,
                operation=operation,
                pid=getattr(process, "pid", None),
                return_code=process.returncode,
                timed_out=True,
                stdout=stdout,
                stderr=stderr,
                logs_before=logs_before,
            )
            if operation == "payment" and diag.new_payment_logs:
                raise CircleCliTimeoutAfterPaymentLogError(
                    "Circle CLI payment timed out after a payment log was created"
                ) from exc
            if operation == "payment":
                raise CircleCliTimeoutBeforePaymentLogError(
                    "Circle CLI payment timed out before any payment log was created"
                ) from exc
            raise CircleCliTimeoutError("Circle CLI read operation timed out") from exc
        except CircleCliOutputError:
            await self._stop(process)
            stdout_task.cancel()
            stderr_task.cancel()
            exit_task.cancel()
            await asyncio.gather(stdout_task, stderr_task, exit_task, return_exceptions=True)
            self._diagnostics(
                stage="cli_output_error",
                start=start,
                argv=argv,
                env=env,
                operation=operation,
                pid=getattr(process, "pid", None),
                return_code=process.returncode,
                logs_before=logs_before,
            )
            raise

        parsed: dict | list | None = None
        json_parse_ok: bool | None = None
        json_parse_error: str | None = None
        if parse_json:
            try:
                candidate = json.loads(stdout)
            except json.JSONDecodeError as exc:
                json_parse_ok = False
                json_parse_error = f"{exc.__class__.__name__}: {exc.msg}"
                self._diagnostics(
                    stage="cli_completed",
                    start=start,
                    argv=argv,
                    env=env,
                    operation=operation,
                    pid=getattr(process, "pid", None),
                    return_code=process.returncode,
                    stdout=stdout,
                    stderr=stderr,
                    json_parse_ok=json_parse_ok,
                    json_parse_error=json_parse_error,
                    logs_before=logs_before,
                )
                if process.returncode == 0:
                    raise CircleCliInvalidJsonOutputError(
                        "Circle CLI returned malformed JSON output"
                    ) from exc
            else:
                json_parse_ok = True
                if not isinstance(candidate, (dict, list)):
                    raise CircleCliInvalidJsonOutputError(
                        "Circle CLI JSON output must be an object or array"
                    )
                parsed = candidate

        diag = self._diagnostics(
            stage="cli_completed",
            start=start,
            argv=argv,
            env=env,
            operation=operation,
            pid=getattr(process, "pid", None),
            return_code=process.returncode,
            stdout=stdout,
            stderr=stderr,
            json_parse_ok=json_parse_ok,
            json_parse_error=json_parse_error,
            logs_before=logs_before,
        )
        return CircleCliResult(
            argv=self._redact_argv(argv),
            exit_code=process.returncode or 0,
            stdout=stdout,
            stderr=stderr,
            parsed=parsed,
            diagnostics=diag,
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
