"""Execution target registry.

Third parties register new execution backends (ECS, Fargate, on-prem
agents, etc.) at runtime. ``aws_exe_sys/orchestrator/dispatch.py`` looks up
targets by ``order.execution_target`` and calls ``dispatch()`` on the
result — no if/elif chain.

Targets are stateless, so instances are stored directly in the
registry. Ordering is preserved by dict insertion order; the built-in
targets (``lambda``, ``codebuild``, ``ssm``) are seeded in
``__init__.py`` when the package is imported.
"""

from typing import Dict, List

from .base import ExecutionTarget


class UnknownTargetError(ValueError):
    """Raised when ``dispatch`` is asked to run an unregistered target."""


_TARGETS: Dict[str, ExecutionTarget] = {}


def register_target(target: ExecutionTarget, *, name: str = "") -> None:
    """Register an execution target.

    ``target`` must satisfy the :class:`ExecutionTarget` protocol.
    ``name`` defaults to ``target.name``; passing an explicit name
    overrides it (useful when registering a single implementation
    under multiple keys).
    """
    resolved = name or getattr(target, "name", "") or ""
    if not resolved:
        raise ValueError(
            "execution target name must be a non-empty string; "
            "set the `name` class attribute or pass name=..."
        )
    _TARGETS[resolved] = target


def get_target(name: str) -> ExecutionTarget:
    """Return the target registered as ``name`` or raise UnknownTargetError."""
    if name not in _TARGETS:
        raise UnknownTargetError(
            f"unknown execution_target {name!r}; "
            f"registered targets: {sorted(_TARGETS)}"
        )
    return _TARGETS[name]


def list_targets() -> List[str]:
    """Return the names of all registered targets in registration order."""
    return list(_TARGETS.keys())


# Public alias used by ``aws_exe_sys.common.statuses`` and ``aws_exe_sys.orchestrator.dispatch``
# so callers can iterate or look up without importing the private dict.
TARGETS = _TARGETS
