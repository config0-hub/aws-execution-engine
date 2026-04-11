"""EventSink protocol.

Third parties can register additional event sinks (CloudWatch Logs,
Kinesis, Datadog, ...) at import time via :func:`register_sink`. The
orchestrator and workers emit events through :func:`events.emit`, which
fans out to every registered sink.

The protocol mirrors the P2 ``orchestrator/targets/`` registry shape so
that the rest of the code base only has one mental model for pluggable
backends. Sinks are stateless singletons stored directly in the
registry.
"""
from __future__ import annotations

from typing import Any, Dict, Protocol, runtime_checkable


@runtime_checkable
class EventSink(Protocol):
    """Structural interface for a pluggable event sink.

    Implementations must set ``name`` to a non-empty string (used as
    the registry key) and provide an :meth:`emit` method that accepts a
    single event dict. Implementations MUST NOT raise for transient
    errors — log them and return so the composite sink can fan out to
    the remaining children. Permanent errors may raise.
    """

    name: str

    def emit(self, event: Dict[str, Any]) -> None:
        """Emit a single event dict."""
        ...
