"""Integration test: orchestrator flow with mocked AWS."""

import json
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from src.common.models import QUEUED, RUNNING, SUCCEEDED, FAILED, JOB_ORDER_NAME
from src.orchestrator.handler import handler as orch_handler


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
    monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_WATCHDOG_SFN", "arn:aws:states:us-east-1:123456:stateMachine:aws-exe-sys-watchdog")


def _s3_event(run_id: str, order_num: str) -> dict:
    """Build a synthetic S3 event for orchestrator invocation."""
    return {
        "Records": [{
            "s3": {
                "bucket": {"name": "test-internal"},
                "object": {
                    "key": f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
                },
            }
        }]
    }


def _insert_order(ddb, run_id, order_num, order_name, status, deps=None,
                   must_succeed=True, trace_id="trace-1", flow_id="user:trace-1-exec"):
    """Insert an order directly into DynamoDB."""
    table = ddb.Table("test-orders")
    now = int(time.time())
    table.put_item(Item={
        "pk": f"{run_id}:{order_num}",
        "run_id": run_id,
        "order_num": order_num,
        "order_name": order_name,
        "status": status,
        "cmds": ["echo test"],
        "timeout": 300,
        "trace_id": trace_id,
        "flow_id": flow_id,
        "queue_id": order_num,
        "dependencies": deps or [],
        "must_succeed": must_succeed,
        "execution_target": "lambda",
        "s3_location": f"s3://test-internal/tmp/exec/{run_id}/{order_num}/exec.zip",
        "callback_url": f"https://presigned/{run_id}/{order_num}",
        "created_at": now,
        "last_update": now,
        "ttl": now + 86400,
    })


def _write_result(s3, run_id, order_num, status="succeeded", log="done"):
    """Write a result.json to S3 callback path."""
    key = f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
    s3.put_object(
        Bucket="test-internal",
        Key=key,
        Body=json.dumps({"status": status, "log": log}),
    )


@pytest.fixture
def mock_aws_resources(aws_env):
    """Create mocked DynamoDB tables and S3 buckets."""
    with mock_aws():
        region = "us-east-1"

        ddb = boto3.resource("dynamodb", region_name=region)
        ddb.create_table(
            TableName="test-orders",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "run_id", "AttributeType": "S"},
                {"AttributeName": "order_num", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "run_id-order_num-index",
                    "KeySchema": [
                        {"AttributeName": "run_id", "KeyType": "HASH"},
                        {"AttributeName": "order_num", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
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
        ddb.create_table(
            TableName="test-locks",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(Bucket="test-internal")
        s3.create_bucket(Bucket="test-done")

        yield {"ddb": ddb, "s3": s3}


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.integration
class TestOrchestratorFlow:

    @patch("src.orchestrator.dispatch._start_watchdog", return_value="arn:watchdog:exec")
    @patch("src.orchestrator.dispatch._dispatch_lambda", return_value="req-123")
    def test_partial_completion_then_dispatch(
        self, mock_lambda, mock_watchdog, mock_aws_resources,
    ):
        """order-1 completes, order-2 still running, order-3 waits.
        Then order-2 completes, order-3 gets dispatched."""
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]
        run_id = "run-orch-1"

        # Pre-populate 3 orders
        _insert_order(ddb, run_id, "0001", "order-1", RUNNING)
        _insert_order(ddb, run_id, "0002", "order-2", RUNNING)
        _insert_order(ddb, run_id, "0003", "order-3", QUEUED,
                      deps=["0001", "0002"])

        # order-1 completes
        _write_result(s3, run_id, "0001", "succeeded")

        # Invoke orchestrator for order-1 completion
        result = orch_handler(_s3_event(run_id, "0001"))
        assert result["status"] == "in_progress"

        # Verify order-1 updated to succeeded in DynamoDB
        order1 = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0001"}
        )["Item"]
        assert order1["status"] == SUCCEEDED

        # order-3 should NOT be dispatched (order-2 still running)
        order3 = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0003"}
        )["Item"]
        assert order3["status"] == QUEUED

        # Verify lock was released (status=completed)
        lock = ddb.Table("test-locks").get_item(
            Key={"pk": f"lock:{run_id}"}
        )["Item"]
        assert lock["status"] == "completed"

        # Now order-2 completes
        _write_result(s3, run_id, "0002", "succeeded")
        result2 = orch_handler(_s3_event(run_id, "0002"))

        # order-3 should now be dispatched (all deps satisfied)
        order3_after = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0003"}
        )["Item"]
        assert order3_after["status"] == RUNNING

        # Verify dispatch was called for order-3
        assert mock_lambda.call_count >= 1

        # Verify order events were written
        events_table = ddb.Table("test-order-events")
        events = events_table.scan()["Items"]
        completed_events = [e for e in events if e["event_type"] == "completed"]
        assert len(completed_events) >= 2  # order-1 and order-2

        dispatched_events = [e for e in events if e["event_type"] == "dispatched"]
        assert len(dispatched_events) >= 1  # order-3

    def test_lock_prevents_concurrent_execution(self, mock_aws_resources):
        """Only one orchestrator acquires the lock; the other exits cleanly."""
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]
        run_id = "run-lock-1"

        _insert_order(ddb, run_id, "0001", "order-1", RUNNING)
        _write_result(s3, run_id, "0001", "succeeded")

        # First invocation acquires lock
        result1 = orch_handler(_s3_event(run_id, "0001"))
        # The lock should be set after first handler runs

        # Manually set lock to "active" to simulate concurrent contention
        ddb.Table("test-locks").put_item(Item={
            "pk": f"lock:{run_id}",
            "run_id": run_id,
            "orchestrator_id": "other-instance",
            "status": "active",
            "acquired_at": int(time.time()),
            "ttl": int(time.time()) + 3600,
            "flow_id": "",
            "trace_id": "",
        })

        # Second invocation should be rejected
        result2 = orch_handler(_s3_event(run_id, "0001"))
        assert result2["status"] == "skipped"
        assert "Lock not acquired" in result2["message"]
