"""Unit tests for aws_exe_sys/worker/run.py — simplified single-entrypoint worker."""

import base64
import json
import os
from pathlib import Path
from unittest.mock import patch
import zipfile

import pytest

from aws_exe_sys.common.result_writer import ExecutionResult, StepResult
from aws_exe_sys.common.sops import SopsKeyExpired
from aws_exe_sys.worker.run import cleanup_stale_workdirs, run


def _encode_commands(commands: list[str]) -> str:
    """Base64-encode a JSON array of commands."""
    return base64.b64encode(json.dumps(commands).encode()).decode()


DONE_ENDPOINT = "s3://test-bucket/results/trigger-1/done.json"


class TestScratchCleanup:
    """Worker scratch cleanup is isolated to worker-owned run directories."""

    @patch("aws_exe_sys.worker.run.tempfile.gettempdir")
    def test_cleanup_removes_only_owned_directories(
        self,
        mock_gettempdir,
        tmp_path,
    ):
        mock_gettempdir.return_value = str(tmp_path)
        scratch_root = tmp_path / "aws-exe-sys-worker"
        owned_first = scratch_root / "run-first"
        owned_second = scratch_root / "run-second"
        foreign_dir = scratch_root / "foreign-dir"
        matching_file = scratch_root / "run-foreign-file"
        outside_file = tmp_path / "foreign-file"

        owned_first.mkdir(parents=True)
        owned_second.mkdir()
        foreign_dir.mkdir()
        matching_file.write_text("not a worker directory")
        outside_file.write_text("outside the worker scratch root")
        (owned_first / "provider-cache").write_text("stale")

        cleanup_stale_workdirs()

        assert not owned_first.exists()
        assert not owned_second.exists()
        assert foreign_dir.is_dir()
        assert matching_file.read_text() == "not a worker directory"
        assert outside_file.read_text() == "outside the worker scratch root"

    @patch("aws_exe_sys.worker.run.shutil.rmtree")
    @patch("aws_exe_sys.worker.run.tempfile.gettempdir")
    def test_cleanup_failure_raises(
        self,
        mock_gettempdir,
        mock_rmtree,
        tmp_path,
    ):
        mock_gettempdir.return_value = str(tmp_path)
        owned_dir = tmp_path / "aws-exe-sys-worker" / "run-stale"
        owned_dir.mkdir(parents=True)
        mock_rmtree.side_effect = OSError("cleanup denied")

        with pytest.raises(OSError, match="cleanup denied"):
            cleanup_stale_workdirs()

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("boto3.client")
    @patch("aws_exe_sys.worker.run.tempfile.gettempdir")
    def test_second_run_removes_first_run_workspace(
        self,
        mock_gettempdir,
        mock_boto_client,
        mock_run_commands,
        mock_write_result,
        tmp_path,
    ):
        mock_gettempdir.return_value = str(tmp_path)
        scratch_root = tmp_path / "aws-exe-sys-worker"
        scratch_root.mkdir()
        foreign_file = scratch_root / "foreign-file"
        foreign_file.write_text("keep")
        workdirs: list[Path] = []

        def download_zip(bucket: str, key: str, destination: str) -> None:
            assert bucket == "bucket"
            assert key == "exec.zip"
            with zipfile.ZipFile(destination, "w") as archive:
                archive.writestr("package.txt", "package")

        def execute_commands(
            commands: list[str],
            *,
            env: dict[str, str],
            work_dir: str,
        ) -> list[StepResult]:
            del commands, env
            current_workdir = Path(work_dir)
            if workdirs:
                assert not workdirs[0].exists()
                assert not (current_workdir / "first-run-only").exists()
            else:
                (current_workdir / "first-run-only").write_text("stale")
            workdirs.append(current_workdir)
            return [
                StepResult(
                    step_name="step-0",
                    status="succeeded",
                    exit_code=0,
                    duration_seconds=0.1,
                    output="ok",
                ),
            ]

        mock_boto_client.return_value.download_file.side_effect = download_zip
        mock_run_commands.side_effect = execute_commands

        for trigger_id in ("first", "second"):
            status = run(
                trigger_id=trigger_id,
                s3_package_uri="s3://bucket/exec.zip",
                sops_type=None,
                sops_path=None,
                commands_b64=_encode_commands(["echo ok"]),
                done_endpoint=DONE_ENDPOINT,
                execution_target="lambda",
            )
            assert status == "succeeded"

        assert len(workdirs) == 2
        assert not workdirs[0].exists()
        assert workdirs[1].is_dir()
        assert foreign_file.read_text() == "keep"
        assert mock_write_result.call_count == 2

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    @patch("aws_exe_sys.worker.run.cleanup_stale_workdirs")
    def test_run_reports_cleanup_failure(
        self,
        mock_cleanup,
        mock_fetch,
        mock_write,
    ):
        mock_cleanup.side_effect = OSError("cleanup denied")

        status = run(
            trigger_id="cleanup-failed",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        assert status == "failed"
        mock_fetch.assert_not_called()
        mock_write.assert_called_once()
        result_arg = mock_write.call_args.args[1]
        assert result_arg.status == "failed"
        assert result_arg.error == "cleanup denied"


class TestRunHappyPath:
    """Happy path: download succeeds, SOPS succeeds, commands succeed."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_full_pipeline_succeeded(
        self,
        mock_fetch,
        mock_sops,
        mock_run_cmds,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_sops.return_value = {"SECRET_KEY": "val"}
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
        ]

        status = run(
            trigger_id="t-1",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="ssm",
            sops_path="/sops/key/path",
            commands_b64=_encode_commands(["echo hello"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        assert status == "succeeded"
        mock_fetch.assert_called_once_with("s3://bucket/exec.zip")
        mock_sops.assert_called_once_with("/tmp/work", sops_type="ssm", sops_path="/sops/key/path")
        mock_run_cmds.assert_called_once()
        mock_write.assert_called_once()
        result_arg = mock_write.call_args[0][1]
        assert isinstance(result_arg, ExecutionResult)
        assert result_arg.trigger_id == "t-1"
        assert result_arg.status == "succeeded"
        assert len(result_arg.steps) == 1
        assert result_arg.error is None

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_no_sops_when_sops_type_is_none(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
        ]

        status = run(
            trigger_id="t-2",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo hi"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="codebuild",
        )

        assert status == "succeeded"
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "succeeded"

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_sops_env_vars_passed_to_commands(
        self,
        mock_fetch,
        mock_sops,
        mock_run_cmds,
        mock_write,
    ):
        """SOPS decrypted env vars should be merged into the subprocess env."""
        mock_fetch.return_value = "/tmp/work"
        mock_sops.return_value = {"MY_SECRET": "s3cr3t"}
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output=""),
        ]

        run(
            trigger_id="t-env",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="kms",
            sops_path=None,
            commands_b64=_encode_commands(["echo test"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        # Check that env dict passed to run_commands includes the SOPS var
        call_kwargs = mock_run_cmds.call_args
        env_passed = call_kwargs[1]["env"] if "env" in call_kwargs[1] else call_kwargs[0][1]
        assert env_passed["MY_SECRET"] == "s3cr3t"


class TestRunS3DownloadFail:
    """S3 download fail -> failed result written to done_endpoint."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_s3_download_failure_writes_failed_result(
        self,
        mock_fetch,
        mock_write,
    ):
        mock_fetch.side_effect = Exception("S3 download failed: NoSuchKey")

        status = run(
            trigger_id="t-s3fail",
            s3_package_uri="s3://bucket/missing.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        assert status == "failed"
        mock_write.assert_called_once()
        result_arg = mock_write.call_args[0][1]
        assert result_arg.trigger_id == "t-s3fail"
        assert result_arg.status == "failed"
        assert "S3 download failed" in result_arg.error
        assert result_arg.steps == []


class TestRunSopsFail:
    """SOPS fail -> failed result written to done_endpoint."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_sops_key_expired_writes_failed_result(
        self,
        mock_fetch,
        mock_sops,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_sops.side_effect = SopsKeyExpired("key /sops/key is missing or expired")

        status = run(
            trigger_id="t-sops",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="ssm",
            sops_path="/sops/key",
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        assert status == "failed"
        mock_write.assert_called_once()
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "failed"
        assert "sops_key_expired" in result_arg.error
        assert result_arg.steps == []

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_sops_generic_error_writes_failed_result(
        self,
        mock_fetch,
        mock_sops,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_sops.side_effect = RuntimeError("sops binary not found")

        status = run(
            trigger_id="t-sops-err",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="ssm",
            sops_path="/sops/key",
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        assert status == "failed"
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "failed"
        assert "sops binary not found" in result_arg.error


class TestRunCommandFail:
    """Command fail -> failed result with partial steps written."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_command_failure_has_partial_steps(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
            StepResult(step_name="step-1", status="failed", exit_code=1, duration_seconds=0.2, output="error"),
        ]

        status = run(
            trigger_id="t-cmdfail",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo ok", "exit 1", "echo unreachable"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="codebuild",
        )

        assert status == "failed"
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "failed"
        assert len(result_arg.steps) == 2
        assert result_arg.steps[0].status == "succeeded"
        assert result_arg.steps[1].status == "failed"
        assert result_arg.error is None  # Error is in steps, not top-level


