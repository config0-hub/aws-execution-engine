"""Event sink package.

Built-in sinks:
    ``dynamodb`` — wraps the legacy :func:`aws_exe_sys.common.dynamodb.put_event`
    helper. Active by default.

Third parties register additional sinks at import time::

    from aws_exe_sys.common.events import register_sink

    class CloudWatchSink:
        name = "cloudwatch"
        def emit(self, event):
            ...

    register_sink(CloudWatchSink())

Emission entry point: call :func:`emit` from anywhere. The event is
dispatched to every sink named in the ``AWS_EXE_SYS_EVENT_SINKS``
environment variable (comma-separated, default ``"dynamodb"``) via a
fresh :class:`CompositeEventSink`.
"""
from __future__ import annotations

import os
from typing import Any, Dict

from .base import EventSink
from .composite import CompositeEventSink
from .dynamodb_sink import DynamoDbEventSink
from .registry import (
    SINKS,
    UnknownSinkError,
    get_sink,
    list_sinks,
    register_sink,
)

# Seed the built-in DynamoDB sink. Order is preserved by the underlying
# dict, so the default sink list ``"dynamodb"`` always resolves.
register_sink(DynamoDbEventSink())


def emit(event: Dict[str, Any]) -> None:
    """Top-level emit.

    Resolves the active sinks via ``AWS_EXE_SYS_EVENT_SINKS``
    (comma-separated names, default ``"dynamodb"``) and fans out
    through a fresh :class:`CompositeEventSink`. The composite is
    intentionally constructed per call so that env-var changes take
    effect immediately and the implementation stays stateless.
    """
    raw = os.environ.get("AWS_EXE_SYS_EVENT_SINKS", "dynamodb")
    names = [n.strip() for n in raw.split(",") if n.strip()]
    children = [get_sink(n) for n in names]
    CompositeEventSink(children).emit(event)


__all__ = [
    "EventSink",
    "DynamoDbEventSink",
    "CompositeEventSink",
    "SINKS",
    "UnknownSinkError",
    "emit",
    "get_sink",
    "list_sinks",
    "register_sink",
]
