"""Unit tests for src/orchestrator/lock.py."""

import boto3
import pytest
from moto import mock_aws

from src.orchestrator.lock import acquire_lock, release_lock


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


class TestAcquireLock:
    def test_acquires_first_time(self, ddb_resource):
        result = acquire_lock(
            "run-1", "flow-1", "trace-1",
            dynamodb_resource=ddb_resource,
        )
        assert result is True

    def test_fails_when_active(self, ddb_resource):
        acquire_lock("run-1", "flow-1", "trace-1", dynamodb_resource=ddb_resource)
        result = acquire_lock("run-1", "flow-2", "trace-2", dynamodb_resource=ddb_resource)
        assert result is False

    def test_succeeds_after_release(self, ddb_resource):
        acquire_lock("run-1", "flow-1", "trace-1", dynamodb_resource=ddb_resource)
        release_lock("run-1", dynamodb_resource=ddb_resource)

        result = acquire_lock("run-1", "flow-2", "trace-2", dynamodb_resource=ddb_resource)
        assert result is True
