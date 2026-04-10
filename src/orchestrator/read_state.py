"""Read current state of all orders for a run, updating from S3 callbacks."""

import json
import logging
import os
from typing import Dict, List, Optional

from src.common import dynamodb, s3 as s3_ops
from src.common.statuses import RUNNING, TERMINAL_STATUSES, SUCCEEDED, FAILED, TIMED_OUT

logger = logging.getLogger(__name__)


def read_state(
    run_id: str,
    trace_id: str = "",
    internal_bucket: str = "",
    dynamodb_resource=None,
    s3_client=None,
) -> List[dict]:
    """Read all orders for a run_id and check for new results.

    For running orders, checks S3 for result.json. If found:
    - Parses the result (status + log)
    - Updates order status in DynamoDB
    - Writes an order_event

    Returns the full list of order records (updated).
    """
    if not internal_bucket:
        internal_bucket = os.environ.get("AWS_EXE_SYS_INTERNAL_BUCKET", "")

    orders = dynamodb.get_all_orders(run_id, dynamodb_resource=dynamodb_resource)

    for order in orders:
        status = order.get("status", "")

        # Only check running orders for new results
        if status != RUNNING:
            continue

        order_num = order.get("order_num", "")
        result = s3_ops.read_result(
            bucket=internal_bucket,
            run_id=run_id,
            order_num=order_num,
            s3_client=s3_client,
        )

        if result is None:
            continue

        # New result found — update state
        new_status = result.get("status", FAILED)
        log_output = result.get("log", "")

        dynamodb.update_order_status(
            run_id=run_id,
            order_num=order_num,
            status=new_status,
            extra_fields={"log": log_output} if log_output else None,
            dynamodb_resource=dynamodb_resource,
        )

        # Write order event
        order_name = order.get("order_name", order_num)
        dynamodb.put_event(
            trace_id=trace_id or order.get("trace_id", ""),
            order_name=order_name,
            event_type="completed",
            status=new_status,
            extra_fields={
                "run_id": run_id,
                "order_num": order_num,
            },
            dynamodb_resource=dynamodb_resource,
        )

        # Update the in-memory order record
        order["status"] = new_status

    return orders
