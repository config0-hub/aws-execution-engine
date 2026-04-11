"""Tests for aws_exe_sys/ssm_config/insert.py::insert_ssm_orders.

Ensures SSM orders written to DynamoDB do NOT include env_dict. Env vars are
baked into the SOPS-encrypted zip bundle at repackage time; persisting them
in a second place exposes secrets unnecessarily and invites drift.
"""

import boto3
import pytest
from moto import mock_aws

from aws_exe_sys.common.models import SsmJob, SsmOrder
from aws_exe_sys.ssm_config.insert import insert_ssm_orders


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDERS_TABLE", "test-orders")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-events")


@pytest.fixture
def ddb_resource(aws_env):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
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
                }
            ],
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


def test_env_dict_not_persisted(ddb_resource):
    """insert_ssm_orders must not write env_dict to the orders table.

    Secrets flow via the zip bundle only; storing them on the DynamoDB row
    creates a second copy of live credentials and makes the table a hotter
    target than it needs to be.
    """
    job = SsmJob(
        orders=[
            SsmOrder(
                cmds=["echo hi"],
                timeout=300,
                order_name="order-1",
                ssm_targets={"instance_ids": ["i-abc"]},
            )
        ],
        git_repo="org/repo",
        git_token_location="aws:::ssm:/token",
        username="testuser",
    )
    repackaged = [
        {
            "order_num": "000",
            "order_name": "order-1",
            "callback_url": "https://callback.test/ok",
            "env_dict": {"DB_PASS": "super-secret", "APP_ENV": "prod"},
        }
    ]

    insert_ssm_orders(
        job=job,
        run_id="run-1",
        flow_id="flow-1",
        trace_id="trace-1",
        repackaged_orders=repackaged,
        internal_bucket="test-bucket",
        dynamodb_resource=ddb_resource,
    )

    table = ddb_resource.Table("test-orders")
    item = table.get_item(Key={"pk": "run-1:000"}).get("Item")
    assert item is not None
    assert "env_dict" not in item, (
        f"env_dict must not be persisted to the orders table; found: {item.get('env_dict')!r}"
    )
