"""Status constants for aws-execution-engine."""

from aws_exe_sys.orchestrator.targets import TARGETS as _TARGETS

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"

JOB_ORDER_NAME = "_job"

# EXECUTION_TARGETS is derived from the execution target registry so
# that third-party registrations (e.g. ECS, Fargate) automatically flow
# through to validation code without touching this file.
EXECUTION_TARGETS = frozenset(_TARGETS.keys())
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, TIMED_OUT})
FAILED_STATUSES = frozenset({FAILED, TIMED_OUT})
