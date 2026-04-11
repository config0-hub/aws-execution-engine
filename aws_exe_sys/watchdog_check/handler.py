"""Watchdog check Lambda — Step Function timeout safety net."""

import logging
import random
import time
from typing import Any, Dict

from aws_exe_sys.common import s3 as s3_ops

logger = logging.getLogger(__name__)

# Continue-polling jitter window, in seconds. Every return from this
# handler populates ``wait_seconds`` so the Step Function's ``Wait``
# state can resolve it via ``SecondsPath``.
JITTER_MIN_SECONDS = 50
JITTER_MAX_SECONDS = 70


def handler(event: Dict[str, Any], context: Any = None) -> dict:
    """Lambda handler invoked by Step Function.

    Input:
        run_id, order_num, timeout (seconds), start_time (epoch),
        internal_bucket

    Returns:
        {"done": true,  "wait_seconds": 0}   — terminal
        {"done": false, "wait_seconds": N}   — continue polling after N
                                                seconds (jittered)
    """
    run_id = event["run_id"]
    order_num = event["order_num"]
    timeout = event["timeout"]
    start_time = event["start_time"]
    internal_bucket = event["internal_bucket"]

    now = int(time.time())
    elapsed = now - start_time
    hard_cap = 2 * timeout

    # (1) Hard cap — absolute backstop. MUST run first, before the
    # result-exists short-circuit: once the natural-timeout path writes
    # its result, the short-circuit would prevent every subsequent
    # iteration from reaching the cap, defeating the whole point of the
    # backstop (which exists for the case where the natural timeout's
    # own write_result FAILED due to S3 outage / throttling).
    if elapsed > hard_cap:
        logger.error(
            "Watchdog hard cap exceeded for %s/%s "
            "(elapsed=%ds, cap=%ds)",
            run_id, order_num, elapsed, hard_cap,
        )
        try:
            s3_ops.write_result(
                bucket=internal_bucket,
                run_id=run_id,
                order_num=order_num,
                status="timed_out_watchdog_cap",
                log=(
                    f"Watchdog hard cap exceeded after {elapsed}s "
                    f"(cap={hard_cap}s)"
                ),
            )
        except Exception:
            # Backstop invariant: the loop MUST terminate even if the
            # S3 write fails — dropping the result is strictly better
            # than looping forever against a broken S3.
            logger.exception(
                "Hard cap S3 write failed for %s/%s; returning done anyway",
                run_id, order_num,
            )
        return {"done": True, "wait_seconds": 0}

    # (2) Happy path — result already exists from the worker or from a
    # prior natural-timeout write.
    if s3_ops.check_result_exists(
        bucket=internal_bucket,
        run_id=run_id,
        order_num=order_num,
    ):
        logger.info("Result exists for %s/%s", run_id, order_num)
        return {"done": True, "wait_seconds": 0}

    # (3) Natural timeout — worker is unresponsive but we're still under
    # the hard cap. Write the canonical 'timed_out' status (distinct from
    # 'timed_out_watchdog_cap' so postmortems can tell the two apart).
    if elapsed > timeout:
        logger.warning(
            "Timeout exceeded for %s/%s (started=%d, timeout=%d, now=%d)",
            run_id, order_num, start_time, timeout, now,
        )
        s3_ops.write_result(
            bucket=internal_bucket,
            run_id=run_id,
            order_num=order_num,
            status="timed_out",
            log="Worker unresponsive, timed out by watchdog",
        )
        return {"done": True, "wait_seconds": 0}

    # (4) Still waiting — jittered poll interval smooths thundering-herd
    # when many orders start together. [50, 70] inclusive.
    wait_seconds = random.randint(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)
    logger.info(
        "Waiting for %s/%s (elapsed=%ds, timeout=%ds, next_wait=%ds)",
        run_id, order_num, elapsed, timeout, wait_seconds,
    )
    return {"done": False, "wait_seconds": wait_seconds}