class TestRunAlwaysWritesResult:
    """Key invariant: result is ALWAYS written even on failure."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_write_result_called_on_exception(
        self,
        mock_fetch,
        mock_write,
    ):
        mock_fetch.side_effect = Exception("boom")

        run(
            trigger_id="t-boom",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        mock_write.assert_called_once()
        assert mock_write.call_args[0][0] == DONE_ENDPOINT

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_write_result_called_on_success(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output=""),
        ]

        run(
            trigger_id="t-ok",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        mock_write.assert_called_once()

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_write_result_called_on_sops_expired(
        self,
        mock_fetch,
        mock_sops,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_sops.side_effect = SopsKeyExpired("gone")

        run(
            trigger_id="t-sops-gone",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="ssm",
            sops_path="/key",
            commands_b64=_encode_commands(["echo"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )

        mock_write.assert_called_once()

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_write_result_failure_is_raised(
        self,
        mock_fetch,
        mock_write,
    ):
        """A worker must not report completion when the done marker was not written."""
        mock_fetch.side_effect = Exception("download failed")
        mock_write.side_effect = OSError("S3 write failed")

        with pytest.raises(OSError, match="S3 write failed"):
            run(
                trigger_id="t-write-fail",
                s3_package_uri="s3://bucket/exec.zip",
                sops_type=None,
                sops_path=None,
                commands_b64=_encode_commands(["echo"]),
                done_endpoint=DONE_ENDPOINT,
                execution_target="lambda",
            )


class TestRunNoEnvironMutation:
    """Verify run() does not mutate os.environ."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.handle_sops")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_no_environ_mutation(
        self,
        mock_fetch,
        mock_sops,
        mock_run_cmds,
        mock_write,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_sops.return_value = {"INJECTED_VAR": "should_not_leak"}
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output=""),
        ]

        env_before = os.environ.copy()
        run(
            trigger_id="t-env",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="kms",
            sops_path=None,
            commands_b64=_encode_commands(["echo"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
        )
        env_after = os.environ.copy()

        assert "INJECTED_VAR" not in env_after
        assert env_before == env_after
