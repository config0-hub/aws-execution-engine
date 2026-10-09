"""Subprocess command runner, and the one execution deadline every blocking call shares."""

from __future__ import annotations

from collections.abc import Iterator
import contextlib
import os
import signal
import subprocess
import time

from botocore.config import Config
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError

from aws_exe_sys.common.result_writer import StepResult


class ExecutionTimedOut(Exception):
    """The execution deadline passed while commands were still running.

    ``steps`` holds every step attempted up to and including the one that
    was killed, so the caller can still report partial progress.
    """

    def __init__(self, message: str, steps: list[StepResult]):
        super().__init__(message)
        self.steps = steps


def boto_config_until(deadline: float) -> Config:
    """A botocore ``Config`` whose connect and read timeouts are the time left to ``deadline``.

    Retries are off (one attempt), so a stalled AWS call (headers sent, body
    withheld) surfaces once as a ``ReadTimeoutError`` or ``ConnectTimeoutError``
    at the deadline, in the calling thread, instead of being retried against
    the same timeout after T; :func:`until_deadline` maps it to ExecutionTimedOut.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExecutionTimedOut("deadline passed", [])
    return Config(connect_timeout=remaining, read_timeout=remaining, retries={"total_max_attempts": 1})


@contextlib.contextmanager
def until_deadline(what: str) -> Iterator[None]:
    """Make a botocore connect/read timeout inside the block the execution deadline.

    Every AWS call under the execution deadline runs inside this block, on a
    client built with :func:`boto_config_until`: the package GetObject and its
    body read, the SSM key GetParameter and DeleteParameter. The call runs in
    the calling thread, nothing is started that could outlive it, and a
    timeout raises ExecutionTimedOut so the result marker is written at T
    exactly as for a killed command. Every other error propagates unchanged.
    """
    try:
        yield
    except (ReadTimeoutError, ConnectTimeoutError) as exc:
        raise ExecutionTimedOut(f"deadline passed while {what}: {exc}", []) from exc


def run_until_deadline(
    args,
    *,
    deadline: float,
    **popen_kwargs,
) -> tuple[int, bytes | str, bytes | str | None, bool]:
    """Start ``args`` in its own session and wait for it, no later than ``deadline``.

    Every subprocess the engine runs under the execution deadline goes through
    here: the shell commands in :func:`run_commands` and ``sops --decrypt``.
    ``start_new_session=True`` puts the process in its own process group so the
    deadline kill (SIGKILL on the group) reaches the process AND its children.

    Returns ``(returncode, stdout, stderr, timed_out)``. ``stdout``/``stderr``
    are whatever ``popen_kwargs`` asked ``Popen`` to capture.
    """
    proc = subprocess.Popen(args, start_new_session=True, **popen_kwargs)
    try:
        stdout, stderr = proc.communicate(timeout=max(deadline - time.monotonic(), 0))
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        stdout, stderr = proc.communicate()
        return proc.returncode, stdout, stderr, True
    return proc.returncode, stdout, stderr, False


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
        if time.monotonic() >= deadline:
            raise ExecutionTimedOut(f"deadline passed before {step_name} started", results)

        start = time.monotonic()
        exit_code, stdout_bytes, _, timed_out = run_until_deadline(
            cmd,
            deadline=deadline,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=work_dir,
            env=env,
        )
        elapsed = time.monotonic() - start

        output = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
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
