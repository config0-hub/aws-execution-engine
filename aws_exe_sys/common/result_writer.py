"""ExecutionResult / StepResult dataclasses and S3 result writer."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import re

import boto3

_S3_URI_RE = re.compile(r"^s3://([^/]+)/(.+)$")


@dataclass
class StepResult:
    """Result of a single command execution step."""

    step_name: str
    status: str
    exit_code: int
    duration_seconds: float
    output: str


@dataclass
class ExecutionResult:
    """Aggregate result of an entire execution run."""

    trigger_id: str
    status: str
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Parse 's3://bucket/key' into (bucket, key). Raises ValueError on bad input."""
    m = _S3_URI_RE.match(uri)
    if not m:
        raise ValueError(f"Invalid S3 URI: {uri!r}")
    return m.group(1), m.group(2)


def write_result(
    done_endpoint: str,
    result: ExecutionResult,
    s3_client=None,
) -> str:
    """Write an ExecutionResult as JSON to the S3 done_endpoint.

    Args:
        done_endpoint: S3 URI (s3://bucket/key) where the result is written.
        result:        The ExecutionResult to persist.
        s3_client:     Optional pre-configured boto3 S3 client (for testing).

    Returns:
        The S3 key that was written.
    """
    bucket, key = _parse_s3_uri(done_endpoint)
    if s3_client is None:
        s3_client = boto3.client("s3")
    body = json.dumps(result.to_dict(), indent=2)
    s3_client.put_object(Bucket=bucket, Key=key, Body=body.encode("utf-8"))
    return key
