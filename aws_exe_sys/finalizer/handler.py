"""Create a failed fallback result when CodeBuild did not write one."""

from __future__ import annotations

import json
from typing import Any

import boto3
from botocore.exceptions import ClientError

from aws_exe_sys.common.result_writer import ExecutionResult, _parse_s3_uri

_SUCCEEDED = "codebuild_succeeded_without_result"
_FAILED = "codebuild_failed_without_result"
_TIMED_OUT = "codebuild_timed_out_without_result"
_STOPPED = "codebuild_stopped_without_result"
_TERMINAL_RESULT_MISSING = "codebuild_terminal_result_missing"


def _require_non_empty_string(event: dict[str, Any], field: str) -> str:
    if field not in event:
        raise ValueError(f"{field} is required")

    value = event[field]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _require_terminal_context(event: dict[str, Any]) -> dict[str, Any]:
    if "terminal_context" not in event:
        raise ValueError("terminal_context is required")

    terminal_context = event["terminal_context"]
    if not isinstance(terminal_context, dict):
        raise ValueError("terminal_context must be an object")
    return terminal_context


def _classify_terminal_context(terminal_context: dict[str, Any]) -> str:
    if "outcome" in terminal_context:
        outcome = terminal_context["outcome"]
        if outcome == "succeeded":
            return _SUCCEEDED
        elif outcome != "failed":
            raise ValueError(f"unknown terminal outcome: {outcome!r}")
    elif "Error" in terminal_context and "Cause" in terminal_context:
        pass
    else:
        return _TERMINAL_RESULT_MISSING

    serialized = json.dumps(terminal_context, sort_keys=True, separators=(",", ":")).upper()
    if any(token in serialized for token in ("TIMED_OUT", "TIMED OUT", "TIMEOUT", "STATES.TIMEOUT")):
        return _TIMED_OUT
    elif any(token in serialized for token in ("STOPPED", "ABORTED", "CANCELLED", "CANCELED")):
        return _STOPPED
    elif any(token in serialized for token in ("FAILED", "FAULT")):
        return _FAILED
    else:
        return _TERMINAL_RESULT_MISSING


def _fallback_error(terminal_context: dict[str, Any]) -> str:
    classification = _classify_terminal_context(terminal_context)
    context_json = json.dumps(terminal_context, sort_keys=True, separators=(",", ":"))
    return f"{classification}: terminal_context={context_json}"


def handler(event: dict[str, Any], context: Any = None) -> dict[str, str]:
    """Atomically create a failed result, or preserve the worker's existing result."""
    del context

    trigger_id = _require_non_empty_string(event, "trigger_id")
    done_endpoint = _require_non_empty_string(event, "done_endpoint")
    terminal_context = _require_terminal_context(event)
    bucket, key = _parse_s3_uri(done_endpoint)

    result = ExecutionResult(
        trigger_id=trigger_id,
        status="failed",
        steps=[],
        error=_fallback_error(terminal_context),
    )
    body = json.dumps(result.to_dict(), indent=2).encode("utf-8")
    s3_client = boto3.client("s3")

    try:
        s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="application/json",
            IfNoneMatch="*",
        )
    except ClientError as exc:
        error_code = exc.response["Error"]["Code"]
        if error_code in ("PreconditionFailed", "412"):
            return {"status": "preserved", "trigger_id": trigger_id}
        raise

    return {"status": "created", "trigger_id": trigger_id}
