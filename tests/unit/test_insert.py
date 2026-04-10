"""Unit tests for src/init_job/insert.py."""

import base64
import json

import boto3
import pytest
from moto import mock_aws

from src.common.models import Job, Order, QUEUED, JOB_ORDER_NAME
from src.common import dynamodb
from src.init_job.insert import insert_orders


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")
    monkeypatch.setenv("AWS_EXE_SYS_LOCKS_TABLE", "test-locks")


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


def _make_job(orders=None, **kwargs):
    defaults = {
        "git_repo": "org/repo",
        "git_token_location": "aws:::ssm:/token",
        "username": "testuser",
    }
    defaults.update(kwargs)
    return Job(orders=orders or [], **defaults)


class TestInsertOrders:
    def test_all_orders_inserted(self, ddb_resource):
        job = _make_job(orders=[
            Order(cmds=["echo a"], timeout=300, order_name="order-a"),
            Order(cmds=["echo b"], timeout=600, order_name="order-b"),
        ])
        repackaged = [
            {"order_num": "0001", "order_name": "order-a", "callback_url": "https://cb1"},
            {"order_num": "0002", "order_name": "order-b", "callback_url": "https://cb2"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="user:abc-exec",
            trace_id="abc123",
            repackaged_orders=repackaged,
            internal_bucket="test-bucket",
            dynamodb_resource=ddb_resource,
        )

        # Verify orders
        order1 = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        assert order1 is not None
        assert order1["order_name"] == "order-a"
        assert order1["status"] == QUEUED
        assert order1["trace_id"] == "abc123"
        assert order1["flow_id"] == "user:abc-exec"
        assert order1["callback_url"] == "https://cb1"
        assert order1["timeout"] == 300

        order2 = dynamodb.get_order("run-1", "0002", dynamodb_resource=ddb_resource)
        assert order2 is not None
        assert order2["order_name"] == "order-b"

    def test_ttl_set(self, ddb_resource):
        job = _make_job(orders=[
            Order(cmds=["echo"], timeout=300),
        ])
        repackaged = [
            {"order_num": "0001", "order_name": "order-1", "callback_url": "https://cb"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="flow",
            trace_id="trace",
            repackaged_orders=repackaged,
            internal_bucket="bucket",
            dynamodb_resource=ddb_resource,
        )

        order = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        assert "ttl" in order
        assert order["ttl"] > order["created_at"]

    def test_job_event_written(self, ddb_resource):
        job = _make_job(orders=[
            Order(cmds=["echo"], timeout=300),
        ])
        repackaged = [
            {"order_num": "0001", "order_name": "order-1", "callback_url": "https://cb"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="flow",
            trace_id="trace-1",
            repackaged_orders=repackaged,
            internal_bucket="bucket",
            dynamodb_resource=ddb_resource,
        )

        events = dynamodb.get_events(
            "trace-1",
            order_name_prefix=JOB_ORDER_NAME,
            dynamodb_resource=ddb_resource,
        )
        assert len(events) == 1
        assert events[0]["event_type"] == "job_started"
        assert events[0]["status"] == "running"

    def test_dependencies_stored(self, ddb_resource):
        job = _make_job(orders=[
            Order(cmds=["echo"], timeout=300, queue_id="q1"),
            Order(cmds=["echo"], timeout=300, queue_id="q2", dependencies=["q1"]),
        ])
        repackaged = [
            {"order_num": "0001", "order_name": "order-1", "callback_url": "https://cb"},
            {"order_num": "0002", "order_name": "order-2", "callback_url": "https://cb"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="flow",
            trace_id="trace",
            repackaged_orders=repackaged,
            internal_bucket="bucket",
            dynamodb_resource=ddb_resource,
        )

        order2 = dynamodb.get_order("run-1", "0002", dynamodb_resource=ddb_resource)
        assert order2["dependencies"] == ["q1"]
        assert order2["queue_id"] == "q2"

    def test_git_b64_includes_commit_hash(self, ddb_resource):
        job = _make_job(
            commit_hash="abc123",
            orders=[
                Order(cmds=["echo"], timeout=300, order_name="order-1"),
            ],
        )
        repackaged = [
            {"order_num": "0001", "order_name": "order-1", "callback_url": "https://cb"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="flow",
            trace_id="trace",
            repackaged_orders=repackaged,
            internal_bucket="bucket",
            dynamodb_resource=ddb_resource,
        )

        order = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        git_data = json.loads(base64.b64decode(order["git_b64"]).decode())
        assert git_data["commit_hash"] == "abc123"

    def test_git_b64_excludes_commit_hash_when_none(self, ddb_resource):
        job = _make_job(orders=[
            Order(cmds=["echo"], timeout=300, order_name="order-1"),
        ])
        repackaged = [
            {"order_num": "0001", "order_name": "order-1", "callback_url": "https://cb"},
        ]

        insert_orders(
            job=job,
            run_id="run-1",
            flow_id="flow",
            trace_id="trace",
            repackaged_orders=repackaged,
            internal_bucket="bucket",
            dynamodb_resource=ddb_resource,
        )

        order = dynamodb.get_order("run-1", "0001", dynamodb_resource=ddb_resource)
        git_data = json.loads(base64.b64decode(order["git_b64"]).decode())
        assert "commit_hash" not in git_data
