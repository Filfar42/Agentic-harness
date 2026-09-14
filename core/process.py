"""Bounded command capture for worker-thread execution.

Capture goes to temporary files, so a noisy subprocess cannot exhaust the
Python heap. Output and wall time are checked while the process is alive.
This is process management, not a substitute for the Docker security boundary.
"""

from __future__ import annotations

import os
import math
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from typing import IO

MAX_OUTPUT_BYTES = 4 * 1024 * 1024


class OutputLimitExceeded(RuntimeError):
    """A command exceeded the capture budget; effects may already have occurred."""


def _kill_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        if process.poll() is None:
            try:
                subprocess.run(
                    [os.path.join(os.environ.get("SYSTEMROOT", "C:\\Windows"), "System32", "taskkill.exe"),
                     "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def run_bounded(
    args: str | Sequence[str], *, timeout: float,
    cwd: str | None = None, env: Mapping[str, str] | None = None,
    shell: bool = False, max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> subprocess.CompletedProcess[str]:
    """Execute with finite time and output budgets; never buffer unlimited output.

On Windows /d disables cmd AutoRun and /s handles nested command quoting.
On timeout or excessive output the process tree is terminated where supported.
The caller must inspect state before repeating a command that may have had effects.
    """
    if not math.isfinite(timeout) or timeout <= 0 or type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError("Process budgets must be positive")
    if shell and not isinstance(args, str):
        raise ValueError("Shell commands must be strings")
    command = args
    if shell and os.name == "nt":
        # Pass cmd its native command line. list2cmdline escapes embedded
        # quotes with backslashes, which cmd does not interpret like the CRT.
        command = f'"{os.environ.get("COMSPEC", "cmd.exe")}" /d /s /c "{args}"'
        shell = False
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

    def size(stream: IO[bytes]) -> int:
        return os.fstat(stream.fileno()).st_size

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, shell=shell, cwd=cwd, env=env,
                                   stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                                   start_new_session=os.name != "nt", creationflags=flags)
        deadline = time.monotonic() + timeout
        try:
            while process.poll() is None:
                if size(stdout) + size(stderr) > max_output_bytes:
                    raise OutputLimitExceeded("Output del comando oltre il limite di 4 MiB; effetti da verificare.")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    process.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if size(stdout) + size(stderr) > max_output_bytes:
                raise OutputLimitExceeded("Output del comando oltre il limite; effetti da verificare.")
            stdout.seek(0)
            stderr.seek(0)
            return subprocess.CompletedProcess(args, process.returncode,
                stdout.read(max_output_bytes).decode("utf-8", "replace"),
                stderr.read(max_output_bytes).decode("utf-8", "replace"))
        finally:
            # POSIX group ids remain usable even when the shell has exited.
            if process.poll() is None or os.name != "nt":
                _kill_tree(process)
