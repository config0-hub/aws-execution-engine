"""Unit tests for the CodeBuild terminal-result finalizer."""

import json
from unittest.mock import MagicMock, patch

import boto3
from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from aws_exe_sys.finalizer import handler as finalizer_handler


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
        client.create_bucket(Bucket="done-bucket")
        yield client


def _event(terminal_context: dict | None = None, **overrides) -> dict:
    if terminal_context is None:
        terminal_context = {"outcome": "succeeded"}
    event = {
        "trigger_id": "trg-001",
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "terminal_context": terminal_context,
    }
    event.update(overrides)
    return event


def _read_result(s3_client) -> dict:
    response = s3_client.get_object(Bucket="done-bucket", Key="trg-001/result.json")
    return json.loads(response["Body"].read().decode("utf-8"))


def test_missing_marker_creates_failed_execution_result(s3_client):
    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        response = finalizer_handler.handler(_event({"Error": "CodeBuild.BuildFailed", "Cause": "build FAILED"}))

    assert response == {"status": "created", "trigger_id": "trg-001"}
    result = _read_result(s3_client)
    assert result["trigger_id"] == "trg-001"
    assert result["status"] == "failed"
    assert result["steps"] == []
    assert result["error"].startswith("codebuild_failed_without_result:")


def test_existing_worker_marker_is_preserved(s3_client):
    worker_result = b'{"trigger_id":"trg-001","status":"succeeded","steps":[]}'
    s3_client.put_object(Bucket="done-bucket", Key="trg-001/result.json", Body=worker_result)

    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        response = finalizer_handler.handler(_event())

    assert response == {"status": "preserved", "trigger_id": "trg-001"}
    stored = s3_client.get_object(Bucket="done-bucket", Key="trg-001/result.json")["Body"].read()
    assert stored == worker_result


def test_success_without_marker_is_failed_contract_outcome(s3_client):
    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        finalizer_handler.handler(_event({"outcome": "succeeded"}))

    assert _read_result(s3_client)["error"].startswith("codebuild_succeeded_without_result:")


@pytest.mark.parametrize(
    ("terminal_context", "classification"),
    [
        ({"Error": "CodeBuild.BuildFailed", "Cause": "BuildStatus=FAILED"}, "codebuild_failed_without_result"),
        ({"Error": "CodeBuild.BuildFailed", "Cause": "BuildStatus=STOPPED"}, "codebuild_stopped_without_result"),
        ({"Error": "CodeBuild.BuildFailed", "Cause": "BuildStatus=TIMED_OUT"}, "codebuild_timed_out_without_result"),
        ({"Error": "UnknownTerminalError", "Cause": "no build status"}, "codebuild_terminal_result_missing"),
    ],
)
def test_terminal_context_has_stable_classification(s3_client, terminal_context, classification):
    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        finalizer_handler.handler(_event(terminal_context))

    assert _read_result(s3_client)["error"].startswith(f"{classification}:")


@pytest.mark.parametrize(
    "event",
    [
        _event(done_endpoint=""),
        _event(done_endpoint="not-an-s3-uri"),
        {"trigger_id": "trg-001", "terminal_context": {"outcome": "succeeded"}},
    ],
)
def test_invalid_or_missing_done_endpoint_fails_loudly(event):
    with pytest.raises(ValueError, match="done_endpoint|Invalid S3 URI"):
        finalizer_handler.handler(event)


def test_non_precondition_s3_error_propagates():
    s3_client = MagicMock()
    s3_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}},
        "PutObject",
    )

    with (
        patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client),
        pytest.raises(ClientError),
    ):
        finalizer_handler.handler(_event())


def test_precondition_failure_is_success():
    s3_client = MagicMock()
    s3_client.put_object.side_effect = ClientError(
        {"Error": {"Code": "PreconditionFailed", "Message": "exists"}},
        "PutObject",
    )

    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        response = finalizer_handler.handler(_event())

    assert response["status"] == "preserved"


def test_duplicate_invocation_is_idempotent(s3_client):
    with patch("aws_exe_sys.finalizer.handler.boto3.client", return_value=s3_client):
        first = finalizer_handler.handler(_event({"Error": "CodeBuild.BuildFailed", "Cause": "FAILED"}))
        first_body = _read_result(s3_client)
        second = finalizer_handler.handler(_event({"outcome": "succeeded"}))
        second_body = _read_result(s3_client)

    assert first["status"] == "created"
    assert second["status"] == "preserved"
    assert second_body == first_body
