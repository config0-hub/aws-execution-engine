"""Unit tests for aws_exe_sys/orchestrator/handler.py — lock metadata + handler flow."""

from aws_exe_sys.orchestrator import handler as orch_handler


def _s3_event(run_id, order_num="0001"):
    return {
        "Records": [
            {
                "s3": {
                    "object": {
                        "key": f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
                    }
                }
            }
        ]
    }


def test_lock_stores_flow_trace_ids(monkeypatch):
    """Handler reads flow_id / trace_id from first order and passes them to acquire_lock."""
    run_id = "run-peek"

    # Stub get_all_orders to return a single seeded order
    monkeypatch.setattr(
        orch_handler.dynamodb,
        "get_all_orders",
        lambda run_id_arg, dynamodb_resource=None: [
            {
                "order_num": "0001",
                "order_name": "order-0001",
                "flow_id": "flow-abc",
                "trace_id": "trace-xyz",
                "status": "queued",
            }
        ],
    )

    captured = {}

    def fake_acquire_lock(run_id_arg, flow_id, trace_id, dynamodb_resource=None):
        captured["run_id"] = run_id_arg
        captured["flow_id"] = flow_id
        captured["trace_id"] = trace_id
        return True

    monkeypatch.setattr(orch_handler, "acquire_lock", fake_acquire_lock)
    monkeypatch.setattr(
        orch_handler, "execute_orders", lambda run_id_arg: {"status": "ok"}
    )

    result = orch_handler.handler(_s3_event(run_id))
    assert result == {"status": "ok"}
    assert captured["run_id"] == run_id
    assert captured["flow_id"] == "flow-abc"
    assert captured["trace_id"] == "trace-xyz"


def test_handler_lock_not_acquired_skips_execute(monkeypatch):
    """When acquire_lock returns False, handler returns 'skipped' and never executes."""
    run_id = "run-blocked"

    monkeypatch.setattr(
        orch_handler.dynamodb,
        "get_all_orders",
        lambda run_id_arg, dynamodb_resource=None: [
            {"flow_id": "f", "trace_id": "t", "status": "queued"}
        ],
    )
    monkeypatch.setattr(orch_handler, "acquire_lock", lambda *a, **kw: False)

    called = {"n": 0}

    def fake_execute(run_id_arg):
        called["n"] += 1
        return {"status": "ok"}

    monkeypatch.setattr(orch_handler, "execute_orders", fake_execute)

    result = orch_handler.handler(_s3_event(run_id))
    assert result["status"] == "skipped"
    assert called["n"] == 0


def test_handler_empty_orders_still_locks(monkeypatch):
    """Handler with no orders for run_id should still attempt a lock with empty metadata."""
    run_id = "run-empty"

    monkeypatch.setattr(
        orch_handler.dynamodb,
        "get_all_orders",
        lambda run_id_arg, dynamodb_resource=None: [],
    )

    captured = {}

    def fake_acquire_lock(run_id_arg, flow_id, trace_id, dynamodb_resource=None):
        captured["flow_id"] = flow_id
        captured["trace_id"] = trace_id
        return True

    monkeypatch.setattr(orch_handler, "acquire_lock", fake_acquire_lock)
    monkeypatch.setattr(
        orch_handler, "execute_orders", lambda run_id_arg: {"status": "no_orders"}
    )

    orch_handler.handler(_s3_event(run_id))
    # With no orders, peek yields empty strings — still acceptable as a fallback.
    assert captured["flow_id"] == ""
    assert captured["trace_id"] == ""
