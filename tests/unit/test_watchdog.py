"""Unit tests for aws_exe_sys/watchdog_check/handler.py."""

import json
import time
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from aws_exe_sys.watchdog_check.handler import handler


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
        # Must land in the natural-timeout branch, not the hard cap:
        # elapsed 70 > timeout 60, but elapsed 70 < 2*timeout 120.
        handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 60,
            "start_time": int(time.time()) - 70,
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


class TestWatchdogJitterAndHardCap:
    """P3-2: jittered wait intervals and absolute hard cap backstop."""

    def test_jitter_within_range(self, s3_client):
        """Every still-waiting return yields wait_seconds in [50, 70]."""
        event = {
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 600,
            "start_time": int(time.time()),  # elapsed ~= 0
            "internal_bucket": "test-internal",
        }
        for _ in range(100):
            result = handler(event)
            assert result["done"] is False
            assert 50 <= result["wait_seconds"] <= 70

    def test_jitter_distribution_not_constant(self, s3_client):
        """100 still-waiting calls must produce >1 distinct wait_seconds
        value — catches any hardcoded-constant regression.
        """
        event = {
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": 600,
            "start_time": int(time.time()),
            "internal_bucket": "test-internal",
        }
        values = {handler(event)["wait_seconds"] for _ in range(100)}
        assert len(values) > 1, (
            f"expected jitter across calls, got constant: {values}"
        )

    def test_hard_cap_writes_distinct_status(self, s3_client):
        """elapsed > 2*timeout writes status='timed_out_watchdog_cap'."""
        timeout = 300
        result = handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": timeout,
            "start_time": int(time.time()) - (2 * timeout + 10),
            "internal_bucket": "test-internal",
        })

        assert result["done"] is True
        assert result["wait_seconds"] == 0

        resp = s3_client.get_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
        )
        body = json.loads(resp["Body"].read())
        assert body["status"] == "timed_out_watchdog_cap"
        assert "hard cap" in body["log"].lower()

    def test_hard_cap_returns_done_even_on_s3_failure(self, s3_client):
        """Backstop invariant: hard cap MUST return done=True even if
        the S3 write itself fails. Dropping the result is strictly
        better than looping forever against a broken S3.
        """
        timeout = 300
        err = ClientError(
            {"Error": {"Code": "ServiceUnavailable", "Message": "boom"}},
            "PutObject",
        )
        with patch(
            "aws_exe_sys.watchdog_check.handler.s3_ops.write_result",
            side_effect=err,
        ):
            result = handler({
                "run_id": "run-1",
                "order_num": "0001",
                "timeout": timeout,
                "start_time": int(time.time()) - (2 * timeout + 10),
                "internal_bucket": "test-internal",
            })

        assert result["done"] is True
        assert result["wait_seconds"] == 0

    def test_natural_timeout_still_uses_original_status(self, s3_client):
        """timeout < elapsed < 2*timeout must still produce status='timed_out'
        (not 'timed_out_watchdog_cap') so postmortems can distinguish.
        """
        timeout = 300
        # elapsed 400 > timeout 300 but < 2*timeout 600
        result = handler({
            "run_id": "run-1",
            "order_num": "0001",
            "timeout": timeout,
            "start_time": int(time.time()) - 400,
            "internal_bucket": "test-internal",
        })

        assert result["done"] is True
        assert result["wait_seconds"] == 0

        resp = s3_client.get_object(
            Bucket="test-internal",
            Key="tmp/callbacks/runs/run-1/0001/result.json",
        )
        body = json.loads(resp["Body"].read())
        assert body["status"] == "timed_out"
        assert body["status"] != "timed_out_watchdog_cap"
