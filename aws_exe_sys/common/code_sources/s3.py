"""S3-backed code source.

Each fetch is independent (no shared state), so ``cleanup`` is a no-op.
"""

from typing import Any

from aws_exe_sys.common.code_source import fetch_code_s3


class S3CodeSource:
    """Code source for orders that ship a zipped code bundle in S3."""

    kind = "s3"

    def detect(self, order: Any, job: Any) -> bool:
        return bool(getattr(order, "s3_location", None))

    def fetch(self, order: Any, job: Any) -> str:
        return fetch_code_s3(order.s3_location)

    def cleanup(self) -> None:
        """No per-run shared state; nothing to release."""
        return None
