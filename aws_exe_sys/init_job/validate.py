"""Validate orders before processing."""

from typing import Dict, List, Optional

from aws_exe_sys.common.models import EXECUTION_TARGETS, Job


def _detect_cycle(job: Job) -> Optional[str]:
    """DFS cycle detection over the order dependency graph.

    Returns a human-readable description of the cycle path on the first
    cycle found, or None if the graph is acyclic.
    """
    # Map dep identifiers (order_name and queue_id) back to the canonical
    # order label used in error messages.
    name_by_id: Dict[str, str] = {}
    graph: Dict[str, List[str]] = {}

    for i, order in enumerate(job.orders):
        canonical = order.order_name or f"order[{i}]"
        graph[canonical] = list(order.dependencies or [])
        name_by_id[canonical] = canonical
        if order.order_name:
            name_by_id[order.order_name] = canonical
        if order.queue_id:
            name_by_id[order.queue_id] = canonical

    WHITE, GRAY, BLACK = 0, 1, 2
    color: Dict[str, int] = {node: WHITE for node in graph}

    def dfs(node: str, path: List[str]) -> Optional[List[str]]:
        color[node] = GRAY
        path.append(node)
        for dep in graph.get(node, []):
            target = name_by_id.get(dep, dep)
            if target not in graph:
                # Unknown dep — not a cycle concern here; other validation
                # can catch dangling references later.
                continue
            if color.get(target, WHITE) == GRAY:
                # Found a back edge — reconstruct the cycle path
                start = path.index(target) if target in path else 0
                return path[start:] + [target]
            if color.get(target, WHITE) == WHITE:
                result = dfs(target, path)
                if result is not None:
                    return result
        path.pop()
        color[node] = BLACK
        return None

    for start in graph:
        if color[start] == WHITE:
            cycle = dfs(start, [])
            if cycle:
                return " -> ".join(cycle)
    return None


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

    # presign_expiry must cover the longest order timeout (+ buffer) so callbacks
    # can complete before the URL expires.
    max_timeout = max((o.timeout for o in job.orders), default=0)
    presign_buffer = 300  # 5-minute safety margin
    if job.presign_expiry < max_timeout + presign_buffer:
        return [
            f"presign_expiry ({job.presign_expiry}) must be >= max order timeout "
            f"({max_timeout}) + {presign_buffer}s buffer"
        ]

    # Cycle detection runs last — fail fast on the simpler checks first.
    cycle_path = _detect_cycle(job)
    if cycle_path is not None:
        return [f"cyclic dependency involving {cycle_path}"]

    return []
