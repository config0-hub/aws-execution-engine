"""Unit tests for aws_exe_sys/common/statuses.py."""

from aws_exe_sys.common.statuses import (
    QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT,
    JOB_ORDER_NAME, EXECUTION_TARGETS, TERMINAL_STATUSES, FAILED_STATUSES,
)


class TestStatuses:
    def test_status_values(self):
        assert QUEUED == "queued"
        assert RUNNING == "running"
        assert SUCCEEDED == "succeeded"
        assert FAILED == "failed"
        assert TIMED_OUT == "timed_out"

    def test_job_order_name(self):
        assert JOB_ORDER_NAME == "_job"

    def test_execution_targets(self):
        assert EXECUTION_TARGETS == frozenset({"lambda", "codebuild", "ssm"})

    def test_terminal_statuses(self):
        assert TERMINAL_STATUSES == frozenset({SUCCEEDED, FAILED, TIMED_OUT})

    def test_failed_statuses(self):
        assert FAILED_STATUSES == frozenset({FAILED, TIMED_OUT})

    def test_terminal_includes_all_failed(self):
        assert FAILED_STATUSES.issubset(TERMINAL_STATUSES)

    def test_running_not_terminal(self):
        assert RUNNING not in TERMINAL_STATUSES

    def test_queued_not_terminal(self):
        assert QUEUED not in TERMINAL_STATUSES
