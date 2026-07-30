"""Golden-vector wire-compatibility anchor for the canonical ExecutionResult.

The engine writes an ExecutionResult to the S3 done_endpoint and callers read
the same bytes. This test pins the exact done-marker JSON shape so producers
and consumers cannot drift the wire format silently.
"""

import json
from pathlib import Path

import boto3
from moto import mock_aws
import pytest

from aws_exe_sys.common.result_writer import ExecutionResult, StepResult, write_result

GOLDEN_PATH = Path(__file__).resolve().parents[1] / "testdata" / "execution_result_done_marker.json"

DONE_BUCKET = "xe-done"
DONE_KEY = "executions/trg-golden-001/result.json"
DONE_ENDPOINT = f"s3://{DONE_BUCKET}/{DONE_KEY}"


def _golden_result() -> ExecutionResult:
    """The ExecutionResult that produces the committed golden bytes."""
    return ExecutionResult(
        trigger_id="trg-golden-001",
        status="succeeded",
        steps=[
            StepResult(
                step_name="step-0",
                status="succeeded",
                exit_code=0,
                duration_seconds=1.23,
                output="hello world\n",
            )
        ],
    )


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def s3_client(aws_env):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=DONE_BUCKET)
        yield client


def test_golden_file_matches_serialized_result():
    """The committed golden file IS the exact bytes write_result produces."""
    expected = json.dumps(_golden_result().to_dict(), indent=2)
    assert GOLDEN_PATH.read_text() == expected


def test_round_trip_write_read_back(s3_client):
    """Write an ExecutionResult, read it back from S3, assert it equals golden."""
    write_result(DONE_ENDPOINT, _golden_result(), s3_client=s3_client)

    obj = s3_client.get_object(Bucket=DONE_BUCKET, Key=DONE_KEY)
    written_text = obj["Body"].read().decode("utf-8")

    # Byte-for-byte identical to the committed golden anchor.
    assert written_text == GOLDEN_PATH.read_text()

    # And the parsed field values match the source result.
    parsed = json.loads(written_text)
    golden = json.loads(GOLDEN_PATH.read_text())
    assert parsed == golden
    assert parsed["trigger_id"] == "trg-golden-001"
    assert parsed["status"] == "succeeded"
    assert parsed["steps"][0]["step_name"] == "step-0"
    assert parsed["steps"][0]["exit_code"] == 0
    assert parsed["steps"][0]["duration_seconds"] == 1.23
    assert parsed["steps"][0]["output"] == "hello world\n"
    # None error is excluded from the canonical shape.
    assert "error" not in parsed
