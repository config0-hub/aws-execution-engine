"""Validate SSM orders before processing."""

from typing import List

from aws_exe_sys.common.models import SsmJob


def validate_ssm_orders(job: SsmJob) -> List[str]:
    """Validate all SSM orders in a job. Returns list of errors (empty if valid).

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

        # ssm_targets is required
        if not order.ssm_targets:
            return [f"{order_label}: ssm_targets is required for SSM orders"]

        # ssm_targets must contain instance_ids or tags
        has_ids = bool(order.ssm_targets.get("instance_ids"))
        has_tags = bool(order.ssm_targets.get("tags"))
        if not has_ids and not has_tags:
            return [f"{order_label}: ssm_targets must contain 'instance_ids' or 'tags'"]

    return []
