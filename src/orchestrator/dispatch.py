"""Dispatch ready orders to Lambda, CodeBuild, or SSM."""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import Dict, List

import boto3


def _json_default(obj):
    """JSON encoder default for DynamoDB Decimal types."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

from src.common import dynamodb
from src.common.models import RUNNING

logger = logging.getLogger(__name__)


def _dispatch_lambda(order: dict, run_id: str, internal_bucket: str) -> str:
    """Invoke the worker Lambda for an order. Returns execution ARN/request ID."""
    lambda_client = boto3.client("lambda")
    function_name = os.environ["AWS_EXE_SYS_WORKER_LAMBDA"]

    payload = {
        "s3_location": order.get("s3_location", ""),
        "internal_bucket": internal_bucket,
    }
    if order.get("sops_key_ssm_path"):
        payload["sops_key_ssm_path"] = order["sops_key_ssm_path"]

    resp = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="Event",  # async
        Payload=json.dumps(payload, default=_json_default).encode(),
    )
    return resp.get("ResponseMetadata", {}).get("RequestId", "")


def _dispatch_codebuild(order: dict, run_id: str, internal_bucket: str) -> str:
    """Start a CodeBuild project for an order. Returns build ID."""
    codebuild_client = boto3.client("codebuild")
    project_name = os.environ["AWS_EXE_SYS_CODEBUILD_PROJECT"]

    env_overrides = [
        {"name": "S3_LOCATION", "value": order.get("s3_location", ""), "type": "PLAINTEXT"},
        {"name": "INTERNAL_BUCKET", "value": internal_bucket, "type": "PLAINTEXT"},
    ]
    if order.get("sops_key_ssm_path"):
        env_overrides.append(
            {"name": "SOPS_KEY_SSM_PATH", "value": order["sops_key_ssm_path"], "type": "PLAINTEXT"},
        )

    resp = codebuild_client.start_build(
        projectName=project_name,
        environmentVariablesOverride=env_overrides,
    )
    return resp.get("build", {}).get("id", "")


def _dispatch_ssm(order: dict, run_id: str, internal_bucket: str) -> str:
    """Send SSM Run Command for an order. Returns command ID."""
    ssm_client = boto3.client("ssm")
    document_name = order.get("ssm_document_name") or os.environ["AWS_EXE_SYS_SSM_DOCUMENT"]

    parameters = {
        "Commands": [json.dumps(order.get("cmds", []), default=_json_default)],
        "CallbackUrl": [order.get("callback_url", "")],
        "Timeout": [str(order.get("timeout", 300))],
    }

    env_dict = order.get("env_dict", {})
    if env_dict:
        parameters["EnvVars"] = [json.dumps(env_dict, default=_json_default)]

    s3_location = order.get("s3_location", "")
    if s3_location:
        parameters["S3Location"] = [s3_location]

    ssm_targets = order.get("ssm_targets", {})
    send_kwargs = {
        "DocumentName": document_name,
        "Parameters": parameters,
        "TimeoutSeconds": order.get("timeout", 300),
        "Comment": f"aws-exe-sys run_id={run_id} order={order.get('order_num', '')}",
    }

    if ssm_targets.get("instance_ids"):
        send_kwargs["InstanceIds"] = ssm_targets["instance_ids"]
    elif ssm_targets.get("tags"):
        send_kwargs["Targets"] = [
            {"Key": f"tag:{k}", "Values": [v] if isinstance(v, str) else v}
            for k, v in ssm_targets["tags"].items()
        ]

    resp = ssm_client.send_command(**send_kwargs)
    return resp.get("Command", {}).get("CommandId", "")


def _start_watchdog(
    order: dict,
    run_id: str,
    internal_bucket: str,
) -> str:
    """Start the watchdog Step Function for timeout safety. Returns execution ARN."""
    sfn_client = boto3.client("stepfunctions")
    state_machine_arn = os.environ.get("AWS_EXE_SYS_WATCHDOG_SFN", "")

    order_num = order.get("order_num", "")
    timeout = order.get("timeout", 300)

    sfn_input = {
        "run_id": run_id,
        "order_num": order_num,
        "timeout": timeout,
        "start_time": int(time.time()),
        "internal_bucket": internal_bucket,
    }

    resp = sfn_client.start_execution(
        stateMachineArn=state_machine_arn,
        name=f"{run_id}-{order_num}",
        input=json.dumps(sfn_input, default=_json_default),
    )
    return resp.get("executionArn", "")


def _dispatch_single(
    order: dict,
    run_id: str,
    flow_id: str,
    trace_id: str,
    internal_bucket: str,
    dynamodb_resource=None,
) -> dict:
    """Dispatch a single order (Lambda, CodeBuild, or SSM) + start watchdog."""
    order_num = order.get("order_num", "")
    order_name = order.get("order_name", order_num)

    execution_target = order.get("execution_target", "codebuild")

    # Dispatch to execution environment
    if execution_target == "lambda":
        execution_id = _dispatch_lambda(order, run_id, internal_bucket)
    elif execution_target == "ssm":
        execution_id = _dispatch_ssm(order, run_id, internal_bucket)
    else:
        execution_id = _dispatch_codebuild(order, run_id, internal_bucket)

    # Start watchdog
    watchdog_arn = _start_watchdog(order, run_id, internal_bucket)

    # Update order status to running
    dynamodb.update_order_status(
        run_id=run_id,
        order_num=order_num,
        status=RUNNING,
        extra_fields={
            "execution_url": execution_id,
            "step_function_url": watchdog_arn,
        },
        dynamodb_resource=dynamodb_resource,
    )

    # Write order event
    dynamodb.put_event(
        trace_id=trace_id,
        order_name=order_name,
        event_type="dispatched",
        status=RUNNING,
        extra_fields={
            "run_id": run_id,
            "order_num": order_num,
            "flow_id": flow_id,
            "execution_url": execution_id,
        },
        dynamodb_resource=dynamodb_resource,
    )

    return {
        "order_num": order_num,
        "order_name": order_name,
        "execution_id": execution_id,
        "watchdog_arn": watchdog_arn,
    }


def dispatch_orders(
    ready_orders: List[dict],
    run_id: str,
    flow_id: str,
    trace_id: str,
    internal_bucket: str = "",
    dynamodb_resource=None,
) -> List[dict]:
    """Dispatch all ready orders in parallel.

    Returns list of dispatch results.
    """
    if not internal_bucket:
        internal_bucket = os.environ.get("AWS_EXE_SYS_INTERNAL_BUCKET", "")

    if not ready_orders:
        return []

    results = []

    with ThreadPoolExecutor(max_workers=min(len(ready_orders), 10)) as executor:
        futures = {
            executor.submit(
                _dispatch_single,
                order, run_id, flow_id, trace_id,
                internal_bucket, dynamodb_resource,
            ): order
            for order in ready_orders
        }

        for future in as_completed(futures):
            try:
                result = future.result()
                results.append(result)
            except Exception as e:
                order = futures[future]
                logger.error(
                    "Failed to dispatch order %s: %s",
                    order.get("order_num"), e,
                )

    return results
