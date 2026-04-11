"""Tests for the expired-lock steal path in aws_exe_sys/common/dynamodb.py::acquire_lock.

DynamoDB TTL cleanup is eventually consistent (up to 48 h), so an abandoned
lock with ttl < now should be stealable immediately by the next orchestrator.
"""

import time
from decimal import Decimal

import boto3
import pytest
from moto import mock_aws

from aws_exe_sys.common import dynamodb


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")


@pytest.fixture
def ddb_resource(aws_env):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-locks",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


def _seed_lock(ddb_resource, run_id, status, ttl):
    table = ddb_resource.Table("test-locks")
    table.put_item(
        Item={
            "pk": f"lock:{run_id}",
            "run_id": run_id,
            "orchestrator_id": "orch-seed",
            "status": status,
            "acquired_at": int(time.time()) - 3600,
            "ttl": Decimal(ttl),
            "flow_id": "flow-seed",
            "trace_id": "trace-seed",
        }
    )


def test_expired_lock_is_stealable(ddb_resource):
    """A lock with ttl < now should be stealable even if status is 'active'."""
    run_id = "run-expired"
    _seed_lock(ddb_resource, run_id, status="active", ttl=int(time.time()) - 60)

    acquired = dynamodb.acquire_lock(
        run_id=run_id,
        orchestrator_id="orch-new",
        ttl=3600,
        flow_id="flow-new",
        trace_id="trace-new",
        dynamodb_resource=ddb_resource,
    )
    assert acquired is True

    # Verify the new lock row overwrote the stale one
    current = dynamodb.get_lock(run_id, dynamodb_resource=ddb_resource)
    assert current["orchestrator_id"] == "orch-new"
    assert current["status"] == "active"


def test_active_lock_blocks(ddb_resource):
    """A lock with ttl in the future and status 'active' must block a new acquire."""
    run_id = "run-active"
    _seed_lock(ddb_resource, run_id, status="active", ttl=int(time.time()) + 3600)

    acquired = dynamodb.acquire_lock(
        run_id=run_id,
        orchestrator_id="orch-new",
        ttl=3600,
        flow_id="flow-new",
        trace_id="trace-new",
        dynamodb_resource=ddb_resource,
    )
    assert acquired is False


def test_completed_lock_is_stealable(ddb_resource):
    """A lock with status='completed' should be stealable regardless of TTL."""
    run_id = "run-done"
    _seed_lock(ddb_resource, run_id, status="completed", ttl=int(time.time()) + 3600)

    acquired = dynamodb.acquire_lock(
        run_id=run_id,
        orchestrator_id="orch-new",
        ttl=3600,
        flow_id="flow-new",
        trace_id="trace-new",
        dynamodb_resource=ddb_resource,
    )
    assert acquired is True
