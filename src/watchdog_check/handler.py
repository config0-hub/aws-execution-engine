"""Watchdog check Lambda — Step Function timeout safety net."""

import logging
import time
from typing import Any, Dict

from src.common import s3 as s3_ops

logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any = None) -> dict:
    """Lambda handler invoked by Step Function.

    Input:
        run_id, order_num, timeout (seconds), start_time (epoch),
        internal_bucket

    Returns:
        {"done": true}  — result exists or timeout written
        {"done": false}  — still waiting
    """
    run_id = event["run_id"]
    order_num = event["order_num"]
    timeout = event["timeout"]
    start_time = event["start_time"]
    internal_bucket = event["internal_bucket"]

    # Check if result.json already exists
    exists = s3_ops.check_result_exists(
        bucket=internal_bucket,
        run_id=run_id,
        order_num=order_num,
    )

    if exists:
        logger.info("Result exists for %s/%s", run_id, order_num)
        return {"done": True}

    # Check if timeout exceeded
    now = int(time.time())
    if now > start_time + timeout:
        logger.warning(
            "Timeout exceeded for %s/%s (started=%d, timeout=%d, now=%d)",
            run_id, order_num, start_time, timeout, now,
        )
        # Write timed_out result
        s3_ops.write_result(
            bucket=internal_bucket,
            run_id=run_id,
            order_num=order_num,
            status="timed_out",
            log="Worker unresponsive, timed out by watchdog",
        )
        return {"done": True}

    # Still waiting
    logger.info(
        "Waiting for %s/%s (elapsed=%ds, timeout=%ds)",
        run_id, order_num, now - start_time, timeout,
    )
    return {"done": False}
