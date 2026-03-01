"""executor.py — Async subprocess runner with timeouts and retries."""

import asyncio
import time
from dataclasses import dataclass


@dataclass
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_code: int = -1
    duration: float = 0.0
    timed_out: bool = False
    command: str = ""


async def run_command(
    cmd: list[str],
    timeout: int = 90,
    sudo: bool = False,
    retries: int = 0,
    cwd: str | None = None,
) -> CommandResult:
    if sudo and cmd and cmd[0] != "sudo":
        cmd = ["sudo"] + cmd

    cmd_str = " ".join(cmd)

    for attempt in range(retries + 1):
        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout
                )
                elapsed = time.monotonic() - start
                return CommandResult(
                    stdout=stdout_bytes.decode("utf-8", errors="replace"),
                    stderr=stderr_bytes.decode("utf-8", errors="replace"),
                    exit_code=proc.returncode or 0,
                    duration=elapsed,
                    timed_out=False,
                    command=cmd_str,
                )
            except asyncio.TimeoutError:
                elapsed = time.monotonic() - start
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                if attempt < retries:
                    continue
                return CommandResult(
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    exit_code=-1,
                    duration=elapsed,
                    timed_out=True,
                    command=cmd_str,
                )
        except FileNotFoundError:
            elapsed = time.monotonic() - start
            return CommandResult(
                stderr=f"Command not found: {cmd[0]}",
                exit_code=127,
                duration=elapsed,
                command=cmd_str,
            )
        except Exception as e:
            elapsed = time.monotonic() - start
            if attempt < retries:
                continue
            return CommandResult(
                stderr=f"Error: {type(e).__name__}: {e}",
                exit_code=-1,
                duration=elapsed,
                command=cmd_str,
            )

    # Should not reach here, but safety return
    return CommandResult(stderr="All retries exhausted", exit_code=-1, command=cmd_str)


async def run_shell(
    command: str,
    timeout: int = 90,
    sudo: bool = False,
    cwd: str | None = None,
) -> CommandResult:
    if sudo and not command.strip().startswith("sudo "):
        command = f"sudo {command}"

    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            elapsed = time.monotonic() - start
            return CommandResult(
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                exit_code=proc.returncode or 0,
                duration=elapsed,
                timed_out=False,
                command=command,
            )
        except asyncio.TimeoutError:
            elapsed = time.monotonic() - start
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            return CommandResult(
                stderr=f"Command timed out after {timeout}s",
                exit_code=-1,
                duration=elapsed,
                timed_out=True,
                command=command,
            )
    except Exception as e:
        elapsed = time.monotonic() - start
        return CommandResult(
            stderr=f"Error: {type(e).__name__}: {e}",
            exit_code=-1,
            duration=elapsed,
            command=command,
        )
