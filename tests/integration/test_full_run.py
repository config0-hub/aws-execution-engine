"""Integration test: full end-to-end run (init_job → orchestrator → finalize)."""

import json
import time
from unittest.mock import patch, MagicMock

import boto3
import pytest
from moto import mock_aws

from src.common.models import Job, Order, QUEUED, RUNNING, SUCCEEDED, JOB_ORDER_NAME
from src.init_job.handler import handler as init_handler
from src.orchestrator.handler import handler as orch_handler


# ── Fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def aws_env(monkeypatch):
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
    monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "aws-exe-sys-worker")
    monkeypatch.setenv("AWS_EXE_SYS_WATCHDOG_SFN", "arn:aws:states:us-east-1:123456:stateMachine:aws-exe-sys-watchdog")


@pytest.fixture
def mock_aws_resources(aws_env):
    with mock_aws():
        region = "us-east-1"

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

        s3 = boto3.client("s3", region_name=region)
        s3.create_bucket(Bucket="test-internal")
        s3.create_bucket(Bucket="test-done")
        s3.create_bucket(Bucket="source-bucket")

        # Upload dummy code.zip
        import zipfile, io
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.sh", "echo hello")
        buf.seek(0)
        s3.put_object(Bucket="source-bucket", Key="code.zip", Body=buf.read())

        yield {"ddb": ddb, "s3": s3}


def _s3_event(run_id: str, order_num: str) -> dict:
    return {
        "Records": [{
            "s3": {
                "bucket": {"name": "test-internal"},
                "object": {
                    "key": f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
                },
            }
        }]
    }


def _write_result(s3, run_id, order_num, status="succeeded", log="done"):
    s3.put_object(
        Bucket="test-internal",
        Key=f"tmp/callbacks/runs/{run_id}/{order_num}/result.json",
        Body=json.dumps({"status": status, "log": log}),
    )


# ── Tests ──────────────────────────────────────────────────────────


@pytest.mark.integration
class TestFullRun:

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.orchestrator.dispatch._start_watchdog", return_value="arn:watchdog:exec")
    @patch("src.orchestrator.dispatch._dispatch_lambda", return_value="req-123")
    @patch("src.common.sops.repackage_order")
    @patch("src.init_job.pr_comment.VcsHelper")
    def test_three_order_dependency_chain(
        self, mock_vcs_cls, mock_sops, mock_dispatch, mock_watchdog,
        mock_resolve_creds, mock_gen_key, mock_store_ssm,
        mock_aws_resources,
    ):
        """Full run: submit 3 orders, simulate completions, verify finalization.

        order-1 (no deps) → order-2 (no deps) → order-3 (depends on 1+2)
        """
        mock_sops.side_effect = lambda code_dir, env, sops_key=None: code_dir
        ddb = mock_aws_resources["ddb"]
        s3 = mock_aws_resources["s3"]

        # Step 1: Submit job via init_job
        job = Job(
            git_repo="org/repo",
            git_token_location="aws:::ssm:/token",
            username="testuser",
            orders=[
                Order(
                    cmds=["echo 1"], timeout=300, order_name="order-1",
                    s3_location="s3://source-bucket/code.zip", execution_target="lambda",
                ),
                Order(
                    cmds=["echo 2"], timeout=300, order_name="order-2",
                    s3_location="s3://source-bucket/code.zip", execution_target="lambda",
                ),
                Order(
                    cmds=["echo 3"], timeout=300, order_name="order-3",
                    s3_location="s3://source-bucket/code.zip", execution_target="lambda",
                    dependencies=["0001", "0002"],
                ),
            ],
        )

        init_result = init_handler({"job_parameters_b64": job.to_b64()})
        assert init_result["status"] == "ok"
        run_id = init_result["run_id"]
        trace_id = init_result["trace_id"]

        # Verify orders are in DynamoDB as queued
        orders_table = ddb.Table("test-orders")
        for num in ["0001", "0002", "0003"]:
            item = orders_table.get_item(Key={"pk": f"{run_id}:{num}"})["Item"]
            assert item["status"] == QUEUED

        # Verify init trigger written
        trigger_key = f"tmp/callbacks/runs/{run_id}/0000/result.json"
        resp = s3.get_object(Bucket="test-internal", Key=trigger_key)
        assert json.loads(resp["Body"].read())["status"] == "init"

        # Step 2: Invoke orchestrator with init trigger
        orch_result = orch_handler(_s3_event(run_id, "0000"))
        assert orch_result["status"] == "in_progress"

        # order-1 and order-2 should be dispatched (running)
        o1 = orders_table.get_item(Key={"pk": f"{run_id}:0001"})["Item"]
        o2 = orders_table.get_item(Key={"pk": f"{run_id}:0002"})["Item"]
        assert o1["status"] == RUNNING
        assert o2["status"] == RUNNING

        # order-3 still queued
        o3 = orders_table.get_item(Key={"pk": f"{run_id}:0003"})["Item"]
        assert o3["status"] == QUEUED

        # Step 3: Simulate order-1 completion
        _write_result(s3, run_id, "0001", "succeeded")
        orch_result = orch_handler(_s3_event(run_id, "0001"))

        o3 = orders_table.get_item(Key={"pk": f"{run_id}:0003"})["Item"]
        assert o3["status"] == QUEUED  # still waiting for order-2

        # Step 4: Simulate order-2 completion
        _write_result(s3, run_id, "0002", "succeeded")
        orch_result = orch_handler(_s3_event(run_id, "0002"))

        o3 = orders_table.get_item(Key={"pk": f"{run_id}:0003"})["Item"]
        assert o3["status"] == RUNNING  # now dispatched

        # Step 5: Simulate order-3 completion
        _write_result(s3, run_id, "0003", "succeeded")
        orch_result = orch_handler(_s3_event(run_id, "0003"))
        assert orch_result["status"] == "finalized"

        # Verify all orders succeeded
        for num in ["0001", "0002", "0003"]:
            item = orders_table.get_item(Key={"pk": f"{run_id}:{num}"})["Item"]
            assert item["status"] == SUCCEEDED

        # Verify job-level completion event
        events_table = ddb.Table("test-order-events")
        all_events = events_table.scan()["Items"]
        job_completed = [
            e for e in all_events
            if e.get("order_name") == JOB_ORDER_NAME and e.get("event_type") == "job_completed"
        ]
        assert len(job_completed) == 1
        assert job_completed[0]["status"] == SUCCEEDED

        # Verify done endpoint written
        done_resp = s3.get_object(Bucket="test-done", Key=f"{run_id}/done")
        done_data = json.loads(done_resp["Body"].read())
        assert done_data["status"] == SUCCEEDED
        assert done_data["summary"][SUCCEEDED] == 3

        # Verify lock released
        lock = ddb.Table("test-locks").get_item(
            Key={"pk": f"lock:{run_id}"}
        )["Item"]
        assert lock["status"] == "completed"
