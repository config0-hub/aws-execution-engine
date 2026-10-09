"""Subprocess command runner — execute a list of shell commands sequentially."""

from __future__ import annotations

import os
import signal
import subprocess
import time

from aws_exe_sys.common.result_writer import StepResult


class ExecutionTimedOut(Exception):
    """The execution deadline passed while commands were still running.

    ``steps`` holds every step attempted up to and including the one that
    was killed, so the caller can still report partial progress.
    """

    def __init__(self, message: str, steps: list[StepResult]):
        super().__init__(message)
        self.steps = steps


def run_commands(
    commands: list[str],
    env: dict[str, str] | None = None,
    work_dir: str | None = None,
    *,
    deadline: float,
) -> list[StepResult]:
    """Execute *commands* sequentially, stopping on first non-zero exit.

    Each command is run via ``subprocess.Popen`` with ``shell=True`` and
    ``stderr=subprocess.STDOUT`` so that stderr is merged into stdout. Every
    command starts its own session (process group) so a deadline kill reaches
    the shell AND its children.

    Args:
        commands: Shell command strings to execute in order.
        env:      Environment dict passed to each subprocess.  ``None``
                  inherits the current process environment.
        work_dir: Working directory for subprocesses.  ``None`` inherits
                  the current working directory.
        deadline: ``time.monotonic()`` value by which the WHOLE run must be
                  done. It is one deadline for the sequence, not a fresh
                  allowance per command.

    Returns:
        A list of :class:`StepResult` — one per command attempted.
        If a command fails (non-zero exit), execution stops and
        remaining commands are not attempted.

    Raises:
        ExecutionTimedOut: the deadline passed before a command could start,
            or while one was running. The running command's process group is
            killed first; the exception carries the steps so far, the killed
            one recorded as ``failed``.
    """
    results: list[StepResult] = []

    for idx, cmd in enumerate(commands):
        step_name = f"step-{idx}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ExecutionTimedOut(f"deadline passed before {step_name} started", results)

        start = time.monotonic()
        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            env=env,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout_bytes, _ = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            # SIGKILL the whole process group: the shell AND its children.
            os.killpg(proc.pid, signal.SIGKILL)
            stdout_bytes, _ = proc.communicate()
        elapsed = time.monotonic() - start

        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        exit_code = proc.returncode
        status = "succeeded" if exit_code == 0 and not timed_out else "failed"

        results.append(
            StepResult(
                step_name=step_name,
                status=status,
                exit_code=exit_code,
                duration_seconds=round(elapsed, 4),
                output=output,
            )
        )

        if timed_out:
            raise ExecutionTimedOut(f"deadline passed while {step_name} was running; killed", results)

        if exit_code != 0:
            break

    return results
