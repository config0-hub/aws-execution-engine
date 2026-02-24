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
    _download_and_extract,
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


class TestSetupEventsDir:
    def test_creates_directory(self):
        with tempfile.TemporaryDirectory() as base:
            with patch.dict(os.environ, {}, clear=False):
                trace_id = "test-trace-123"
                # Override the base path to use temp dir
                with patch("src.worker.run._setup_events_dir") as _:
                    events_dir = os.path.join(base, trace_id, "events")
                    os.makedirs(events_dir, exist_ok=True)
                    assert os.path.isdir(events_dir)

    def test_sets_env_var(self):
        with patch.dict(os.environ, {}, clear=False):
            trace_id = "test-trace-456"
            events_dir = _setup_events_dir(trace_id)
            assert os.environ["AWS_EXE_SYS_EVENTS_DIR"] == events_dir
            assert events_dir == f"/tmp/share/{trace_id}/events"

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
    @patch("src.worker.run._download_and_extract")
    def test_successful_run(self, mock_download, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create cmds.json
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_download.return_value = tmpdir
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
    @patch("src.worker.run._download_and_extract")
    def test_failed_run(self, mock_download, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["exit 1"], f)

            mock_download.return_value = tmpdir
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
    @patch("src.worker.run._download_and_extract")
    def test_no_commands(self, mock_download, mock_decrypt, mock_callback):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_download.return_value = tmpdir
            mock_decrypt.return_value = {"CALLBACK_URL": "https://cb.url"}

            status = run("s3://bucket/exec.zip")

            assert status == "failed"

    @patch("src.worker.run._collect_and_write_events")
    @patch("src.worker.run.send_callback")
    @patch("src.worker.run._decrypt_and_load_env")
    @patch("src.worker.run._download_and_extract")
    def test_cmds_from_env(self, mock_download, mock_decrypt, mock_callback, mock_collect):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_download.return_value = tmpdir
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
    @patch("src.worker.run._download_and_extract")
    def test_no_trace_id_skips_events(self, mock_download, mock_decrypt, mock_callback, mock_collect):
        """Without TRACE_ID, events dir is not set up and collection is skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "cmds.json"), "w") as f:
                json.dump(["echo hello"], f)

            mock_download.return_value = tmpdir
            mock_decrypt.return_value = {"CALLBACK_URL": "https://cb.url"}

            status = run("s3://bucket/exec.zip")

            assert status == "succeeded"
            mock_collect.assert_not_called()
