"""Execution target protocol.

An "execution target" is the runtime that executes an order: AWS
Lambda, CodeBuild, SSM Run Command, or — via third-party registration —
any other backend (ECS, Fargate, an on-prem agent, …). Each target
implements a single ``dispatch`` method that turns an order into a
concrete execution ID.

The protocol intentionally takes no dependency on ``aws_exe_sys.common`` so
that the registry can be loaded from inside ``aws_exe_sys.common.statuses``
without circular imports.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ExecutionTarget(Protocol):
    """Structural interface for an execution backend.

    Implementations must set ``name`` to a non-empty string (used as
    the registry key and as ``Order.execution_target``) and provide a
    ``dispatch`` method that returns an execution identifier string
    (ARN, build ID, SSM command ID, etc.).
    """

    name: str

    def dispatch(self, order: Any, run_id: str, internal_bucket: str) -> str:
        """Start executing ``order`` and return the execution identifier."""
        ...
