"""Dispatch ready orders through the execution target registry."""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal
from typing import List

import boto3

from aws_exe_sys.common import dynamodb
from aws_exe_sys.common.models import RUNNING
from aws_exe_sys.orchestrator.targets import TARGETS, UnknownTargetError

logger = logging.getLogger(__name__)


def _json_default(obj):
    """JSON encoder default for DynamoDB Decimal types."""
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


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
    """Dispatch a single order (Lambda, CodeBuild, or SSM) + start watchdog.

    Two-step reservation pattern:
      1. Atomically flip QUEUED -> DISPATCHING. If this fails, another
         orchestrator already owns the order; return empty result.
      2. Invoke the worker and start the watchdog.
      3. Flip DISPATCHING -> RUNNING with execution metadata.
    """
    order_num = order.get("order_num", "")
    order_name = order.get("order_name", order_num)

    # Step 1: reserve the order
    reserved = dynamodb.reserve_order_for_dispatch(
        run_id=run_id,
        order_num=order_num,
        dynamodb_resource=dynamodb_resource,
    )
    if not reserved:
        logger.info(
            "Skipping dispatch for %s/%s — order not in QUEUED state",
            run_id, order_num,
        )
        return {
            "order_num": order_num,
            "order_name": order_name,
            "execution_id": "",
            "watchdog_arn": "",
            "skipped": True,
        }

    execution_target = order.get("execution_target", "codebuild")

    # Step 2: dispatch through the execution target registry. Third-party
    # targets (ECS, Fargate, on-prem agents, …) are registered via
    # ``aws_exe_sys.orchestrator.targets.register_target`` — no if/elif here.
    if execution_target not in TARGETS:
        raise UnknownTargetError(
            f"unknown execution_target {execution_target!r}; "
            f"registered targets: {sorted(TARGETS)}"
        )
    execution_id = TARGETS[execution_target].dispatch(
        order, run_id, internal_bucket,
    )

    # Start watchdog
    watchdog_arn = _start_watchdog(order, run_id, internal_bucket)

    # Step 3: flip DISPATCHING -> RUNNING with execution metadata
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
