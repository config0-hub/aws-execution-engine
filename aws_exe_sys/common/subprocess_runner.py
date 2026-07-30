"""Subprocess command runner — execute a list of shell commands sequentially."""

from __future__ import annotations

import subprocess
import time

from aws_exe_sys.common.result_writer import StepResult


def run_commands(
    commands: list[str],
    env: dict[str, str] | None = None,
    work_dir: str | None = None,
) -> list[StepResult]:
    """Execute *commands* sequentially, stopping on first non-zero exit.

    Each command is run via ``subprocess.Popen`` with ``shell=True`` and
    ``stderr=subprocess.STDOUT`` so that stderr is merged into stdout.

    Args:
        commands: Shell command strings to execute in order.
        env:      Environment dict passed to each subprocess.  ``None``
                  inherits the current process environment.
        work_dir: Working directory for subprocesses.  ``None`` inherits
                  the current working directory.

    Returns:
        A list of :class:`StepResult` — one per command attempted.
        If a command fails (non-zero exit), execution stops and
        remaining commands are not attempted.
    """
    results: list[StepResult] = []

    for idx, cmd in enumerate(commands):
        step_name = f"step-{idx}"
        start = time.monotonic()

        proc = subprocess.Popen(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            env=env,
        )
        stdout_bytes, _ = proc.communicate()
        elapsed = time.monotonic() - start

        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        exit_code = proc.returncode
        status = "succeeded" if exit_code == 0 else "failed"

        results.append(
            StepResult(
                step_name=step_name,
                status=status,
                exit_code=exit_code,
                duration_seconds=round(elapsed, 4),
                output=output,
            )
        )

        if exit_code != 0:
            break

    return results
