"""Unit tests for aws_exe_sys/common/subprocess_runner.py."""

import os
import tempfile

from aws_exe_sys.common.subprocess_runner import run_commands


class TestRunCommandsSuccess:
    def test_single_echo(self):
        results = run_commands(["echo hello"])
        assert len(results) == 1
        r = results[0]
        assert r.step_name == "step-0"
        assert r.status == "succeeded"
        assert r.exit_code == 0
        assert "hello" in r.output
        assert r.duration_seconds >= 0

    def test_multiple_commands(self):
        results = run_commands(["echo first", "echo second", "echo third"])
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.step_name == f"step-{i}"
            assert r.status == "succeeded"
            assert r.exit_code == 0

    def test_true_command(self):
        results = run_commands(["true"])
        assert len(results) == 1
        assert results[0].exit_code == 0
        assert results[0].status == "succeeded"


class TestRunCommandsFailure:
    def test_exit_nonzero(self):
        results = run_commands(["exit 1"])
        assert len(results) == 1
        assert results[0].status == "failed"
        assert results[0].exit_code == 1

    def test_exit_code_42(self):
        results = run_commands(["exit 42"])
        assert len(results) == 1
        assert results[0].exit_code == 42

    def test_stop_on_first_failure(self):
        results = run_commands(["echo ok", "exit 1", "echo never"])
        assert len(results) == 2
        assert results[0].status == "succeeded"
        assert results[1].status == "failed"
        # Third command was never attempted


class TestRunCommandsStderrMerge:
    def test_stderr_in_output(self):
        results = run_commands(["echo stdout_text && echo stderr_text >&2"])
        assert len(results) == 1
        assert results[0].status == "succeeded"
        assert "stdout_text" in results[0].output
        assert "stderr_text" in results[0].output


class TestRunCommandsWorkDir:
    def test_custom_work_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            results = run_commands(["pwd"], work_dir=tmpdir)
            assert len(results) == 1
            assert results[0].status == "succeeded"
            # pwd output should contain the tmpdir path
            assert tmpdir in results[0].output.strip()


class TestRunCommandsEnv:
    def test_custom_env(self):
        env = os.environ.copy()
        env["MY_TEST_VAR"] = "test_value_12345"
        results = run_commands(["echo $MY_TEST_VAR"], env=env)
        assert len(results) == 1
        assert "test_value_12345" in results[0].output


class TestRunCommandsStepCapture:
    def test_per_step_duration(self):
        results = run_commands(["echo fast"])
        assert len(results) == 1
        assert isinstance(results[0].duration_seconds, float)
        assert results[0].duration_seconds >= 0

    def test_output_is_plain_text(self):
        results = run_commands(["echo 'plain text output'"])
        assert len(results) == 1
        assert "plain text output" in results[0].output
        # Not base64 encoded
        assert results[0].output.strip() == "plain text output"
