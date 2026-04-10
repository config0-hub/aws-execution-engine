"""Validate orders before processing."""

from typing import List

from src.common.models import EXECUTION_TARGETS, Job


def validate_orders(job: Job) -> List[str]:
    """Validate all orders in a job. Returns list of errors (empty if valid).

    Fail-fast: returns on the first invalid order.
    """
    if not job.orders:
        return ["Job has no orders"]

    for i, order in enumerate(job.orders):
        order_label = order.order_name or f"order[{i}]"

        # cmds must exist and be non-empty
        if not order.cmds:
            return [f"{order_label}: cmds is empty or missing"]

        # timeout must be present and positive
        if not order.timeout or order.timeout <= 0:
            return [f"{order_label}: timeout is missing or invalid"]

        # execution_target must be valid
        if order.execution_target not in EXECUTION_TARGETS:
            return [f"{order_label}: invalid execution_target '{order.execution_target}' "
                    f"(must be one of {sorted(EXECUTION_TARGETS)})"]

        # Must have a code source: s3_location OR (git_repo + git_token_location from job)
        has_s3 = bool(order.s3_location)
        has_git = bool(order.git_repo or job.git_repo) and bool(job.git_token_location)
        if not has_s3 and not has_git:
            return [f"{order_label}: no code source (need s3_location or git_repo + git_token_location)"]

    return []
