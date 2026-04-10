"""Unit tests for src/orchestrator/read_state.py."""

import json

import boto3
import pytest
from moto import mock_aws

from src.common import dynamodb
from src.common.models import RUNNING, SUCCEEDED, QUEUED
from src.orchestrator.read_state import read_state


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")
    monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "test-internal")


@pytest.fixture
def aws_resources(aws_env):
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
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
        s3.create_bucket(Bucket="test-internal")

        yield {"ddb": ddb, "s3": s3}


class TestReadState:
    def test_reads_orders(self, aws_resources):
        ddb = aws_resources["ddb"]
        dynamodb.put_order("run-1", "0001", {
            "order_name": "deploy-vpc",
            "status": QUEUED,
            "trace_id": "abc",
        }, dynamodb_resource=ddb)

        orders = read_state(
            "run-1", trace_id="abc",
            internal_bucket="test-internal",
            dynamodb_resource=ddb,
        )
        assert len(orders) == 1
        assert orders[0]["order_name"] == "deploy-vpc"

    def test_detects_new_result(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        dynamodb.put_order("run-1", "0001", {
            "order_name": "deploy-vpc",
            "status": RUNNING,
            "trace_id": "abc",
            "order_num": "0001",
        }, dynamodb_resource=ddb)

        # Write a result.json to S3
        s3.put_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
            Body=json.dumps({"status": "succeeded", "log": "done"}).encode(),
        )

        orders = read_state(
            "run-1", trace_id="abc",
            internal_bucket="test-internal",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        assert len(orders) == 1
        assert orders[0]["status"] == SUCCEEDED

    def test_updates_order_in_dynamodb(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        dynamodb.put_order("run-1", "0001", {
            "order_name": "deploy-vpc",
            "status": RUNNING,
            "trace_id": "abc",
            "order_num": "0001",
        }, dynamodb_resource=ddb)

        s3.put_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
            Body=json.dumps({"status": "succeeded", "log": "ok"}).encode(),
        )

        read_state(
            "run-1", trace_id="abc",
            internal_bucket="test-internal",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        # Verify DynamoDB was updated
        order = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb)
        assert order["status"] == SUCCEEDED

    def test_writes_order_event(self, aws_resources):
        ddb = aws_resources["ddb"]
        s3 = aws_resources["s3"]

        dynamodb.put_order("run-1", "0001", {
            "order_name": "deploy-vpc",
            "status": RUNNING,
            "trace_id": "abc",
            "order_num": "0001",
        }, dynamodb_resource=ddb)

        s3.put_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
            Body=json.dumps({"status": "succeeded", "log": "ok"}).encode(),
        )

        read_state(
            "run-1", trace_id="abc",
            internal_bucket="test-internal",
            dynamodb_resource=ddb,
            s3_client=s3,
        )

        events = dynamodb.get_events("abc", dynamodb_resource=ddb)
        assert len(events) == 1
        assert events[0]["event_type"] == "completed"

    def test_ignores_queued_orders(self, aws_resources):
        """Queued orders should not have S3 checked."""
        ddb = aws_resources["ddb"]

        dynamodb.put_order("run-1", "0001", {
            "order_name": "deploy-vpc",
            "status": QUEUED,
            "trace_id": "abc",
        }, dynamodb_resource=ddb)

        orders = read_state(
            "run-1", trace_id="abc",
            internal_bucket="test-internal",
            dynamodb_resource=ddb,
        )

        assert len(orders) == 1
        assert orders[0]["status"] == QUEUED
