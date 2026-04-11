"""Send callback result via presigned S3 PUT URL."""

import json
import logging
import time

import requests

from aws_exe_sys.common.schemas import ResultV1

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def send_callback(
    callback_url: str,
    status: str,
    log: str,
    *,
    run_id: str = "",
    order_num: str = "",
) -> bool:
    """PUT result JSON to presigned S3 URL.

    Retries up to MAX_RETRIES times on failure. If all retries are
    exhausted and both ``run_id`` and ``order_num`` are provided, falls
    back to a direct DynamoDB ``update_order_status`` write so the order
    is not left stranded in ``RUNNING`` when presigned S3 PUT is
    unreachable. The fallback is a logged no-op when either id is empty —
    we never fabricate synthetic ids.

    Returns True if the presigned PUT succeeded, False otherwise
    (regardless of whether the DynamoDB fallback fired or not).
    """
    payload = json.dumps(ResultV1(status=status, log=log).to_dict())

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.put(
                callback_url,
                data=payload,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            if resp.status_code in (200, 201, 204):
                logger.info("Callback sent: status=%s", status)
                return True
            else:
                logger.warning(
                    "Callback returned %d (attempt %d/%d)",
                    resp.status_code, attempt + 1, MAX_RETRIES + 1,
                )
        except Exception as e:
            logger.warning(
                "Callback failed (attempt %d/%d): %s",
                attempt + 1, MAX_RETRIES + 1, e,
            )

        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY)

    logger.error("All callback retries exhausted for status=%s", status)

    # DynamoDB fallback — write failed status directly so the order is
    # not left stranded in RUNNING. Skip if either id is missing (logged
    # no-op); fabricating ids would either miss the real row or create a
    # phantom row.
    if not run_id or not order_num:
        logger.warning(
            "Callback fallback skipped: missing run_id/order_num "
            "(run_id=%r, order_num=%r)",
            run_id, order_num,
        )
        return False

    try:
        # Lazy import so boto3 is only pulled in when the fallback fires.
        from aws_exe_sys.common.dynamodb import update_order_status

        update_order_status(
            run_id=run_id,
            order_num=order_num,
            status="failed",
            extra_fields={"error": "callback_failed"},
        )
        logger.warning(
            "Callback fallback wrote DynamoDB status=failed for "
            "run_id=%s order_num=%s",
            run_id, order_num,
        )
    except Exception:
        logger.exception(
            "Callback fallback DynamoDB write failed for "
            "run_id=%s order_num=%s",
            run_id, order_num,
        )

    return False
