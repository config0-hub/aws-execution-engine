"""Unit tests for src/orchestrator/dispatch.py."""

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch, MagicMock

from src.common import dynamodb
from src.common.models import RUNNING
from src.orchestrator.dispatch import dispatch_orders, _dispatch_single


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")
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


class TestDispatchSingle:
    @patch("src.orchestrator.dispatch._start_watchdog")
    @patch("src.orchestrator.dispatch._dispatch_lambda")
    def test_lambda_dispatch(self, mock_lambda, mock_watchdog, ddb_resource):
        mock_lambda.return_value = "req-123"
        mock_watchdog.return_value = "arn:sfn:exec-1"

        # Insert order first
        dynamodb.put_order("run-1", "0001", {
            "order_name": "test", "status": "queued",
        }, dynamodb_resource=ddb_resource)

        order = {
            "order_num": "0001",
            "order_name": "test",
            "execution_target": "lambda",
            "s3_location": "s3://bucket/exec.zip",
            "timeout": 300,
        }

        result = _dispatch_single(
            order, "run-1", "flow-1", "trace-1",
            "test-internal", dynamodb_resource=ddb_resource,
        )

        assert result["execution_id"] == "req-123"
        mock_lambda.assert_called_once()
        mock_watchdog.assert_called_once()

        # Verify order updated to running
        updated = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        assert updated["status"] == RUNNING

    @patch("src.orchestrator.dispatch._start_watchdog")
    @patch("src.orchestrator.dispatch._dispatch_codebuild")
    def test_codebuild_dispatch(self, mock_cb, mock_watchdog, ddb_resource):
        mock_cb.return_value = "build-123"
        mock_watchdog.return_value = "arn:sfn:exec-2"

        dynamodb.put_order("run-1", "0001", {
            "order_name": "test", "status": "queued",
        }, dynamodb_resource=ddb_resource)

        order = {
            "order_num": "0001",
            "order_name": "test",
            "execution_target": "codebuild",
            "s3_location": "s3://bucket/exec.zip",
            "timeout": 300,
        }

        result = _dispatch_single(
            order, "run-1", "flow-1", "trace-1",
            "test-internal", dynamodb_resource=ddb_resource,
        )

        assert result["execution_id"] == "build-123"
        mock_cb.assert_called_once()

    @patch("src.orchestrator.dispatch._start_watchdog")
    @patch("src.orchestrator.dispatch._dispatch_ssm")
    def test_ssm_dispatch(self, mock_ssm, mock_watchdog, ddb_resource):
        mock_ssm.return_value = "cmd-123"
        mock_watchdog.return_value = "arn:sfn:exec-3"

        dynamodb.put_order("run-1", "0001", {
            "order_name": "test", "status": "queued",
        }, dynamodb_resource=ddb_resource)

        order = {
            "order_num": "0001",
            "order_name": "test",
            "execution_target": "ssm",
            "s3_location": "s3://bucket/exec.zip",
            "timeout": 300,
            "ssm_targets": {"instance_ids": ["i-abc123"]},
        }

        result = _dispatch_single(
            order, "run-1", "flow-1", "trace-1",
            "test-internal", dynamodb_resource=ddb_resource,
        )

        assert result["execution_id"] == "cmd-123"
        mock_ssm.assert_called_once()
        mock_watchdog.assert_called_once()

        # Verify order updated to running
        updated = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        assert updated["status"] == RUNNING



class TestDispatchOrders:
    @patch("src.orchestrator.dispatch._start_watchdog")
    @patch("src.orchestrator.dispatch._dispatch_lambda")
    def test_parallel_dispatch(self, mock_lambda, mock_watchdog, ddb_resource):
        mock_lambda.return_value = "req-id"
        mock_watchdog.return_value = "arn:sfn"

        for i in range(3):
            num = f"000{i+1}"
            dynamodb.put_order("run-1", num, {
                "order_name": f"order-{i}", "status": "queued",
            }, dynamodb_resource=ddb_resource)

        orders = [
            {"order_num": f"000{i+1}", "order_name": f"order-{i}",
             "execution_target": "lambda", "s3_location": "s3://b/e.zip", "timeout": 300}
            for i in range(3)
        ]

        results = dispatch_orders(
            orders, "run-1", "flow-1", "trace-1",
            internal_bucket="test-internal",
            dynamodb_resource=ddb_resource,
        )

        assert len(results) == 3
        assert mock_lambda.call_count == 3

    def test_empty_list(self, ddb_resource):
        results = dispatch_orders(
            [], "run-1", "flow-1", "trace-1",
            dynamodb_resource=ddb_resource,
        )
        assert results == []
