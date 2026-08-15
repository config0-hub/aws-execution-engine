"""Best-effort completion callback: POST the ExecutionResult to a caller-supplied URL.

Optional extension to the worker's always-write-a-result invariant. A callback
is attempted only after the done-marker write succeeds, and a callback
failure is LOG-ONLY - it must never fail the execution or the marker write.
This is the one sanctioned best-effort seam (decided 2026-08-13).
"""

import json
import logging
import urllib.error
import urllib.request

from aws_exe_sys.common.result_writer import ExecutionResult

logger = logging.getLogger(__name__)

_TIMEOUT_SECONDS = 10


def post_callback(callback_url: str | None, callback_token: str | None, result: ExecutionResult) -> None:
    """POST result to callback_url, bearer-authed with callback_token if set.

    No-op when callback_url is absent. Never raises: a failure is logged and
    swallowed so the caller's already-written done-marker is unaffected.
    """
    if not callback_url:
        return

    headers = {"Content-Type": "application/json"}
    if callback_token:
        headers["Authorization"] = f"Bearer {callback_token}"
    request = urllib.request.Request(
        callback_url,
        data=json.dumps(result.to_dict()).encode("utf-8"),
        headers=headers,
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS):
            pass
    except (urllib.error.URLError, OSError) as exc:
        logger.warning(
            "Completion callback failed: trigger_id=%s callback_url=%s error=%s",
            result.trigger_id,
            callback_url,
            exc,
        )
