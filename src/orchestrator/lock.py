"""Lock management for orchestrator — wraps dynamodb lock operations."""

import uuid

from src.common import dynamodb


def acquire_lock(
    run_id: str,
    flow_id: str,
    trace_id: str,
    ttl: int = 3600,
    dynamodb_resource=None,
) -> bool:
    """Attempt to acquire the orchestrator lock for a run_id.

    Returns True if lock acquired, False if another orchestrator instance
    is already handling this run_id.
    """
    orchestrator_id = str(uuid.uuid4())
    return dynamodb.acquire_lock(
        run_id=run_id,
        orchestrator_id=orchestrator_id,
        ttl=ttl,
        flow_id=flow_id,
        trace_id=trace_id,
        dynamodb_resource=dynamodb_resource,
    )


def release_lock(run_id: str, dynamodb_resource=None) -> None:
    """Release the orchestrator lock for a run_id."""
    dynamodb.release_lock(run_id=run_id, dynamodb_resource=dynamodb_resource)
