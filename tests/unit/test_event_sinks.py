"""Unit tests for aws_exe_sys/common/events/* — the EventSink registry, the
built-in DynamoDB sink, the CompositeEventSink fan-out, third-party
sink registration, and unknown-sink errors.

The pattern matches ``tests/unit/test_dispatch_targets.py`` (which
exercises the equivalent ``orchestrator/targets/`` registry) plus the
moto fixture from ``tests/unit/test_dynamodb.py``.
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from aws_exe_sys.common import dynamodb
from aws_exe_sys.common import events
from aws_exe_sys.common.events import (
    CompositeEventSink,
    UnknownSinkError,
    register_sink,
)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_ORDER_EVENTS_TABLE", "test-order-events")


@pytest.fixture
def order_events_table(aws_env):
    """Create a moto-backed ``order_events`` table and yield the resource."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName="test-order-events",
            KeySchema=[
                {"AttributeName": "trace_id", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "trace_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


@pytest.fixture
def restore_sinks():
    """Snapshot/restore the global sink registry around each test so
    third-party registrations don't leak between tests."""
    snapshot = dict(events.SINKS)
    try:
        yield
    finally:
        events.SINKS.clear()
        events.SINKS.update(snapshot)


def test_dynamodb_sink_default(order_events_table, restore_sinks, monkeypatch):
    """events.emit() with default config writes a row to order_events."""
    monkeypatch.delenv("AWS_EXE_SYS_EVENT_SINKS", raising=False)
    events.emit(
        {
            "trace_id": "t1",
            "order_name": "o1",
            "event_type": "dispatched",
            "status": "running",
            "flow_id": "f1",
        }
    )
    rows = dynamodb.get_events(
        "t1", dynamodb_resource=order_events_table
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["trace_id"] == "t1"
    assert row["order_name"] == "o1"
    assert row["event_type"] == "dispatched"
    assert row["status"] == "running"
    assert row["flow_id"] == "f1"
    # SK format `<order_name>:<epoch>:<event_type>` is the existing contract.
    assert row["sk"].startswith("o1:")
    assert row["sk"].endswith(":dispatched")


def test_composite_sink_mirrors():
    """CompositeEventSink.emit invokes every child once with the same dict."""
    fake_a = MagicMock(spec=["emit", "name"])
    fake_a.name = "fake-a"
    fake_b = MagicMock(spec=["emit", "name"])
    fake_b.name = "fake-b"
    composite = CompositeEventSink([fake_a, fake_b])

    payload: Dict[str, Any] = {
        "trace_id": "t1",
        "order_name": "o1",
        "event_type": "dispatched",
        "status": "running",
    }
    composite.emit(payload)

    fake_a.emit.assert_called_once_with(payload)
    fake_b.emit.assert_called_once_with(payload)


def test_composite_sink_swallows_child_failure(caplog):
    """If one child raises, the remaining children are still invoked
    and no exception escapes — the critical backstop invariant."""
    failing = MagicMock(spec=["emit", "name"])
    failing.name = "boom"
    failing.emit.side_effect = RuntimeError("kaboom")

    spy = MagicMock(spec=["emit", "name"])
    spy.name = "spy"

    composite = CompositeEventSink([failing, spy])
    payload = {
        "trace_id": "t1",
        "order_name": "o1",
        "event_type": "dispatched",
        "status": "running",
    }

    # Must NOT raise.
    composite.emit(payload)

    failing.emit.assert_called_once_with(payload)
    spy.emit.assert_called_once_with(payload)


def test_third_party_cloudwatch_stub_registration(
    order_events_table, restore_sinks, monkeypatch
):
    """A third-party sink registered via register_sink() participates in
    the env-var-driven dispatch alongside the built-in dynamodb sink."""
    received: List[Dict[str, Any]] = []

    class CloudWatchSink:
        name = "cloudwatch"

        def emit(self, event: Dict[str, Any]) -> None:
            received.append(event)

    register_sink(CloudWatchSink())
    monkeypatch.setenv("AWS_EXE_SYS_EVENT_SINKS", "dynamodb,cloudwatch")

    payload = {
        "trace_id": "t1",
        "order_name": "o1",
        "event_type": "dispatched",
        "status": "running",
        "flow_id": "f1",
    }
    events.emit(payload)

    # DynamoDB row landed.
    rows = dynamodb.get_events("t1", dynamodb_resource=order_events_table)
    assert len(rows) == 1
    assert rows[0]["order_name"] == "o1"

    # Stub sink also received the event.
    assert len(received) == 1
    assert received[0] == payload


def test_unknown_sink_raises(order_events_table, restore_sinks, monkeypatch):
    """An unknown name in AWS_EXE_SYS_EVENT_SINKS raises UnknownSinkError."""
    monkeypatch.setenv("AWS_EXE_SYS_EVENT_SINKS", "dynamodb,nonexistent")
    with pytest.raises(UnknownSinkError) as exc_info:
        events.emit(
            {
                "trace_id": "t1",
                "order_name": "o1",
                "event_type": "dispatched",
                "status": "running",
            }
        )
    assert "nonexistent" in str(exc_info.value)
