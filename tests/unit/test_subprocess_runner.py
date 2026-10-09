"""Unit tests for aws_exe_sys/common/subprocess_runner.py."""

import os
from pathlib import Path
import tempfile
import time

from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
import pytest

from aws_exe_sys.common.subprocess_runner import (
    ExecutionTimedOut,
    boto_config_until,
    run_commands,
    until_deadline,
)


def _far() -> float:
    """A deadline no test here reaches."""
    return time.monotonic() + 60


def _process_is_gone(pid: int) -> bool:
    """True when ``pid`` no longer runs: no /proc entry, or a zombie waiting to be reaped.

    The killed shell's children are reparented to PID 1; under pytest-as-PID-1
    (the Dockerfile.test harness) nobody reaps them, so a zombie counts as gone.
    """
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return True
    return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"


class TestRunCommandsSuccess:
    def test_single_echo(self):
        results = run_commands(["echo hello"], deadline=_far())
        assert len(results) == 1
        r = results[0]
        assert r.step_name == "step-0"
        assert r.status == "succeeded"
        assert r.exit_code == 0
        assert "hello" in r.output
        assert r.duration_seconds >= 0

    def test_multiple_commands(self):
        results = run_commands(["echo first", "echo second", "echo third"], deadline=_far())
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.step_name == f"step-{i}"
            assert r.status == "succeeded"
            assert r.exit_code == 0

    def test_true_command(self):
        results = run_commands(["true"], deadline=_far())
        assert len(results) == 1
        assert results[0].exit_code == 0
        assert results[0].status == "succeeded"


class TestRunCommandsFailure:
    def test_exit_nonzero(self):
        results = run_commands(["exit 1"], deadline=_far())
        assert len(results) == 1
        assert results[0].status == "failed"
        assert results[0].exit_code == 1

    def test_exit_code_42(self):
        results = run_commands(["exit 42"], deadline=_far())
        assert len(results) == 1
        assert results[0].exit_code == 42

    def test_stop_on_first_failure(self):
        results = run_commands(["echo ok", "exit 1", "echo never"], deadline=_far())
        assert len(results) == 2
        assert results[0].status == "succeeded"
        assert results[1].status == "failed"
        # Third command was never attempted


class TestRunCommandsStderrMerge:
    def test_stderr_in_output(self):
        results = run_commands(["echo stdout_text && echo stderr_text >&2"], deadline=_far())
        assert len(results) == 1
        assert results[0].status == "succeeded"
        assert "stdout_text" in results[0].output
        assert "stderr_text" in results[0].output


class TestRunCommandsWorkDir:
    def test_custom_work_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = run_commands(["pwd"], work_dir=tmpdir, deadline=_far())
            assert len(results) == 1
            assert results[0].status == "succeeded"
            # pwd output should contain the tmpdir path
            assert tmpdir in results[0].output.strip()


class TestRunCommandsEnv:
    def test_custom_env(self):
        env = os.environ.copy()
        env["MY_TEST_VAR"] = "test_value_12345"
        results = run_commands(["echo $MY_TEST_VAR"], env=env, deadline=_far())
        assert len(results) == 1
        assert "test_value_12345" in results[0].output


class TestRunCommandsStepCapture:
    def test_per_step_duration(self):
        results = run_commands(["echo fast"], deadline=_far())
        assert len(results) == 1
        assert isinstance(results[0].duration_seconds, float)
        assert results[0].duration_seconds >= 0

    def test_output_is_plain_text(self):
        results = run_commands(["echo 'plain text output'"], deadline=_far())
        assert len(results) == 1
        assert "plain text output" in results[0].output
        # Not base64 encoded
        assert results[0].output.strip() == "plain text output"


class TestRunCommandsDeadline:
    """One deadline for the whole sequence; on expiry the process GROUP is killed."""

    def test_command_longer_than_deadline_is_killed_with_its_child(self, tmp_path):
        pidfile = tmp_path / "child.pid"
        cmd = f"sleep 30 & echo $! > {pidfile}; echo started; wait"
        started = time.monotonic()

        with pytest.raises(ExecutionTimedOut) as excinfo:
            run_commands(["echo before", cmd, "echo never"], deadline=time.monotonic() + 1)

        assert time.monotonic() - started < 10
        steps = excinfo.value.steps
        assert [s.step_name for s in steps] == ["step-0", "step-1"]
        assert steps[0].status == "succeeded"
        assert steps[1].status == "failed"
        assert steps[1].exit_code != 0
        assert "started" in steps[1].output
        child_pid = int(pidfile.read_text().strip())
        assert _process_is_gone(child_pid), f"child {child_pid} survived the deadline kill"

    def test_deadline_is_shared_not_per_command(self):
        """Two half-second commands against a 0.7s deadline: the second is killed."""
        with pytest.raises(ExecutionTimedOut) as excinfo:
            run_commands(["sleep 0.5", "sleep 0.5", "echo never"], deadline=time.monotonic() + 0.7)

        steps = excinfo.value.steps
        assert len(steps) == 2
        assert steps[0].status == "succeeded"
        assert steps[1].status == "failed"

    def test_deadline_already_passed_runs_nothing(self):
        with pytest.raises(ExecutionTimedOut) as excinfo:
            run_commands(["echo never"], deadline=time.monotonic() - 1)
        assert excinfo.value.steps == []


class TestUntilDeadline:
    """An AWS call under the deadline runs in the calling thread; its botocore
    timeout (the time left, retries off) is the deadline, every other error passes."""

    def test_botocore_read_timeout_is_the_deadline(self):
        with pytest.raises(ExecutionTimedOut, match="while fetching: Read timeout"), until_deadline("fetching"):
            raise ReadTimeoutError(endpoint_url="http://127.0.0.1:1/x")

    def test_botocore_connect_timeout_is_the_deadline(self):
        with pytest.raises(ExecutionTimedOut, match="while fetching: Connect timeout"), until_deadline("fetching"):
            raise ConnectTimeoutError(endpoint_url="http://127.0.0.1:1/x")

    def test_other_errors_pass_unchanged(self):
        with pytest.raises(KeyError, match="boom"), until_deadline("looking up"):
            {}["boom"]

    def test_boto_config_carries_the_remaining_time_with_retries_off(self):
        config = boto_config_until(time.monotonic() + 10)
        assert 9 < config.read_timeout <= 10
        assert 9 < config.connect_timeout <= 10
        assert config.retries == {"total_max_attempts": 1}

    def test_boto_config_refuses_a_passed_deadline(self):
        with pytest.raises(ExecutionTimedOut):
            boto_config_until(time.monotonic() - 1)
