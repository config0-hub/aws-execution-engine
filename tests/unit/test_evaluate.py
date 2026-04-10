"""Unit tests for src/orchestrator/evaluate.py."""

import pytest

from src.common.models import QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT
from src.orchestrator.evaluate import evaluate_orders


def _order(queue_id, status=QUEUED, deps=None, must_succeed=True):
    return {
        "order_num": queue_id,
        "queue_id": queue_id,
        "order_name": queue_id,
        "status": status,
        "dependencies": deps or [],
        "must_succeed": must_succeed,
    }


class TestEvaluateOrders:
    def test_no_deps_are_ready(self):
        orders = [_order("a"), _order("b")]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 2
        assert len(failed) == 0
        assert len(waiting) == 0

    def test_all_deps_succeeded_makes_ready(self):
        orders = [
            _order("a", status=SUCCEEDED),
            _order("b", status=SUCCEEDED),
            _order("c", deps=["a", "b"]),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 1
        assert ready[0]["queue_id"] == "c"

    def test_failed_dep_with_must_succeed_fails_order(self):
        orders = [
            _order("a", status=FAILED),
            _order("b", deps=["a"], must_succeed=True),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 0
        assert len(failed) == 1
        assert failed[0]["queue_id"] == "b"

    def test_running_dep_means_wait(self):
        orders = [
            _order("a", status=RUNNING),
            _order("b", deps=["a"]),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 0
        assert len(waiting) == 1
        assert waiting[0]["queue_id"] == "b"

    def test_timed_out_dep_fails_dependent(self):
        orders = [
            _order("a", status=TIMED_OUT),
            _order("b", deps=["a"], must_succeed=True),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(failed) == 1
        assert failed[0]["queue_id"] == "b"

    def test_failed_dep_with_must_succeed_false_is_ready(self):
        orders = [
            _order("a", status=FAILED),
            _order("b", deps=["a"], must_succeed=False),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 1
        assert ready[0]["queue_id"] == "b"

    def test_already_running_orders_skipped(self):
        orders = [
            _order("a", status=RUNNING),
            _order("b", status=SUCCEEDED),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        # Neither should appear in any list
        assert len(ready) == 0
        assert len(failed) == 0
        assert len(waiting) == 0

    def test_mixed_deps_partial_succeeded(self):
        orders = [
            _order("a", status=SUCCEEDED),
            _order("b", status=RUNNING),
            _order("c", deps=["a", "b"]),
        ]
        ready, failed, waiting = evaluate_orders(orders)
        assert len(ready) == 0
        assert len(waiting) == 1
