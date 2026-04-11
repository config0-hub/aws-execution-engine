"""EventSink registry.

Mirrors ``aws_exe_sys/orchestrator/targets/registry.py`` exactly so that the
events surface follows the same mental model as execution targets,
credentials, code sources, and VCS providers. Sinks are stateless
singletons; built-in sinks are seeded in :mod:`__init__` at import time
and third parties register additional sinks via :func:`register_sink`.
"""
from __future__ import annotations

from typing import Dict, List

from .base import EventSink


class UnknownSinkError(ValueError):
    """Raised when :func:`get_sink` is asked for an unregistered sink name."""


_SINKS: Dict[str, EventSink] = {}


def register_sink(sink: EventSink, *, name: str = "") -> None:
    """Register an event sink.

    ``sink`` must satisfy the :class:`EventSink` protocol. ``name``
    defaults to ``sink.name``; passing an explicit name overrides it
    (useful when registering a single implementation under multiple
    keys).
    """
    resolved = name or getattr(sink, "name", "") or ""
    if not resolved:
        raise ValueError(
            "event sink name must be a non-empty string; "
            "set the `name` class attribute or pass name=..."
        )
    _SINKS[resolved] = sink


def get_sink(name: str) -> EventSink:
    """Return the sink registered as ``name`` or raise :class:`UnknownSinkError`."""
    if name not in _SINKS:
        raise UnknownSinkError(
            f"unknown event sink {name!r}; "
            f"registered sinks: {sorted(_SINKS)}"
        )
    return _SINKS[name]


def list_sinks() -> List[str]:
    """Return the names of all registered sinks in registration order."""
    return list(_SINKS.keys())


# Public alias used by call sites that want to iterate or look up
# without importing the private dict — same shape as
# ``orchestrator.targets.TARGETS``.
SINKS = _SINKS
