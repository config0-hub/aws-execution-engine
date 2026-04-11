"""Evaluate order dependencies and determine which orders are ready."""

from typing import Dict, List, Tuple

from aws_exe_sys.common.statuses import QUEUED, TERMINAL_STATUSES, FAILED_STATUSES, SUCCEEDED, FAILED, TIMED_OUT


def evaluate_orders(orders: List[dict]) -> Tuple[List[dict], List[dict], List[dict]]:
    """Evaluate dependency graph and classify queued orders.

    Returns:
        (ready_to_dispatch, failed_due_to_deps, still_waiting)
    """
    # Build lookup: queue_id (and order_name) -> status
    # Dependencies can reference either queue_id (order_num) or order_name
    status_by_queue_id: Dict[str, str] = {}
    for order in orders:
        qid = order.get("queue_id", order.get("order_num", ""))
        status_by_queue_id[qid] = order.get("status", "")
        # Also index by order_name so name-based deps resolve correctly
        order_name = order.get("order_name", "")
        if order_name:
            status_by_queue_id[order_name] = order.get("status", "")

    ready = []
    failed_deps = []
    waiting = []

    for order in orders:
        if order.get("status") != QUEUED:
            continue

        deps = order.get("dependencies", [])
        if not deps:
            # No dependencies — ready to go
            ready.append(order)
            continue

        must_succeed = order.get("must_succeed", True)
        all_succeeded = True
        any_dep_running = False
        any_dep_failed = False

        for dep_id in deps:
            dep_status = status_by_queue_id.get(dep_id, QUEUED)

            if dep_status == SUCCEEDED:
                continue
            elif dep_status in FAILED_STATUSES:
                any_dep_failed = True
                all_succeeded = False
            elif dep_status in (QUEUED, "running"):
                any_dep_running = True
                all_succeeded = False
            else:
                any_dep_running = True
                all_succeeded = False

        if all_succeeded:
            ready.append(order)
        elif any_dep_failed and must_succeed:
            failed_deps.append(order)
        elif any_dep_running or not any_dep_failed:
            waiting.append(order)
        else:
            # Deps failed but must_succeed=False — still ready
            ready.append(order)

    return ready, failed_deps, waiting
