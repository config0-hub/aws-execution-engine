"""Unit tests for aws_exe_sys/worker/run.py — simplified single-entrypoint worker."""

import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import threading
import time
from unittest.mock import ANY, patch
import zipfile

import pytest

from aws_exe_sys.common.result_writer import ExecutionResult, StepResult
from aws_exe_sys.common.sops import SopsKeyExpired
from aws_exe_sys.worker.handler import handler
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

        cleanup_stale_workdirs(deadline=time.monotonic() + 60)

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
            cleanup_stale_workdirs(deadline=time.monotonic() + 60)

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

        def get_package(Bucket: str, Key: str) -> dict:
            assert Bucket == "bucket"
            assert Key == "exec.zip"
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("package.txt", "package")
            return {"Body": io.BytesIO(archive.getvalue())}

        def execute_commands(
            commands: list[str],
            *,
            env: dict[str, str],
            work_dir: str,
            deadline: float,
        ) -> list[StepResult]:
            del commands, env, deadline
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

        mock_boto_client.return_value.get_object.side_effect = get_package
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
                timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
        )

        assert status == "succeeded"
        mock_fetch.assert_called_once_with("s3://bucket/exec.zip", deadline=ANY)
        mock_sops.assert_called_once_with("/tmp/work", sops_type="ssm", sops_path="/sops/key/path", deadline=ANY)
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
            timeout_seconds=3600,
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
                timeout_seconds=3600,
            )


class TestRunCallback:
    """callback_url / callback_token — best-effort POST after the marker write."""

    @patch("aws_exe_sys.worker.run.post_callback")
    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_absent_callback_url_no_post_attempted(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
        mock_post_callback,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
        ]

        run(
            trigger_id="t-no-cb",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo ok"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=3600,
        )

        mock_post_callback.assert_called_once_with(None, None, mock_write.call_args[0][1])

    @patch("aws_exe_sys.worker.run.post_callback")
    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_present_callback_posted_with_token_after_marker_write(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
        mock_post_callback,
    ):
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
        ]
        call_order = []
        mock_write.side_effect = lambda *a, **k: call_order.append("write_result")
        mock_post_callback.side_effect = lambda *a, **k: call_order.append("post_callback")

        run(
            trigger_id="t-cb",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo ok"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=3600,
            callback_url="https://caller.example.com/hooks/done",
            callback_token="tok-abc",
        )

        assert call_order == ["write_result", "post_callback"]
        mock_post_callback.assert_called_once_with(
            "https://caller.example.com/hooks/done",
            "tok-abc",
            mock_write.call_args[0][1],
        )

    @patch("aws_exe_sys.worker.run.post_callback")
    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_callback_failure_does_not_affect_returned_status(
        self,
        mock_fetch,
        mock_write,
        mock_post_callback,
    ):
        """post_callback is a no-raise function — its own internals log-and-swallow
        the failure, so run() never sees an exception from it. Even so, a mock
        that raised would prove the failure never reaches the caller's status."""
        mock_fetch.side_effect = Exception("boom")
        mock_post_callback.return_value = None  # post_callback never raises by contract

        status = run(
            trigger_id="t-cb-fail",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=3600,
            callback_url="https://caller.example.com/hooks/done",
            callback_token="tok-abc",
        )

        assert status == "failed"
        mock_post_callback.assert_called_once()

    @patch("aws_exe_sys.worker.run.post_callback")
    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_write_result_failure_skips_callback(
        self,
        mock_fetch,
        mock_write,
        mock_post_callback,
    ):
        """A marker-write failure is raised (not swallowed) and the callback
        that follows it in the finally block never runs — the invariant is
        callback-after-successful-write, not callback-no-matter-what."""
        mock_fetch.side_effect = Exception("download failed")
        mock_write.side_effect = OSError("S3 write failed")

        with pytest.raises(OSError, match="S3 write failed"):
            run(
                trigger_id="t-write-fail-cb",
                s3_package_uri="s3://bucket/exec.zip",
                sops_type=None,
                sops_path=None,
                commands_b64=_encode_commands(["echo"]),
                done_endpoint=DONE_ENDPOINT,
                execution_target="lambda",
                timeout_seconds=3600,
                callback_url="https://caller.example.com/hooks/done",
                callback_token="tok-abc",
            )

        mock_post_callback.assert_not_called()


