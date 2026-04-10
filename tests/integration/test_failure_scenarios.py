"""Integration test: failure scenarios (dep failure, timeout, lock contention)."""

import json
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from src.common.models import QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT, JOB_ORDER_NAME
from src.orchestrator.handler import handler as orch_handler
from src.watchdog_check.handler import handler as watchdog_handler


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def aws_env(monkeypatch):
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
    s3.put_object(
        Bucket="test-internal",
        Key=f"tmp/callbacks/runs/{run_id}/{order_num}/result.json",
        Body=json.dumps({"status": status, "log": log}),
    )


@pytest.fixture
def mock_aws_resources(aws_env):
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
class TestMustSucceedFailure:

    def test_dep_failure_cascades_and_finalizes(self, mock_aws_resources):
        """order-1 (must_succeed) fails → order-2 (depends on 1) marked failed → job failed."""
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]
        run_id = "run-fail-1"

        # order-1: must_succeed=True, running
        _insert_order(ddb, run_id, "0001", "order-1", RUNNING, must_succeed=True)
        # order-2: depends on order-1, queued
        _insert_order(ddb, run_id, "0002", "order-2", QUEUED,
                      deps=["0001"], must_succeed=True)

        # order-1 fails
        _write_result(s3, run_id, "0001", "failed", "exit code 1")

        result = orch_handler(_s3_event(run_id, "0001"))
        assert result["status"] == "finalized"

        # Verify order-1 is failed
        o1 = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0001"}
        )["Item"]
        assert o1["status"] == FAILED

        # Verify order-2 is failed (dependency failed)
        o2 = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0002"}
        )["Item"]
        assert o2["status"] == FAILED

        # Verify done endpoint written with failed status
        done_resp = s3.get_object(Bucket="test-done", Key=f"{run_id}/done")
        done_data = json.loads(done_resp["Body"].read())
        assert done_data["status"] == FAILED

        # Verify job completion event
        events = ddb.Table("test-order-events").scan()["Items"]
        job_events = [
            e for e in events
            if e["order_name"] == JOB_ORDER_NAME and e["event_type"] == "job_completed"
        ]
        assert len(job_events) == 1
        assert job_events[0]["status"] == FAILED


@pytest.mark.integration
class TestWatchdogTimeout:

    def test_watchdog_writes_timed_out_result(self, mock_aws_resources):
        """Watchdog detects timeout and writes timed_out result.json."""
        s3 = mock_aws_resources["s3"]
        run_id = "run-timeout-1"

        # No result.json exists — invoke watchdog with expired timeout
        watchdog_event = {
            "run_id": run_id,
            "order_num": "0001",
            "timeout": 60,
            "start_time": int(time.time()) - 120,  # 120s ago, timeout is 60s
            "internal_bucket": "test-internal",
        }

        result = watchdog_handler(watchdog_event)
        assert result["done"] is True

        # Verify timed_out result.json was written
        resp = s3.get_object(
            Bucket="test-internal",
            Key=f"tmp/callbacks/runs/{run_id}/0001/result.json",
        )
        result_data = json.loads(resp["Body"].read())
        assert result_data["status"] == "timed_out"
        assert "watchdog" in result_data["log"].lower()

    def test_watchdog_returns_false_when_waiting(self, mock_aws_resources):
        """Watchdog returns done=False when timeout not yet exceeded."""
        run_id = "run-timeout-2"

        watchdog_event = {
            "run_id": run_id,
            "order_num": "0001",
            "timeout": 600,
            "start_time": int(time.time()),  # just started
            "internal_bucket": "test-internal",
        }

        result = watchdog_handler(watchdog_event)
        assert result["done"] is False

    def test_watchdog_returns_true_when_result_exists(self, mock_aws_resources):
        """Watchdog returns done=True when result.json already exists."""
        s3 = mock_aws_resources["s3"]
        run_id = "run-timeout-3"

        # Write existing result
        _write_result(s3, run_id, "0001", "succeeded")

        watchdog_event = {
            "run_id": run_id,
            "order_num": "0001",
            "timeout": 600,
            "start_time": int(time.time()),
            "internal_bucket": "test-internal",
        }

        result = watchdog_handler(watchdog_event)
        assert result["done"] is True

    def test_timed_out_order_triggers_finalization(self, mock_aws_resources):
        """After watchdog writes timed_out, orchestrator finalizes with timed_out status."""
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]
        run_id = "run-timeout-4"

        _insert_order(ddb, run_id, "0001", "order-1", RUNNING)

        # Watchdog writes timed_out
        watchdog_event = {
            "run_id": run_id,
            "order_num": "0001",
            "timeout": 60,
            "start_time": int(time.time()) - 120,
            "internal_bucket": "test-internal",
        }
        watchdog_handler(watchdog_event)

        # Orchestrator picks up the timed_out result
        result = orch_handler(_s3_event(run_id, "0001"))
        assert result["status"] == "finalized"

        # Verify order is timed_out
        o1 = ddb.Table("test-orders").get_item(
            Key={"pk": f"{run_id}:0001"}
        )["Item"]
        assert o1["status"] == TIMED_OUT

        # Verify done endpoint
        done_resp = s3.get_object(Bucket="test-done", Key=f"{run_id}/done")
        done_data = json.loads(done_resp["Body"].read())
        assert done_data["status"] == TIMED_OUT


@pytest.mark.integration
class TestLockContention:

    def test_concurrent_orchestrator_only_one_proceeds(self, mock_aws_resources):
        """Two orchestrator invocations for same run_id — only one acquires lock."""
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]
        run_id = "run-contention-1"

        _insert_order(ddb, run_id, "0001", "order-1", RUNNING)
        _write_result(s3, run_id, "0001", "succeeded")

        # Pre-acquire the lock (simulate another instance)
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

        # This invocation should fail to acquire lock
        result = orch_handler(_s3_event(run_id, "0001"))
        assert result["status"] == "skipped"
