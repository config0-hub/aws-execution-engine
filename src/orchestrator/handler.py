"""Lambda entrypoint for orchestrator — Part 2: execute_orders."""

import logging
import os
import re
from typing import Any, Dict

from src.common import dynamodb
from src.common.models import FAILED
from src.orchestrator.lock import acquire_lock, release_lock
from src.orchestrator.read_state import read_state
from src.orchestrator.evaluate import evaluate_orders
from src.orchestrator.dispatch import dispatch_orders
from src.orchestrator.finalize import check_and_finalize

logger = logging.getLogger(__name__)


def _parse_run_id_from_s3_key(key: str) -> str:
    """Extract run_id from S3 key path.

    Expected format: tmp/callbacks/runs/<run_id>/<order_num>/result.json
    """
    match = re.search(r"tmp/callbacks/runs/([^/]+)/", key)
    if match:
        return match.group(1)
    return ""


def execute_orders(run_id: str, dynamodb_resource=None, s3_client=None) -> dict:
    """Main orchestrator logic for a given run_id."""
    internal_bucket = os.environ.get("AWS_EXE_SYS_INTERNAL_BUCKET", "")
    done_bucket = os.environ.get("AWS_EXE_SYS_DONE_BUCKET", "")

    # Read state (updates running orders from S3 callbacks)
    orders = read_state(
        run_id=run_id,
        internal_bucket=internal_bucket,
        dynamodb_resource=dynamodb_resource,
        s3_client=s3_client,
    )

    if not orders:
        logger.warning("No orders found for run_id=%s", run_id)
        release_lock(run_id, dynamodb_resource=dynamodb_resource)
        return {"status": "no_orders"}

    # Extract trace_id and flow_id from first order
    trace_id = orders[0].get("trace_id", "")
    flow_id = orders[0].get("flow_id", "")

    # Evaluate dependencies — loop until no more cascading failures
    while True:
        ready, failed_deps, waiting = evaluate_orders(orders)

        if not failed_deps:
            break

        # Mark failed-due-to-deps orders and update in-memory list
        for order in failed_deps:
            dynamodb.update_order_status(
                run_id=run_id,
                order_num=order.get("order_num", ""),
                status=FAILED,
                extra_fields={"failure_reason": "dependency_failed"},
                dynamodb_resource=dynamodb_resource,
            )
            order["status"] = FAILED

            dynamodb.put_event(
                trace_id=trace_id,
                order_name=order.get("order_name", ""),
                event_type="dependency_failed",
                status=FAILED,
                extra_fields={"run_id": run_id},
                dynamodb_resource=dynamodb_resource,
            )

    # Dispatch ready orders
    if ready:
        dispatch_orders(
            ready_orders=ready,
            run_id=run_id,
            flow_id=flow_id,
            trace_id=trace_id,
            internal_bucket=internal_bucket,
            dynamodb_resource=dynamodb_resource,
        )

    # Check if all done and finalize
    # Re-read to get latest statuses after dispatch
    all_orders = dynamodb.get_all_orders(run_id, dynamodb_resource=dynamodb_resource)
    finalized = check_and_finalize(
        orders=all_orders,
        run_id=run_id,
        flow_id=flow_id,
        trace_id=trace_id,
        done_bucket=done_bucket,
        dynamodb_resource=dynamodb_resource,
        s3_client=s3_client,
    )

    return {
        "status": "finalized" if finalized else "in_progress",
        "dispatched": len(ready),
        "failed_deps": len(failed_deps),
        "waiting": len(waiting),
    }


def handler(event: Dict[str, Any], context: Any = None) -> dict:
    """Lambda entrypoint — triggered by S3 ObjectCreated event."""
    # Parse run_id from S3 event
    run_id = ""
    if "Records" in event:
        for record in event["Records"]:
            s3_key = record.get("s3", {}).get("object", {}).get("key", "")
            run_id = _parse_run_id_from_s3_key(s3_key)
            if run_id:
                break

    if not run_id:
        logger.error("Could not extract run_id from event: %s", event)
        return {"status": "error", "message": "Missing run_id"}

    # Acquire lock
    # Use placeholder flow_id/trace_id — will be read from orders
    if not acquire_lock(run_id, flow_id="", trace_id=""):
        logger.info("Lock not acquired for run_id=%s, another instance is handling", run_id)
        return {"status": "skipped", "message": "Lock not acquired"}

    try:
        return execute_orders(run_id)
    except Exception as e:
        logger.exception("Orchestrator failed for run_id=%s", run_id)
        release_lock(run_id)
        return {"status": "error", "message": str(e)}
