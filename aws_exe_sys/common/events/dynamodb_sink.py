"""Built-in DynamoDB event sink.

Wraps the existing :func:`aws_exe_sys.common.dynamodb.put_event` helper so call
sites that go through :func:`events.emit` land in the same
``order_events`` table using the same retry decorator. Migration is
incremental — :func:`put_event` stays as the sink's implementation
detail and its tests stay valid. The 7 production call sites currently
calling ``dynamodb.put_event`` directly are intentionally NOT migrated
in this phase.
"""
from __future__ import annotations

from typing import Any, Dict

from aws_exe_sys.common import dynamodb


class DynamoDbEventSink:
    """Built-in sink that writes events to the ``order_events`` table.

    The sink unpacks the four required positional fields
    (``trace_id``, ``order_name``, ``event_type``, ``status``) plus the
    optional ``data`` payload and forwards everything else as
    ``extra_fields`` so metadata such as ``flow_id`` and ``run_id``
    lands at the top level of the row, matching the existing
    :func:`put_event` contract.
    """

    name = "dynamodb"

    def emit(self, event: Dict[str, Any]) -> None:
        trace_id = event["trace_id"]
        order_name = event["order_name"]
        event_type = event["event_type"]
        status = event["status"]
        data = event.get("data")
        reserved = {"trace_id", "order_name", "event_type", "status", "data"}
        extra = {k: v for k, v in event.items() if k not in reserved}
        dynamodb.put_event(
            trace_id=trace_id,
            order_name=order_name,
            event_type=event_type,
            status=status,
            data=data,
            extra_fields=extra or None,
        )
