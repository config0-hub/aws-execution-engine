"""Idempotency tests for aws_exe_sys/orchestrator/dispatch.py.

Ensures a single order cannot be dispatched twice via the conditional-update
reservation pattern (QUEUED -> DISPATCHING -> RUNNING).
"""

from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

from aws_exe_sys.common import dynamodb
from aws_exe_sys.common.models import RUNNING
from aws_exe_sys.orchestrator.dispatch import _dispatch_single


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")
    monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "test-internal")
    monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_WATCHDOG_SFN", "arn:aws:states:us-east-1:123:stateMachine:watchdog")


@pytest.fixture
def ddb_resource(aws_env):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-orders",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName="test-events",
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
        yield resource


@patch("aws_exe_sys.orchestrator.dispatch._start_watchdog")
@patch("aws_exe_sys.orchestrator.targets.lambda_target.LambdaTarget.dispatch")
def test_duplicate_dispatch_is_blocked(mock_lambda, mock_watchdog, ddb_resource):
    """A second dispatch attempt on the same order must not invoke the worker again.

    Simulates two concurrent orchestrator invocations racing on the same order.
    Only the first dispatch should flip QUEUED -> DISPATCHING; the second should
    hit the conditional check and short-circuit.
    """
    mock_lambda.return_value = "req-123"
    mock_watchdog.return_value = "arn:sfn:exec-1"

    dynamodb.put_order(
        "run-1",
        "0001",
        {
            "order_name": "dup-order",
            "status": "queued",
        },
        dynamodb_resource=ddb_resource,
    )

    order = {
        "order_num": "0001",
        "order_name": "dup-order",
        "execution_target": "lambda",
        "s3_location": "s3://bucket/exec.zip",
        "timeout": 300,
    }

    # First dispatch — should succeed and invoke the worker exactly once
    _dispatch_single(
        order, "run-1", "flow-1", "trace-1",
        "test-internal", dynamodb_resource=ddb_resource,
    )
    assert mock_lambda.call_count == 1

    # Verify order is now RUNNING (flipped past DISPATCHING)
    updated = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
    assert updated["status"] == RUNNING

    # Second dispatch on the same order — should NOT invoke the worker again
    _dispatch_single(
        order, "run-1", "flow-1", "trace-1",
        "test-internal", dynamodb_resource=ddb_resource,
    )
    assert mock_lambda.call_count == 1, "Worker was invoked twice — idempotency broken"


@patch("aws_exe_sys.orchestrator.dispatch._start_watchdog")
@patch("aws_exe_sys.orchestrator.targets.lambda_target.LambdaTarget.dispatch")
def test_dispatch_skipped_if_status_not_queued(mock_lambda, mock_watchdog, ddb_resource):
    """If the order row is not in QUEUED status, dispatch must be a no-op."""
    mock_lambda.return_value = "req-xyz"
    mock_watchdog.return_value = "arn:sfn:exec-xyz"

    dynamodb.put_order(
        "run-2",
        "0001",
        {
            "order_name": "already-running",
            "status": "running",  # not QUEUED — should skip
        },
        dynamodb_resource=ddb_resource,
    )

    order = {
        "order_num": "0001",
        "order_name": "already-running",
        "execution_target": "lambda",
        "s3_location": "s3://bucket/exec.zip",
        "timeout": 300,
    }

    _dispatch_single(
        order, "run-2", "flow-2", "trace-2",
        "test-internal", dynamodb_resource=ddb_resource,
    )

    assert mock_lambda.call_count == 0, "Worker was invoked for a non-queued order"
    assert mock_watchdog.call_count == 0, "Watchdog was started for a non-queued order"
