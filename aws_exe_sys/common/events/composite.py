"""Composite event sink — fans out to every child, swallows failures."""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from .base import EventSink

logger = logging.getLogger(__name__)


class CompositeEventSink:
    """Fan-out sink that emits to every child in registration order.

    Semantics match Python's :mod:`logging.handlers` behaviour: one
    child failing MUST NOT prevent the other children from being
    invoked. Failures are logged via :meth:`logging.Logger.exception`
    and swallowed so the calling code never sees a sink error. This is
    the critical backstop invariant for ``events.emit`` — a misbehaving
    third-party sink can never break the built-in DynamoDB writer.
    """

    name = "composite"

    def __init__(
        self, children: List[EventSink], name: str = "composite"
    ) -> None:
        self.name = name
        self._children: List[EventSink] = list(children)

    def emit(self, event: Dict[str, Any]) -> None:
        for child in self._children:
            try:
                child.emit(event)
            except Exception:  # noqa: BLE001 — backstop invariant, see docstring
                logger.exception(
                    "EventSink child %r raised on emit; "
                    "continuing to remaining sinks",
                    getattr(child, "name", child),
                )
