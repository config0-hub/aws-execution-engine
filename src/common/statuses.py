"""Status constants for aws-execution-engine."""

QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"
TIMED_OUT = "timed_out"

JOB_ORDER_NAME = "_job"

EXECUTION_TARGETS = frozenset({"lambda", "codebuild", "ssm"})
TERMINAL_STATUSES = frozenset({SUCCEEDED, FAILED, TIMED_OUT})
FAILED_STATUSES = frozenset({FAILED, TIMED_OUT})
