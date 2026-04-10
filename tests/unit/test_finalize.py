"""Unit tests for src/orchestrator/finalize.py."""

import json

import boto3
import pytest
from moto import mock_aws

from src.common import dynamodb
from src.common.models import SUCCEEDED, FAILED, TIMED_OUT, JOB_ORDER_NAME
from src.orchestrator.finalize import check_and_finalize


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")
    monkeypatch.setenv("AWS_EXE_SYS_DONE_BUCKET", "test-done")


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for table_name, schema in [
            ("test-orders", [{"AttributeName": "pk", "KeyType": "HASH"}]),
            ("test-locks", [{"AttributeName": "pk", "KeyType": "HASH"}]),
        ]:
            ddb.create_table(
                TableName=table_name,
                KeySchema=schema,
                AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        ddb.create_table(
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

        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-done")

        # Acquire lock so finalize can release it
        dynamodb.acquire_lock("run-1", "orch-1", 3600, "flow-1", "trace-1",
                              dynamodb_resource=ddb)

        yield {"ddb": ddb, "s3": s3}


class TestCheckAndFinalize:
    def test_all_succeeded(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        orders = [
            {"order_num": "0001", "status": SUCCEEDED, "must_succeed": True},
            {"order_num": "0002", "status": SUCCEEDED, "must_succeed": True},
        ]

        result = check_and_finalize(
            orders, "run-1", "flow-1", "trace-1",
            done_bucket="test-done",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        assert result is True

        # Verify done endpoint written
        resp = s3.get_object(Bucket="test-done", Key="run-1/done")
        body = json.loads(resp["Body"].read())
        assert body["status"] == SUCCEEDED
        assert body["summary"][SUCCEEDED] == 2

    def test_must_succeed_failure(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        orders = [
            {"order_num": "0001", "status": SUCCEEDED, "must_succeed": True},
            {"order_num": "0002", "status": FAILED, "must_succeed": True},
        ]

        result = check_and_finalize(
            orders, "run-1", "flow-1", "trace-1",
            done_bucket="test-done",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        assert result is True
        resp = s3.get_object(Bucket="test-done", Key="run-1/done")
        body = json.loads(resp["Body"].read())
        assert body["status"] == FAILED

    def test_timed_out(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        orders = [
            {"order_num": "0001", "status": SUCCEEDED, "must_succeed": True},
            {"order_num": "0002", "status": TIMED_OUT, "must_succeed": True},
        ]

        result = check_and_finalize(
            orders, "run-1", "flow-1", "trace-1",
            done_bucket="test-done",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        assert result is True
        resp = s3.get_object(Bucket="test-done", Key="run-1/done")
        body = json.loads(resp["Body"].read())
        assert body["status"] == TIMED_OUT

    def test_not_all_done_releases_lock(self, aws_resources):
        ddb = aws_resources["ddb"]

        orders = [
            {"order_num": "0001", "status": SUCCEEDED, "must_succeed": True},
            {"order_num": "0002", "status": "running", "must_succeed": True},
        ]

        result = check_and_finalize(
            orders, "run-1", "flow-1", "trace-1",
            done_bucket="test-done",
            dynamodb_resource=ddb,
        )

        assert result is False

        # Lock should be released (status=completed)
        lock = dynamodb.get_lock("run-1", dynamodb_resource=ddb)
        assert lock["status"] == "completed"

    def test_job_event_written(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        orders = [
            {"order_num": "0001", "status": SUCCEEDED, "must_succeed": True},
        ]

        check_and_finalize(
            orders, "run-1", "flow-1", "trace-1",
            done_bucket="test-done",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        events = dynamodb.get_events(
            "trace-1",
            order_name_prefix=JOB_ORDER_NAME,
            dynamodb_resource=ddb,
        )
        assert len(events) == 1
        assert events[0]["event_type"] == "job_completed"
        assert events[0]["status"] == SUCCEEDED
