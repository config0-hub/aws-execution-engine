"""Unit tests for src/watchdog_check/handler.py."""

import json
import time

import boto3
import pytest
from moto import mock_aws

from src.watchdog_check.handler import handler


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "test-internal")


@pytest.fixture
def s3_client(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-internal")
        yield s3


class TestWatchdogCheck:
    def test_result_exists_returns_done(self, s3_client):
        # Write result.json
        s3_client.put_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
            Body=json.dumps({"status": "succeeded"}).encode(),
        )

        result = handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 300,
            "start_time": int(time.time()),
            "internal_bucket": "test-internal",
        })

        assert result["done"] is True

    def test_no_result_not_timed_out(self, s3_client):
        result = handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 300,
            "start_time": int(time.time()),
            "internal_bucket": "test-internal",
        })

        assert result["done"] is False

    def test_no_result_timed_out(self, s3_client):
        result = handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 300,
            "start_time": int(time.time()) - 400,  # well past timeout
            "internal_bucket": "test-internal",
        })

        assert result["done"] is True

    def test_timed_out_result_content(self, s3_client):
        handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 10,
            "start_time": int(time.time()) - 100,
            "internal_bucket": "test-internal",
        })

        # Verify the written result.json
        resp = s3_client.get_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
        )
        body = json.loads(resp["Body"].read())
        assert body["status"] == "timed_out"
        assert "watchdog" in body["log"].lower()
