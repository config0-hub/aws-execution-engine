"""Dispatch a validated SimplePayload to the appropriate execution target."""

import json
import logging
import os
import uuid

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
    """Start the Standard workflow that owns the CodeBuild lifecycle."""
    state_machine_arn = os.environ["AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN"]
    client = boto3.client("stepfunctions")
    execution_name = f"aws-exe-{uuid.uuid4().hex}"

    response = client.start_execution(
        stateMachineArn=state_machine_arn,
        name=execution_name,
        input=json.dumps(_payload_to_dict(payload)),
    )
    logger.info(
        "Dispatched CodeBuild workflow",
        extra={"state_machine": state_machine_arn, "trigger_id": payload.trigger_id},
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
