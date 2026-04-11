"""Check completion and finalize job when all orders are done."""

import logging
import os
from typing import List

from aws_exe_sys.common import dynamodb, s3 as s3_ops
from aws_exe_sys.common.statuses import JOB_ORDER_NAME, TERMINAL_STATUSES, SUCCEEDED, FAILED, TIMED_OUT
from aws_exe_sys.common.sops import delete_sops_key_ssm
from aws_exe_sys.orchestrator.lock import release_lock

logger = logging.getLogger(__name__)


def _resolve_job_status(orders: List[dict]) -> str:
    """Determine overall job status from order statuses."""
    has_timed_out = False
    has_failed = False

    for order in orders:
        status = order.get("status", "")
        must_succeed = order.get("must_succeed", True)

        if status == TIMED_OUT:
            has_timed_out = True
        elif status == FAILED and must_succeed:
            has_failed = True

    if has_timed_out:
        return TIMED_OUT
    if has_failed:
        return FAILED
    return SUCCEEDED


def _build_summary(orders: List[dict]) -> dict:
    """Build a summary dict of order statuses."""
    summary = {SUCCEEDED: 0, FAILED: 0, TIMED_OUT: 0}
    for order in orders:
        status = order.get("status", "")
        if status in summary:
            summary[status] += 1
    return summary


def check_and_finalize(
    orders: List[dict],
    run_id: str,
    flow_id: str,
    trace_id: str,
    done_bucket: str = "",
    dynamodb_resource=None,
    s3_client=None,
) -> bool:
    """Check if all orders are done and finalize if so.

    Returns True if finalized, False if still in progress.
    """
    if not done_bucket:
        done_bucket = os.environ.get("AWS_EXE_SYS_DONE_BUCKET", "")

    # Check if all orders are terminal
    all_done = all(
        order.get("status", "") in TERMINAL_STATUSES
        for order in orders
    )

    if not all_done:
        # Release lock — next S3 callback will re-trigger
        release_lock(run_id, dynamodb_resource=dynamodb_resource)
        return False

    # All done — finalize
    job_status = _resolve_job_status(orders)
    summary = _build_summary(orders)

    # Write job-level completion event
    dynamodb.put_event(
        trace_id=trace_id,
        order_name=JOB_ORDER_NAME,
        event_type="job_completed",
        status=job_status,
        extra_fields={
            "flow_id": flow_id,
            "run_id": run_id,
            "summary": summary,
            "done_endpt": f"s3://{done_bucket}/{run_id}/done",
        },
        dynamodb_resource=dynamodb_resource,
    )

    # Clean up SOPS keys from SSM
    for order in orders:
        sops_key_ssm_path = order.get("sops_key_ssm_path")
        if sops_key_ssm_path:
            try:
                delete_sops_key_ssm(sops_key_ssm_path)
            except Exception as e:
                logger.warning("Failed to delete SOPS key %s: %s", sops_key_ssm_path, e)

    # Write done endpoint
    s3_ops.write_done_endpoint(
        bucket=done_bucket,
        run_id=run_id,
        status=job_status,
        summary=summary,
        s3_client=s3_client,
    )

    # Release lock
    release_lock(run_id, dynamodb_resource=dynamodb_resource)

    logger.info("Job %s finalized: %s (summary: %s)", run_id, job_status, summary)
    return True
