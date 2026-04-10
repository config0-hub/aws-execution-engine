"""Unit tests for src/worker/run.py."""

import json
import os
import tempfile
import zipfile
from unittest.mock import patch, MagicMock, call

import pytest

from src.worker.run import (
    run,
    _execute_commands,
    _setup_events_dir,
    _collect_and_write_events,
)


class TestExecuteCommands:
    def test_successful_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(["echo hello"], tmpdir)
            assert status == "succeeded"
            assert "hello" in log

    def test_failed_command(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(["exit 1"], tmpdir)
            assert status == "failed"
            assert "Exit code: 1" in log

    def test_multiple_commands_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(
                ["echo first", "echo second"],
                tmpdir,
            )
            assert status == "succeeded"
            assert "first" in log
            assert "second" in log

    def test_stops_on_first_failure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(
                ["echo before", "exit 1", "echo after"],
                tmpdir,
            )
            assert status == "failed"
            assert "before" in log
            assert "after" not in log

    def test_timeout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(
                ["sleep 10"],
                tmpdir,
                timeout=1,
            )
            assert status == "timed_out"
            assert "timed out" in log.lower()

    def test_captures_stderr(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(
                ["echo error_msg >&2"],
                tmpdir,
            )
            # stderr is merged with stdout via STDOUT redirect
            assert "error_msg" in log

    def test_custom_env_passed_to_subprocess(self):
        """Verify that a custom env dict is used instead of os.environ."""
        custom_env = os.environ.copy()
        custom_env["TEST_WORKER_CUSTOM"] = "from_custom_env"
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(
                ["echo $TEST_WORKER_CUSTOM"],
                tmpdir,
                env=custom_env,
            )
            assert status == "succeeded"
            assert "from_custom_env" in log

    def test_default_env_when_none(self):
        """When env=None, falls back to os.environ.copy()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            status, log = _execute_commands(["echo hello"], tmpdir, env=None)
            assert status == "succeeded"
            assert "hello" in log


class TestSetupEventsDir:
    def test_creates_directory(self):
        trace_id = "test-trace-123"
        events_dir = _setup_events_dir(trace_id)
        assert os.path.isdir(events_dir)
        assert events_dir == f"/tmp/share/{trace_id}/events"

    def test_does_not_set_env_var(self):
        """_setup_events_dir no longer mutates os.environ."""
        with patch.dict(os.environ, {}, clear=False):
            # Remove it if it exists
            os.environ.pop("AWS_EXE_SYS_EVENTS_DIR", None)
            trace_id = "test-trace-no-env"
            _setup_events_dir(trace_id)
            assert "AWS_EXE_SYS_EVENTS_DIR" not in os.environ

    def test_idempotent(self):
        trace_id = "test-trace-789"
        dir1 = _setup_events_dir(trace_id)
        dir2 = _setup_events_dir(trace_id)
        assert dir1 == dir2
        assert os.path.isdir(dir1)


class TestCollectAndWriteEvents:
    @patch("src.worker.run.dynamodb.put_event")
    def test_writes_events_to_dynamodb(self, mock_put_event):
        with tempfile.TemporaryDirectory() as events_dir:
            # Write two event files
            with open(os.path.join(events_dir, "tf_plan.json"), "w") as f:
                json.dump({
                    "event_type": "tf_plan",
                    "status": "succeeded",
                    "message": "Plan: 3 to add",
                }, f)
            with open(os.path.join(events_dir, "tf_apply.json"), "w") as f:
                json.dump({
                    "event_type": "tf_apply",
                    "status": "succeeded",
                }, f)

            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order", "flow-1", "run-1",
            )

            assert count == 2
            assert mock_put_event.call_count == 2

            # Verify first call (tf_apply.json comes first alphabetically)
            calls = mock_put_event.call_args_list
            # Files are sorted, so tf_apply before tf_plan
            call_args_0 = calls[0]
            assert call_args_0[1]["trace_id"] == "trace-1"
            assert call_args_0[1]["order_name"] == "my-order"
            assert call_args_0[1]["event_type"] == "tf_apply"
            assert call_args_0[1]["status"] == "succeeded"
            assert call_args_0[1]["extra_fields"]["flow_id"] == "flow-1"
            assert call_args_0[1]["extra_fields"]["run_id"] == "run-1"

            call_args_1 = calls[1]
            assert call_args_1[1]["event_type"] == "tf_plan"
            assert call_args_1[1]["data"]["message"] == "Plan: 3 to add"

    @patch("src.worker.run.dynamodb.put_event")
    def test_empty_dir_no_calls(self, mock_put_event):
        with tempfile.TemporaryDirectory() as events_dir:
            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order",
            )
            assert count == 0
            mock_put_event.assert_not_called()

    @patch("src.worker.run.dynamodb.put_event")
    def test_nonexistent_dir_no_calls(self, mock_put_event):
        count = _collect_and_write_events(
            "/nonexistent/path", "trace-1", "my-order",
        )
        assert count == 0
        mock_put_event.assert_not_called()

    @patch("src.worker.run.dynamodb.put_event")
    def test_malformed_json_skipped(self, mock_put_event):
        with tempfile.TemporaryDirectory() as events_dir:
            # Write invalid JSON
            with open(os.path.join(events_dir, "bad.json"), "w") as f:
                f.write("not valid json{{{")
            # Write valid JSON
            with open(os.path.join(events_dir, "good.json"), "w") as f:
                json.dump({"event_type": "ok", "status": "info"}, f)

            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order",
            )
            assert count == 1
            mock_put_event.assert_called_once()

    @patch("src.worker.run.dynamodb.put_event")
    def test_non_dict_json_skipped(self, mock_put_event):
        with tempfile.TemporaryDirectory() as events_dir:
            with open(os.path.join(events_dir, "array.json"), "w") as f:
                json.dump([1, 2, 3], f)

            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order",
            )
            assert count == 0
            mock_put_event.assert_not_called()

    @patch("src.worker.run.dynamodb.put_event")
    def test_missing_fields_uses_fallbacks(self, mock_put_event):
        with tempfile.TemporaryDirectory() as events_dir:
            # JSON with no event_type or status — uses filename stem and "info"
            with open(os.path.join(events_dir, "custom_check.json"), "w") as f:
                json.dump({"message": "all good"}, f)

            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order",
            )
            assert count == 1
            call_kwargs = mock_put_event.call_args[1]
            assert call_kwargs["event_type"] == "custom_check"
            assert call_kwargs["status"] == "info"
            assert call_kwargs["data"]["message"] == "all good"

    @patch("src.worker.run.dynamodb.put_event")
    def test_dynamodb_error_does_not_crash(self, mock_put_event):
        mock_put_event.side_effect = Exception("DynamoDB unavailable")
        with tempfile.TemporaryDirectory() as events_dir:
            with open(os.path.join(events_dir, "event.json"), "w") as f:
                json.dump({"event_type": "test", "status": "ok"}, f)

            count = _collect_and_write_events(
                events_dir, "trace-1", "my-order",
            )
            assert count == 0  # Failed to write


class TestRun:
    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_successful_run(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create cmds.json
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {
                "CALLBACK_URL": "https://cb.url",
                "TRACE_ID": "tr-1",
                "ORDER_ID": "order-1",
                "FLOW_ID": "flow-1",
                "RUN_ID": "run-1",
            }

            status = run("s3://bucket/exec.zip")

            assert status == "succeeded"
            mock_callback.assert_called_once()
            call_args = mock_callback.call_args[0]
            assert call_args[0] == "https://cb.url"
            assert call_args[1] == "succeeded"

            # Verify events collection was called
            mock_collect.assert_called_once()
            collect_args = mock_collect.call_args[0]
            assert collect_args[1] == "tr-1"   # trace_id
            assert collect_args[2] == "order-1" # order_name
            assert collect_args[3] == "flow-1"  # flow_id
            assert collect_args[4] == "run-1"   # run_id

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_failed_run(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["exit 1"], f)

            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {
                "CALLBACK_URL": "https://cb.url",
                "TRACE_ID": "tr-1",
                "ORDER_ID": "order-1",
            }

            status = run("s3://bucket/exec.zip")

            assert status == "failed"
            mock_callback.assert_called_once()
            assert mock_callback.call_args[0][1] == "failed"
            # Events still collected even on failure
            mock_collect.assert_called_once()

    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_no_commands(self, mock_fetch, mock_decrypt, mock_callback):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {"CALLBACK_URL": "https://cb.url"}

            status = run("s3://bucket/exec.zip")

            assert status == "failed"

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_cmds_from_env(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {
                "CALLBACK_URL": "https://cb.url",
                "CMDS": json.dumps(["echo from_env"]),
                "TRACE_ID": "tr-1",
                "ORDER_ID": "order-1",
            }

            status = run("s3://bucket/exec.zip")

            assert status == "succeeded"

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_no_trace_id_skips_events(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        """Without TRACE_ID, events dir is not set up and collection is skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {"CALLBACK_URL": "https://cb.url"}

            status = run("s3://bucket/exec.zip")

            assert status == "succeeded"
            mock_collect.assert_not_called()

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_sops_key_passed_to_decrypt(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        """Verify sops_key_ssm_path is forwarded to _decrypt_and_load_env."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {"CALLBACK_URL": "https://cb.url"}

            run("s3://bucket/exec.zip", sops_key_ssm_path="/my/ssm/path")

            mock_decrypt.assert_called_once_with(tmpdir, sops_key_ssm_path="/my/ssm/path")

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run.fetch_code_s3")
    def test_no_environ_mutation(self, mock_fetch, mock_decrypt, mock_callback, mock_collect):
        """Verify run() does not mutate os.environ."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_fetch.return_value = tmpdir
            mock_decrypt.return_value = {
                "CALLBACK_URL": "https://cb.url",
                "TRACE_ID": "tr-1",
                "ORDER_ID": "order-1",
                "INJECTED_VAR": "should_not_leak",
            }

            env_before = os.environ.copy()
            run("s3://bucket/exec.zip")
            env_after = os.environ.copy()

            # os.environ should not have INJECTED_VAR
            assert "INJECTED_VAR" not in env_after
            # os.environ should not have AWS_EXE_SYS_EVENTS_DIR set by run()
            assert env_before.get("AWS_EXE_SYS_EVENTS_DIR") == env_after.get("AWS_EXE_SYS_EVENTS_DIR")
