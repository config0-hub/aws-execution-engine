"""DynamoDB operations for orders, order_events, and orchestrator_locks tables."""

import functools
import logging
import os
import random
import time
from typing import Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Retry configuration
MAX_RETRIES = 4
BASE_DELAY = 0.5  # seconds
MAX_DELAY = 16.0  # seconds

_THROTTLE_CODES = frozenset({
    "ProvisionedThroughputExceededException",
    "ThrottlingException",
    "RequestLimitExceeded",
})


def retry_on_throttle(func):
    """Retry DynamoDB operations on throttling with exponential backoff + jitter."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        last_exc = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except ClientError as exc:
                error_code = exc.response["Error"]["Code"]
                if error_code not in _THROTTLE_CODES:
                    raise
                last_exc = exc
                if attempt < MAX_RETRIES:
                    delay = min(BASE_DELAY * (2 ** attempt), MAX_DELAY)
                    jitter = random.uniform(0, delay * 0.5)
                    sleep_time = delay + jitter
                    logger.warning(
                        "DynamoDB throttled (%s), retry %d/%d in %.1fs",
                        error_code, attempt + 1, MAX_RETRIES, sleep_time,
                    )
                    time.sleep(sleep_time)
        raise last_exc
    return wrapper


def _get_table(table_env_var: str, dynamodb_resource=None):
    """Get a DynamoDB table resource."""
    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")
    table_name = os.environ[table_env_var]
    return dynamodb_resource.Table(table_name)


# --- Orders table operations ---


@retry_on_throttle
def put_order(
    run_id: str,
    order_num: str,
    order_data: dict,
    dynamodb_resource=None,
) -> None:
    """Insert an order record into the orders table."""
    table = _get_table("AWS_EXE_SYS_ORDERS_TABLE", dynamodb_resource)
    item = {
        "pk": f"{run_id}:{order_num}",
        "run_id": run_id,
        "order_num": order_num,
        **order_data,
    }
    table.put_item(Item=item)


@retry_on_throttle
def get_order(
    run_id: str,
    order_num: str,
    dynamodb_resource=None,
) -> Optional[dict]:
    """Get a single order by run_id and order_num."""
    table = _get_table("AWS_EXE_SYS_ORDERS_TABLE", dynamodb_resource)
    response = table.get_item(Key={"pk": f"{run_id}:{order_num}"})
    return response.get("Item")


@retry_on_throttle
def get_all_orders(
    run_id: str,
    dynamodb_resource=None,
) -> List[dict]:
    """Query all orders for a run_id using GSI."""
    table = _get_table("AWS_EXE_SYS_ORDERS_TABLE", dynamodb_resource)
    response = table.query(
        IndexName="run_id-order_num-index",
        KeyConditionExpression=Key("run_id").eq(run_id),
    )
    return response.get("Items", [])


@retry_on_throttle
def update_order_status(
    run_id: str,
    order_num: str,
    status: str,
    extra_fields: Optional[dict] = None,
    dynamodb_resource=None,
) -> None:
    """Update order status and last_update timestamp."""
    table = _get_table("AWS_EXE_SYS_ORDERS_TABLE", dynamodb_resource)
    update_expr = "SET #status = :status, last_update = :last_update"
    expr_values = {
        ":status": status,
        ":last_update": int(time.time()),
    }
    expr_names = {"#status": "status"}

    if extra_fields:
        for k, v in extra_fields.items():
            safe_key = k.replace("-", "_")
            update_expr += f", #{safe_key} = :{safe_key}"
            expr_values[f":{safe_key}"] = v
            expr_names[f"#{safe_key}"] = k

    table.update_item(
        Key={"pk": f"{run_id}:{order_num}"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
        ExpressionAttributeNames=expr_names,
    )


# --- Order events table operations ---


@retry_on_throttle
def put_event(
    trace_id: str,
    order_name: str,
    event_type: str,
    status: str,
    data: Optional[dict] = None,
    extra_fields: Optional[dict] = None,
    dynamodb_resource=None,
) -> None:
    """Insert an event with current epoch as SK.

    Args:
        data: Subprocess payload -- stored as nested map under "data" key.
        extra_fields: Metadata fields (flow_id, run_id) -- stored at top level.
    """
    table = _get_table("AWS_EXE_SYS_ORDER_EVENTS_TABLE", dynamodb_resource)
    epoch = int(time.time())
    sk = f"{order_name}:{epoch}:{event_type}"
    item = {
        "trace_id": trace_id,
        "sk": sk,
        "order_name": order_name,
        "epoch": epoch,
        "event_type": event_type,
        "status": status,
        "ttl": epoch + 900,  # 15 min for testing; change to 172800 (2 days) for prod
    }
    if extra_fields:
        item.update(extra_fields)
    if data:
        item["data"] = data
    table.put_item(Item=item)


@retry_on_throttle
def get_events(
    trace_id: str,
    order_name_prefix: Optional[str] = None,
    dynamodb_resource=None,
) -> List[dict]:
    """Query events for a trace_id, optional begins_with filter on SK."""
    table = _get_table("AWS_EXE_SYS_ORDER_EVENTS_TABLE", dynamodb_resource)
    if order_name_prefix:
        response = table.query(
            KeyConditionExpression=Key("trace_id").eq(trace_id)
            & Key("sk").begins_with(f"{order_name_prefix}:")
        )
    else:
        response = table.query(
            KeyConditionExpression=Key("trace_id").eq(trace_id)
        )
    return response.get("Items", [])


@retry_on_throttle
def get_latest_event(
    trace_id: str,
    order_name: str,
    dynamodb_resource=None,
) -> Optional[dict]:
    """Get the most recent event for an order."""
    table = _get_table("AWS_EXE_SYS_ORDER_EVENTS_TABLE", dynamodb_resource)
    response = table.query(
        KeyConditionExpression=Key("trace_id").eq(trace_id)
        & Key("sk").begins_with(f"{order_name}:"),
        ScanIndexForward=False,
        Limit=1,
    )
    items = response.get("Items", [])
    return items[0] if items else None


# --- Orchestrator locks table operations ---


@retry_on_throttle
def acquire_lock(
    run_id: str,
    orchestrator_id: str,
    ttl: int,
    flow_id: str,
    trace_id: str,
    dynamodb_resource=None,
) -> bool:
    """Acquire a lock using conditional put.

    Succeeds if lock doesn't exist OR status is 'completed'.
    Returns True if lock acquired, False otherwise.
    """
    table = _get_table("AWS_EXE_SYS_LOCKS_TABLE", dynamodb_resource)
    now = int(time.time())
    try:
        table.put_item(
            Item={
                "pk": f"lock:{run_id}",
                "run_id": run_id,
                "orchestrator_id": orchestrator_id,
                "status": "active",
                "acquired_at": now,
                "ttl": now + ttl,
                "flow_id": flow_id,
                "trace_id": trace_id,
            },
            ConditionExpression=Attr("pk").not_exists() | Attr("status").eq("completed"),
        )
        return True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        return False


@retry_on_throttle
def release_lock(
    run_id: str,
    dynamodb_resource=None,
) -> None:
    """Release a lock by updating status to completed."""
    table = _get_table("AWS_EXE_SYS_LOCKS_TABLE", dynamodb_resource)
    table.update_item(
        Key={"pk": f"lock:{run_id}"},
        UpdateExpression="SET #status = :status",
        ExpressionAttributeValues={":status": "completed"},
        ExpressionAttributeNames={"#status": "status"},
    )


@retry_on_throttle
def get_lock(
    run_id: str,
    dynamodb_resource=None,
) -> Optional[dict]:
    """Get the current lock record for a run_id."""
    table = _get_table("AWS_EXE_SYS_LOCKS_TABLE", dynamodb_resource)
    response = table.get_item(Key={"pk": f"lock:{run_id}"})
    return response.get("Item")
