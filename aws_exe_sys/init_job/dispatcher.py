"""Dispatch a validated SimplePayload to the appropriate execution target."""

import json
import logging
import os

import boto3

from aws_exe_sys.common.payload import SimplePayload

logger = logging.getLogger("init_job.dispatcher")

_PAYLOAD_FIELDS = (
    "trigger_id",
    "s3_package_uri",
    "sops_type",
    "sops_path",
    "commands_b64",
    "done_endpoint",
    "execution_target",
)


def _payload_to_dict(payload: SimplePayload) -> dict[str, str]:
    """Convert payload to a flat dict of string values for dispatch."""
    return {field: str(getattr(payload, field) or "") for field in _PAYLOAD_FIELDS}


def dispatch_to_lambda(payload: SimplePayload) -> dict:
    """Invoke the worker Lambda with all 7 payload fields."""
    function_name = os.environ["AWS_EXE_SYS_WORKER_LAMBDA"]
    client = boto3.client("lambda")

    response = client.invoke(
        FunctionName=function_name,
        InvocationType="Event",
        Payload=json.dumps(_payload_to_dict(payload)).encode(),
    )
    logger.info(
        "Dispatched to Lambda",
        extra={"function": function_name, "trigger_id": payload.trigger_id},
    )
    return response


def dispatch_to_codebuild(payload: SimplePayload) -> dict:
    """Start a CodeBuild build with all 7 payload fields as env vars."""
    project_name = os.environ["AWS_EXE_SYS_CODEBUILD_PROJECT"]
    client = boto3.client("codebuild")

    env_overrides = [
        {"name": field.upper(), "value": value, "type": "PLAINTEXT"}
        for field, value in _payload_to_dict(payload).items()
    ]

    response = client.start_build(
        projectName=project_name,
        environmentVariablesOverride=env_overrides,
    )
    logger.info(
        "Dispatched to CodeBuild",
        extra={"project": project_name, "trigger_id": payload.trigger_id},
    )
    return response


_DISPATCHERS = {
    "lambda": dispatch_to_lambda,
    "codebuild": dispatch_to_codebuild,
}


def dispatch(payload: SimplePayload) -> dict:
    """Route payload to the correct execution target."""
    dispatcher = _DISPATCHERS.get(payload.execution_target)
    if dispatcher is None:
        raise ValueError(f"Unknown execution_target: {payload.execution_target!r}")
    return dispatcher(payload)
