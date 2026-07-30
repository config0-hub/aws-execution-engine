"""Unit tests for aws_exe_sys/worker/handler.py.

Focus: the always-write-a-result invariant must hold even for a PRE-run
failure (a bad payload that never reaches run()). The WATCH side treats an
absent done-marker as "still running", so a skipped write parks the order
forever — the handler must write a ``failed`` ExecutionResult anyway.
"""

import base64
import json
from unittest.mock import patch

import boto3
from moto import mock_aws
import pytest

from aws_exe_sys.worker.handler import handler


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


DONE_BUCKET = "done-bucket"
DONE_KEY = "executions/trg-bad/result.json"
DONE_ENDPOINT = f"s3://{DONE_BUCKET}/{DONE_KEY}"


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def s3_client(aws_env):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=DONE_BUCKET)
        yield client


def _read_done_marker(s3_client) -> dict:
    obj = s3_client.get_object(Bucket=DONE_BUCKET, Key=DONE_KEY)
    return json.loads(obj["Body"].read().decode())


class TestPreRunFailureStillWritesResult:
    def test_bad_payload_writes_failed_execution_result(self, s3_client):
        """A validation failure (empty trigger_id, bad target) never reaches
        run(), yet a failed ExecutionResult must still land on done_endpoint."""
        event = {
            "trigger_id": "",  # invalid — fails validate() before run()
            "s3_package_uri": "s3://pkg-bucket/exec.zip",
            "sops_type": None,
            "sops_path": None,
            "commands_b64": _b64_cmds(["echo hi"]),
            "done_endpoint": DONE_ENDPOINT,
            "execution_target": "not-a-real-target",
        }

        resp = handler(event)

        assert resp["status"] == "failed"
        assert "pre-run failure" in resp["error"]

        marker = _read_done_marker(s3_client)
        assert marker["status"] == "failed"
        assert marker["trigger_id"] == ""
        assert "pre-run failure" in marker["error"]

    def test_failed_marker_carries_trigger_id_when_present(self, s3_client):
        """Even when only the execution_target is invalid, the failed marker
        is written and carries the trigger_id from the raw payload."""
        event = {
            "trigger_id": "trg-bad",
            "s3_package_uri": "s3://pkg-bucket/exec.zip",
            "sops_type": None,
            "sops_path": None,
            "commands_b64": _b64_cmds(["echo hi"]),
            "done_endpoint": DONE_ENDPOINT,
            "execution_target": "ecs",  # invalid
        }

        resp = handler(event)

        assert resp["status"] == "failed"
        marker = _read_done_marker(s3_client)
        assert marker["status"] == "failed"
        assert marker["trigger_id"] == "trg-bad"

    @patch("aws_exe_sys.worker.handler.write_result")
    def test_marker_write_failure_is_raised(self, mock_write):
        mock_write.side_effect = OSError("S3 write failed")
        event = {
            "trigger_id": "trg-bad",
            "s3_package_uri": "s3://pkg-bucket/exec.zip",
            "sops_type": None,
            "sops_path": None,
            "commands_b64": _b64_cmds(["echo hi"]),
            "done_endpoint": DONE_ENDPOINT,
            "execution_target": "removed-target",
        }

        with pytest.raises(OSError, match="S3 write failed"):
            handler(event)

    def test_missing_done_endpoint_is_raised(self):
        event = {
            "trigger_id": "trg-bad",
            "s3_package_uri": "s3://pkg-bucket/exec.zip",
            "sops_type": None,
            "sops_path": None,
            "commands_b64": _b64_cmds(["echo hi"]),
            "done_endpoint": "",
            "execution_target": "removed-target",
        }

        with pytest.raises(ValueError, match="no done_endpoint"):
            handler(event)
