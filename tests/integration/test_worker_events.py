"""Integration test: worker subprocess events → DynamoDB order_events."""

import json
import os
import tempfile
import zipfile
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def aws_env(monkeypatch):
    """Set up environment variables for mocked AWS."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-order-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")
    monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "test-internal")
    monkeypatch.setenv("AWS_EXE_SYS_DONE_BUCKET", "test-done")
    monkeypatch.setenv("SOPS_AGE_KEY", "AGE-SECRET-KEY-MOCK-FOR-TESTING")


@pytest.fixture
def mock_aws_resources(aws_env):
    """Create mocked DynamoDB tables and S3 buckets."""
    with mock_aws():
        region = "us-east-1"

        # DynamoDB
        ddb = boto3.resource("dynamodb", region_name=region)
        ddb.create_table(
            TableName="test-order-events",
            KeySchema=[
                {"AttributeName": "trace_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "trace_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        # S3
        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(Bucket="test-internal")

        yield {"ddb": ddb, "s3": s3}


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.integration
class TestWorkerEvents:

    @patch("src.worker.run.send_callback")
    @patch("src.common.sops.decrypt_env")
    def test_subprocess_events_reach_dynamodb(
        self, mock_sops_decrypt, mock_callback, mock_aws_resources,
    ):
        """A command writes a JSON event file to $AWS_EXE_SYS_EVENTS_DIR.
        After execution, the worker reads it and writes to DynamoDB."""

        trace_id = "test-trace-events"
        order_name = "plan-order"

        # Mock SOPS to return engine env vars
        mock_sops_decrypt.return_value = {
            "TRACE_ID": trace_id,
            "ORDER_ID": order_name,
            "ORDER_NUM": "0001",
            "FLOW_ID": "user:test-flow",
            "RUN_ID": "run-evt-1",
            "CALLBACK_URL": "https://callback.test",
            "CMDS": json.dumps([
                # The subprocess writes a JSON event file to $AWS_EXE_SYS_EVENTS_DIR
                'echo \'{"event_type":"tf_plan","status":"succeeded","message":"Plan: 2 to add"}\' > $AWS_EXE_SYS_EVENTS_DIR/tf_plan.json',
            ]),
        }

        # Create a minimal exec.zip with a secrets.enc.json
        s3 = mock_aws_resources["s3"]
        with tempfile.TemporaryDirectory() as tmpdir:
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("secrets.enc.json", "{}")
            buf.seek(0)
            s3.put_object(
                Bucket="test-internal",
                Key="tmp/exec/run-evt-1/0001/exec.zip",
                Body=buf.read(),
            )

        from src.worker.run import run
        status = run("s3://test-internal/tmp/exec/run-evt-1/0001/exec.zip")

        assert status == "succeeded"

        # Verify the event was written to DynamoDB
        ddb = mock_aws_resources["ddb"]
        events_table = ddb.Table("test-order-events")
        result = events_table.scan()
        items = result["Items"]

        assert len(items) == 1
        event = items[0]
        assert event["trace_id"] == trace_id
        assert event["order_name"] == order_name
        assert event["event_type"] == "tf_plan"
        assert event["status"] == "succeeded"
        assert event["data"]["message"] == "Plan: 2 to add"
        assert event["flow_id"] == "user:test-flow"
        assert event["run_id"] == "run-evt-1"

        # Verify callback was still sent
        mock_callback.assert_called_once()
        assert mock_callback.call_args[0][1] == "succeeded"

    @patch("src.worker.run.send_callback")
    @patch("src.common.sops.decrypt_env")
    def test_no_events_written_when_subprocess_writes_nothing(
        self, mock_sops_decrypt, mock_callback, mock_aws_resources,
    ):
        """When no event files are written, DynamoDB stays empty."""

        mock_sops_decrypt.return_value = {
            "TRACE_ID": "trace-no-events",
            "ORDER_ID": "simple-order",
            "ORDER_NUM": "0001",
            "FLOW_ID": "user:test",
            "RUN_ID": "run-no-evt",
            "CALLBACK_URL": "https://callback.test",
            "CMDS": json.dumps(["echo hello"]),
        }

        s3 = mock_aws_resources["s3"]
        with tempfile.TemporaryDirectory() as tmpdir:
            import io
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("secrets.enc.json", "{}")
            buf.seek(0)
            s3.put_object(
                Bucket="test-internal",
                Key="tmp/exec/run-no-evt/0001/exec.zip",
                Body=buf.read(),
            )

        from src.worker.run import run
        status = run("s3://test-internal/tmp/exec/run-no-evt/0001/exec.zip")

        assert status == "succeeded"

        ddb = mock_aws_resources["ddb"]
        events_table = ddb.Table("test-order-events")
        result = events_table.scan()
        assert len(result["Items"]) == 0
