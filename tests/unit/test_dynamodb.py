"""Unit tests for src/common/dynamodb.py using moto."""

import os
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.common import dynamodb


@pytest.fixture
def aws_env(monkeypatch):
    """Set up environment variables and mock AWS."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-order-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")


@pytest.fixture
def ddb_resource(aws_env):
    """Create mock DynamoDB tables and return the resource."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")

        # Orders table
        resource.create_table(
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

        # Order events table
        resource.create_table(
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

        # Locks table
        resource.create_table(
            TableName="test-locks",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )

        yield resource


class TestOrdersTable:
    def test_put_and_get_order(self, ddb_resource):
        order_data = {
            "trace_id": "abc123",
            "flow_id": "user:abc123-exec",
            "order_name": "deploy-vpc",
            "cmds": ["echo hi"],
            "status": "queued",
        }
        dynamodb.put_order("run-1", "001", order_data, dynamodb_resource=ddb_resource)
        result = dynamodb.get_order("run-1", "001", dynamodb_resource=ddb_resource)
        assert result is not None
        assert result["pk"] == "run-1:001"
        assert result["order_name"] == "deploy-vpc"
        assert result["status"] == "queued"

    def test_get_order_not_found(self, ddb_resource):
        result = dynamodb.get_order("nonexistent", "001", dynamodb_resource=ddb_resource)
        assert result is None

    def test_get_all_orders(self, ddb_resource):
        for i in range(3):
            dynamodb.put_order(
                "run-1",
                f"00{i+1}",
                {"order_name": f"order-{i+1}", "status": "queued"},
                dynamodb_resource=ddb_resource,
            )
        # Also insert an order for a different run
        dynamodb.put_order(
            "run-2", "001", {"order_name": "other", "status": "queued"},
            dynamodb_resource=ddb_resource,
        )

        results = dynamodb.get_all_orders("run-1", dynamodb_resource=ddb_resource)
        assert len(results) == 3

    def test_update_order_status(self, ddb_resource):
        dynamodb.put_order(
            "run-1", "001",
            {"status": "queued", "order_name": "test"},
            dynamodb_resource=ddb_resource,
        )
        dynamodb.update_order_status(
            "run-1", "001", "running",
            dynamodb_resource=ddb_resource,
        )
        result = dynamodb.get_order("run-1", "001", dynamodb_resource=ddb_resource)
        assert result["status"] == "running"
        assert "last_update" in result

    def test_update_order_status_with_extra_fields(self, ddb_resource):
        dynamodb.put_order(
            "run-1", "001",
            {"status": "queued", "order_name": "test"},
            dynamodb_resource=ddb_resource,
        )
        dynamodb.update_order_status(
            "run-1", "001", "running",
            extra_fields={"execution_url": "https://example.com"},
            dynamodb_resource=ddb_resource,
        )
        result = dynamodb.get_order("run-1", "001", dynamodb_resource=ddb_resource)
        assert result["execution_url"] == "https://example.com"


class TestOrderEventsTable:
    def test_put_and_get_events(self, ddb_resource):
        dynamodb.put_event(
            "trace-1", "deploy-vpc", "dispatched", "running",
            dynamodb_resource=ddb_resource,
        )
        events = dynamodb.get_events("trace-1", dynamodb_resource=ddb_resource)
        assert len(events) == 1
        assert events[0]["order_name"] == "deploy-vpc"
        assert events[0]["event_type"] == "dispatched"

    def test_get_events_with_prefix(self, ddb_resource):
        dynamodb.put_event(
            "trace-1", "deploy-vpc", "dispatched", "running",
            dynamodb_resource=ddb_resource,
        )
        dynamodb.put_event(
            "trace-1", "deploy-rds", "dispatched", "running",
            dynamodb_resource=ddb_resource,
        )
        dynamodb.put_event(
            "trace-1", "_job", "started", "running",
            dynamodb_resource=ddb_resource,
        )

        vpc_events = dynamodb.get_events(
            "trace-1", order_name_prefix="deploy-vpc",
            dynamodb_resource=ddb_resource,
        )
        assert len(vpc_events) == 1
        assert vpc_events[0]["order_name"] == "deploy-vpc"

    def test_get_latest_event(self, ddb_resource):
        dynamodb.put_event(
            "trace-1", "deploy-vpc", "dispatched", "running",
            dynamodb_resource=ddb_resource,
        )
        # Small sleep to ensure different epoch
        import time
        time.sleep(1.1)
        dynamodb.put_event(
            "trace-1", "deploy-vpc", "completed", "succeeded",
            dynamodb_resource=ddb_resource,
        )

        latest = dynamodb.get_latest_event(
            "trace-1", "deploy-vpc",
            dynamodb_resource=ddb_resource,
        )
        assert latest is not None
        assert latest["event_type"] == "completed"
        assert latest["status"] == "succeeded"

    def test_get_latest_event_not_found(self, ddb_resource):
        result = dynamodb.get_latest_event(
            "nonexistent", "noorder",
            dynamodb_resource=ddb_resource,
        )
        assert result is None

    def test_put_event_with_extra_fields(self, ddb_resource):
        dynamodb.put_event(
            "trace-1", "deploy-vpc", "dispatched", "running",
            extra_fields={"execution_url": "https://exec.example.com"},
            dynamodb_resource=ddb_resource,
        )
        events = dynamodb.get_events("trace-1", dynamodb_resource=ddb_resource)
        assert events[0]["execution_url"] == "https://exec.example.com"


class TestLocksTable:
    def test_acquire_lock(self, ddb_resource):
        acquired = dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        assert acquired is True

    def test_acquire_lock_fails_if_active(self, ddb_resource):
        dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        acquired = dynamodb.acquire_lock(
            "run-1", "orch-2", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        assert acquired is False

    def test_acquire_lock_succeeds_after_release(self, ddb_resource):
        dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        dynamodb.release_lock("run-1", dynamodb_resource=ddb_resource)

        acquired = dynamodb.acquire_lock(
            "run-1", "orch-2", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        assert acquired is True

    def test_release_lock(self, ddb_resource):
        dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        dynamodb.release_lock("run-1", dynamodb_resource=ddb_resource)

        lock = dynamodb.get_lock("run-1", dynamodb_resource=ddb_resource)
        assert lock["status"] == "completed"

    def test_get_lock(self, ddb_resource):
        dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        lock = dynamodb.get_lock("run-1", dynamodb_resource=ddb_resource)
        assert lock is not None
        assert lock["run_id"] == "run-1"
        assert lock["orchestrator_id"] == "orch-1"
        assert lock["status"] == "active"

    def test_get_lock_not_found(self, ddb_resource):
        lock = dynamodb.get_lock("nonexistent", dynamodb_resource=ddb_resource)
        assert lock is None


def _throttle_error(code="ProvisionedThroughputExceededException"):
    """Create a ClientError simulating DynamoDB throttling."""
    return ClientError(
        {"Error": {"Code": code, "Message": "Rate exceeded"}},
        "PutItem",
    )


def _access_denied_error():
    """Create a non-throttle ClientError."""
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "Forbidden"}},
        "PutItem",
    )


class TestRetryOnThrottle:
    @patch("src.common.dynamodb.time.sleep")
    def test_retries_on_provisioned_throughput_exceeded(self, mock_sleep, ddb_resource):
        """Throttling on first call, succeeds on retry."""
        real_table = ddb_resource.Table("test-orders")
        original_put = real_table.put_item
        call_count = {"n": 0}

        def flaky_put(**kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise _throttle_error()
            return original_put(**kwargs)

        mock_table = MagicMock(wraps=real_table)
        mock_table.put_item = flaky_put

        with patch("src.common.dynamodb._get_table", return_value=mock_table):
            dynamodb.put_order(
                "run-1", "001",
                {"status": "queued", "order_name": "test"},
                dynamodb_resource=ddb_resource,
            )

        assert call_count["n"] == 2
        assert mock_sleep.call_count == 1

    @patch("src.common.dynamodb.time.sleep")
    def test_retries_on_throttling_exception(self, mock_sleep, ddb_resource):
        """Also retries on ThrottlingException error code."""
        real_table = ddb_resource.Table("test-orders")
        original_get = real_table.get_item
        call_count = {"n": 0}

        def flaky_get(**kwargs):
            call_count["n"] += 1
            if call_count["n"] <= 2:
                raise _throttle_error("ThrottlingException")
            return original_get(**kwargs)

        mock_table = MagicMock(wraps=real_table)
        mock_table.get_item = flaky_get

        with patch("src.common.dynamodb._get_table", return_value=mock_table):
            result = dynamodb.get_order("run-1", "001", dynamodb_resource=ddb_resource)

        assert call_count["n"] == 3
        assert mock_sleep.call_count == 2
        assert result is None  # item doesn't exist, but no error

    @patch("src.common.dynamodb.time.sleep")
    def test_raises_after_max_retries_exhausted(self, mock_sleep, ddb_resource):
        """Gives up after MAX_RETRIES and re-raises the throttle error."""
        mock_table = MagicMock()
        mock_table.put_item.side_effect = _throttle_error()

        with patch("src.common.dynamodb._get_table", return_value=mock_table):
            with pytest.raises(ClientError) as exc_info:
                dynamodb.put_order(
                    "run-1", "001",
                    {"status": "queued", "order_name": "test"},
                    dynamodb_resource=ddb_resource,
                )

        assert exc_info.value.response["Error"]["Code"] == "ProvisionedThroughputExceededException"
        assert mock_sleep.call_count == dynamodb.MAX_RETRIES

    @patch("src.common.dynamodb.time.sleep")
    def test_does_not_retry_non_throttle_errors(self, mock_sleep, ddb_resource):
        """Non-throttle ClientErrors are raised immediately without retry."""
        mock_table = MagicMock()
        mock_table.put_item.side_effect = _access_denied_error()

        with patch("src.common.dynamodb._get_table", return_value=mock_table):
            with pytest.raises(ClientError) as exc_info:
                dynamodb.put_order(
                    "run-1", "001",
                    {"status": "queued", "order_name": "test"},
                    dynamodb_resource=ddb_resource,
                )

        assert exc_info.value.response["Error"]["Code"] == "AccessDeniedException"
        assert mock_sleep.call_count == 0  # no retries

    def test_conditional_check_not_retried_on_acquire_lock(self, ddb_resource):
        """ConditionalCheckFailedException on acquire_lock is not retried (expected behavior)."""
        # First acquire succeeds
        dynamodb.acquire_lock(
            "run-1", "orch-1", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        # Second acquire fails with ConditionalCheckFailed (not a throttle)
        result = dynamodb.acquire_lock(
            "run-1", "orch-2", 3600, "user:abc-exec", "abc",
            dynamodb_resource=ddb_resource,
        )
        assert result is False
