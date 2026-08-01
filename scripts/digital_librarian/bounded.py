"""Resource-bounded execution for optional local analyzers."""

from __future__ import annotations

from dataclasses import dataclass
import os
import resource
import signal
import subprocess
import tempfile


@dataclass(frozen=True)
class BoundedProcessResult:
    returncode: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool = False
    output_limited: bool = False
    unavailable: bool = False


def _process_limits(maximum_bytes: int, maximum_memory_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (maximum_bytes, maximum_bytes))
    resource.setrlimit(
        resource.RLIMIT_AS, (maximum_memory_bytes, maximum_memory_bytes)
    )
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


def run_bounded(
    command: list[str],
    timeout_seconds: int | float,
    maximum_bytes: int,
    maximum_memory_bytes: int = 1_073_741_824,
) -> BoundedProcessResult:
    """Run a parser with bounded time, memory, and regular-file output."""
    environment = os.environ.copy()
    environment.update({"LC_ALL": "C", "LANG": "C"})
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                env=environment,
                start_new_session=True,
                preexec_fn=lambda: _process_limits(
                    maximum_bytes, maximum_memory_bytes
                ),
            )
        except OSError:
            return BoundedProcessResult(None, b"", b"", unavailable=True)
        timed_out = False
        try:
            returncode = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            returncode = process.wait()
        stdout.seek(0, os.SEEK_END)
        stdout_size = stdout.tell()
        stderr.seek(0, os.SEEK_END)
        stderr_size = stderr.tell()
        stdout.seek(0)
        stderr.seek(0)
        return BoundedProcessResult(
            returncode,
            stdout.read(maximum_bytes),
            stderr.read(maximum_bytes),
            timed_out=timed_out,
            output_limited=(
                stdout_size >= maximum_bytes
                or stderr_size >= maximum_bytes
                or returncode == -signal.SIGXFSZ
            ),
        )
