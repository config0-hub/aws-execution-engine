"""Integration test: full init_job flow with mocked AWS."""

import base64
import json
import os
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from src.common.models import Job, Order, QUEUED, JOB_ORDER_NAME
from src.init_job.handler import handler, process_job_and_insert_orders


# ── Fixtures ──────────────────────────────────────────────────────


def _make_job(
    orders=None,
    pr_number=None,
    s3_location="s3://source-bucket/code.zip",
):
    """Build a Job with 2 orders (no deps) by default."""
    if orders is None:
        orders = [
            Order(
                cmds=["echo order-1"],
                timeout=300,
                order_name="order-1",
                s3_location=s3_location,
                execution_target="lambda",
            ),
            Order(
                cmds=["echo order-2"],
                timeout=300,
                order_name="order-2",
                s3_location=s3_location,
                execution_target="lambda",
            ),
        ]
    return Job(
        git_repo="org/repo",
        git_token_location="aws:::ssm:/token",
        username="testuser",
        orders=orders,
        pr_number=pr_number,
    )


@pytest.fixture
def aws_env(monkeypatch):
    """Set up environment variables for mocked AWS."""
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


@pytest.fixture
def mock_aws_resources(aws_env):
    """Create mocked DynamoDB tables and S3 buckets."""
    with mock_aws():
        region = "us-east-1"

        # DynamoDB tables
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

        # S3 buckets
        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(Bucket="test-internal")
        s3.create_bucket(Bucket="test-done")
        s3.create_bucket(Bucket="source-bucket")

        # Upload a dummy code.zip to source bucket
        import zipfile, tempfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.sh", "echo hello")
        buf.seek(0)
        s3.put_object(Bucket="source-bucket", Key="code.zip", Body=buf.read())

        yield {"ddb": ddb, "s3": s3}


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.integration
class TestInitJobFlow:

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.common.sops.repackage_order")
    @patch("src.init_job.pr_comment.VcsHelper")
    def test_full_init_job_creates_orders_and_trigger(
        self, mock_vcs_cls, mock_sops, mock_resolve_creds,
        mock_gen_key, mock_store_ssm, mock_aws_resources,
    ):
        """End-to-end init_job: 2 orders, no PR, direct invoke."""
        # Mock SOPS to be a no-op (just return code_dir)
        mock_sops.side_effect = lambda code_dir, env, sops_key=None: code_dir

        job = _make_job()
        event = {"job_parameters_b64": job.to_b64()}
        result = handler(event)

        assert result["status"] == "ok"
        assert "run_id" in result
        assert "trace_id" in result
        assert "flow_id" in result
        assert "done_endpt" in result

        run_id = result["run_id"]

        # Verify orders in DynamoDB
        ddb = mock_aws_resources["ddb"]
        orders_table = ddb.Table("test-orders")

        order1 = orders_table.get_item(Key={"pk": f"{run_id}:0001"}).get("Item")
        assert order1 is not None
        assert order1["status"] == QUEUED
        assert order1["order_name"] == "order-1"

        order2 = orders_table.get_item(Key={"pk": f"{run_id}:0002"}).get("Item")
        assert order2 is not None
        assert order2["status"] == QUEUED
        assert order2["order_name"] == "order-2"

        # Verify exec.zip uploaded to S3 for each order
        s3 = mock_aws_resources["s3"]
        for num in ["0001", "0002"]:
            resp = s3.head_object(
                Bucket="test-internal",
                Key=f"tmp/exec/{run_id}/{num}/exec.zip",
            )
            assert resp["ResponseMetadata"]["HTTPStatusCode"] == 200

        # Verify init trigger written
        resp = s3.get_object(
            Bucket="test-internal",
            Key=f"tmp/callbacks/runs/{run_id}/0000/result.json",
        )
        trigger = json.loads(resp["Body"].read())
        assert trigger["status"] == "init"

        # Verify job-level _job event written
        events_table = ddb.Table("test-order-events")
        events = events_table.scan()["Items"]
        job_events = [e for e in events if e["order_name"] == JOB_ORDER_NAME]
        assert len(job_events) >= 1
        assert job_events[0]["event_type"] == "job_started"

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.common.sops.repackage_order")
    @patch("src.init_job.pr_comment.VcsHelper")
    def test_init_job_pr_comment_disabled(
        self, mock_vcs_cls, mock_sops, mock_resolve_creds,
        mock_gen_key, mock_store_ssm, mock_aws_resources,
    ):
        """PR comments are disabled (AC-5); init_pr_comment should be None."""
        mock_sops.side_effect = lambda code_dir, env, sops_key=None: code_dir

        job = _make_job(pr_number=10)
        event = {"job_parameters_b64": job.to_b64()}
        result = handler(event)

        assert result["status"] == "ok"
        assert result["init_pr_comment"] is None

        # VCS should never be instantiated (PR comments disabled)
        mock_vcs_cls.assert_not_called()

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.common.sops.repackage_order")
    @patch("src.init_job.pr_comment.VcsHelper")
    def test_init_job_response_fields(
        self, mock_vcs_cls, mock_sops, mock_resolve_creds,
        mock_gen_key, mock_store_ssm, mock_aws_resources,
    ):
        """Verify all expected response fields are present."""
        mock_sops.side_effect = lambda code_dir, env, sops_key=None: code_dir

        job = _make_job()
        result = process_job_and_insert_orders(job.to_b64())

        assert result["status"] == "ok"
        assert result["run_id"]
        assert result["trace_id"]
        assert result["flow_id"]
        assert result["done_endpt"].startswith("s3://test-done/")
        assert result["pr_search_tag"]

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.common.sops.repackage_order")
    @patch("src.init_job.pr_comment.VcsHelper")
    def test_init_job_via_apigw(
        self, mock_vcs_cls, mock_sops, mock_resolve_creds,
        mock_gen_key, mock_store_ssm, mock_aws_resources,
    ):
        """Verify API Gateway invocation returns proper response format."""
        mock_sops.side_effect = lambda code_dir, env, sops_key=None: code_dir

        job = _make_job()
        event = {
            "httpMethod": "POST",
            "body": json.dumps({"job_parameters_b64": job.to_b64()}),
        }
        result = handler(event)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ok"
        assert "run_id" in body