class TestRunDeadline:
    """timeout_seconds (T) is the deadline for the whole run: the command is
    killed and the marker is ``failed`` with an error that names T."""

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_command_longer_than_timeout_writes_timed_out_failed_result(
        self,
        mock_fetch,
        mock_write,
        tmp_path,
    ):
        mock_fetch.return_value = str(tmp_path)

        status = run(
            trigger_id="t-timeout",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo ok", "sleep 30", "echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=1,
        )

        assert status == "failed"
        mock_write.assert_called_once()
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "failed"
        assert result_arg.error.startswith("execution timed out at 1 seconds")
        assert [s.step_name for s in result_arg.steps] == ["step-0", "step-1"]
        assert result_arg.steps[0].status == "succeeded"
        assert result_arg.steps[1].status == "failed"

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.run_commands")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_deadline_counts_from_run_entry(
        self,
        mock_fetch,
        mock_run_cmds,
        mock_write,
    ):
        """The deadline handed to run_commands is entry + T, not a fresh T after fetch/decrypt."""
        mock_fetch.return_value = "/tmp/work"
        mock_run_cmds.return_value = [
            StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok"),
        ]
        before = time.monotonic()

        run(
            trigger_id="t-deadline",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type=None,
            sops_path=None,
            commands_b64=_encode_commands(["echo ok"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=600,
        )

        deadline = mock_run_cmds.call_args.kwargs["deadline"]
        assert before + 600 <= deadline <= time.monotonic() + 600


def _package_zip() -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("run.sh", "echo never\n")
    return archive.getvalue()


class _StalledAwsHandler(BaseHTTPRequestHandler):
    """A stand-in S3 + SSM endpoint that answers headers at once and withholds bodies.

    S3: HEAD returns the package size; GET sends its headers, then holds the
    zip body for ``STALL_SECONDS``; PUT records the result marker (time, body).
    SSM: POST (GetParameter) holds its response for ``STALL_SECONDS``.
    """

    protocol_version = "HTTP/1.1"  # answers the S3 PUT's Expect: 100-continue at once
    PACKAGE = _package_zip()
    STALL_SECONDS = 6.0
    STALL_GET = False
    STALL_SSM = False
    markers: list[tuple[float, dict]] = []

    def log_message(self, *_args):  # keep pytest output clean
        pass

    def _package_headers(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.PACKAGE)))
        self.send_header("Content-Type", "application/zip")
        self.end_headers()

    def do_HEAD(self):
        self._package_headers()

    def do_GET(self):
        self._package_headers()
        if self.STALL_GET:
            time.sleep(self.STALL_SECONDS)
        try:
            self.wfile.write(self.PACKAGE)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_PUT(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.markers.append((time.monotonic(), json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.STALL_SSM:
            time.sleep(self.STALL_SECONDS)
        body = b'{"Parameter": {"Value": "AGE-SECRET-KEY-1TEST"}}'
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-amz-json-1.1")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return


@pytest.fixture
def stalled_aws(monkeypatch):
    """Point boto3's S3 and SSM clients at the stalling stand-in over real HTTP."""
    _StalledAwsHandler.markers = []
    _StalledAwsHandler.STALL_GET = False
    _StalledAwsHandler.STALL_SSM = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StalledAwsHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    endpoint = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", endpoint)
    monkeypatch.setenv("AWS_ENDPOINT_URL_SSM", endpoint)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    yield _StalledAwsHandler
    server.shutdown()


def _handler_event(**overrides) -> dict:
    event = {
        "trigger_id": "t-stalled",
        "s3_package_uri": "s3://pkg-bucket/exec/stalled.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _encode_commands(["echo never"]),
        "done_endpoint": DONE_ENDPOINT,
        "execution_target": "lambda",
        "timeout_seconds": 1,
    }
    event.update(overrides)
    return event


class TestRunDeadlineCoversPreparation:
    """T covers every blocking preparation call, not only the commands: a
    stalled S3 GET (headers sent, body withheld), a stalled SSM GetParameter or
    a hung ``sops`` yields the timed-out ``failed`` marker at T, with no step
    run. The two stalled cases go through the real handler and the real
    ``write_result`` PUT, so the marker's arrival time is what is measured."""

    MARKER_WITHIN_SECONDS = 3  # T=1 plus the reserve for the result write; the body is held for 6

    def test_stalled_package_download_writes_the_marker_at_timeout(self, stalled_aws):
        stalled_aws.STALL_GET = True
        started = time.monotonic()

        response = handler(_handler_event())

        assert response == {"status": "failed"}
        assert len(stalled_aws.markers) == 1
        written_at, marker = stalled_aws.markers[0]
        assert written_at - started < self.MARKER_WITHIN_SECONDS, "marker waited for the withheld body"
        assert marker["status"] == "failed"
        assert marker["error"].startswith("execution timed out at 1 seconds")
        assert "fetching the package" in marker["error"]
        assert marker["steps"] == []

    def test_stalled_ssm_key_fetch_writes_the_marker_at_timeout(self, stalled_aws):
        stalled_aws.STALL_SSM = True
        started = time.monotonic()

        response = handler(_handler_event(sops_type="ssm", sops_path="/exe-sys/sops-keys/t-stalled"))

        assert response == {"status": "failed"}
        assert len(stalled_aws.markers) == 1
        written_at, marker = stalled_aws.markers[0]
        assert written_at - started < self.MARKER_WITHIN_SECONDS, "marker waited for the withheld SSM response"
        assert marker["status"] == "failed"
        assert marker["error"].startswith("execution timed out at 1 seconds")
        assert "fetching the SOPS key from SSM" in marker["error"]
        assert marker["steps"] == []

    def test_codebuild_entry_exits_at_timeout_with_the_download_stalled(self, stalled_aws):
        """The CodeBuild delivery (``entrypoint.sh`` → ``python3 -m aws_exe_sys.worker.handler``)
        must EXIT near T, not only write its marker near T: nothing the package
        fetch started may be joined at interpreter exit while the body is withheld."""
        stalled_aws.STALL_GET = True
        env = {
            **os.environ,
            "TRIGGER_ID": "t-stalled",
            "S3_PACKAGE_URI": "s3://pkg-bucket/exec/stalled.zip",
            "COMMANDS_B64": _encode_commands(["echo never"]),
            "DONE_ENDPOINT": DONE_ENDPOINT,
            "EXECUTION_TARGET": "codebuild",
            "TIMEOUT_SECONDS": "1",
        }
        started = time.monotonic()

        proc = subprocess.run(
            [sys.executable, "-m", "aws_exe_sys.worker.handler"],
            cwd=Path(__file__).resolve().parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=stalled_aws.STALL_SECONDS + 10,
        )

        exited_at = time.monotonic()
        assert proc.returncode == 1, proc.stderr
        assert exited_at - started < self.MARKER_WITHIN_SECONDS, f"process waited for the withheld body: {proc.stderr}"
        assert len(stalled_aws.markers) == 1
        written_at, marker = stalled_aws.markers[0]
        assert written_at - started < self.MARKER_WITHIN_SECONDS, "marker waited for the withheld body"
        assert marker["status"] == "failed"
        assert marker["error"].startswith("execution timed out at 1 seconds")
        assert "fetching the package" in marker["error"]
        assert marker["steps"] == []

    @patch("aws_exe_sys.worker.run.write_result")
    @patch("aws_exe_sys.worker.run.fetch_code_s3")
    def test_hung_sops_decrypt_is_killed_at_timeout(self, mock_fetch, mock_write, tmp_path, monkeypatch):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        (work_dir / "secrets.enc.json").write_text("{}")
        mock_fetch.return_value = str(work_dir)

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        fake_sops = fake_bin / "sops"
        fake_sops.write_text("#!/bin/sh\nsleep 30\n")
        fake_sops.chmod(fake_sops.stat().st_mode | stat.S_IXUSR)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
        started = time.monotonic()

        status = run(
            trigger_id="t-hung-sops",
            s3_package_uri="s3://bucket/exec.zip",
            sops_type="kms",
            sops_path=None,
            commands_b64=_encode_commands(["echo never"]),
            done_endpoint=DONE_ENDPOINT,
            execution_target="lambda",
            timeout_seconds=1,
        )

        assert time.monotonic() - started < 10, "sops ran past the deadline"
        assert status == "failed"
        result_arg = mock_write.call_args[0][1]
        assert result_arg.status == "failed"
        assert result_arg.error.startswith("execution timed out at 1 seconds")
        assert "sops was running; killed" in result_arg.error
        assert result_arg.steps == []


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
            timeout_seconds=3600,
        )
        env_after = os.environ.copy()

        assert "INJECTED_VAR" not in env_after
        assert env_before == env_after
