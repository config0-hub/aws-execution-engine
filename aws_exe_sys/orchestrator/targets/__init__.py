"""Execution target registry package.

Seeds the three built-in targets (``lambda``, ``codebuild``, ``ssm``)
at import time. Third parties can call :func:`register_target` to
add new backends after the package is imported.
"""

from .base import ExecutionTarget
from .codebuild import CodeBuildTarget
from .lambda_target import LambdaTarget
from .registry import (
    TARGETS,
    UnknownTargetError,
    get_target,
    list_targets,
    register_target,
)
from .ssm import SsmTarget

# Seed the built-in targets. Order is preserved by the underlying
# dict, which matters for ``statuses.EXECUTION_TARGETS`` snapshots.
register_target(LambdaTarget())
register_target(CodeBuildTarget())
register_target(SsmTarget())

__all__ = [
    "ExecutionTarget",
    "LambdaTarget",
    "CodeBuildTarget",
    "SsmTarget",
    "TARGETS",
    "UnknownTargetError",
    "get_target",
    "list_targets",
    "register_target",
]
