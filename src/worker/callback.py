"""Send callback result via presigned S3 PUT URL."""

import json
import logging
import time

import requests

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


def send_callback(callback_url: str, status: str, log: str) -> bool:
    """PUT result JSON to presigned S3 URL.

    Retries up to MAX_RETRIES times on failure.
    Returns True if successful, False if all retries exhausted.
    """
    payload = json.dumps({"status": status, "log": log})

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
    return False
