"""Cycle detection tests for aws_exe_sys/init_job/validate.py.

Cyclic dependencies deadlock the evaluator (ready=[], failed_deps=[]), so we
reject them at submission time.
"""

from aws_exe_sys.common.models import Job, Order
from aws_exe_sys.init_job.validate import validate_orders


def _job(orders):
    return Job(
        orders=orders,
        git_repo="org/repo",
        git_token_location="aws:::ssm:/token",
        username="testuser",
    )


def _order(name, dependencies=None):
    return Order(
        cmds=["echo hi"],
        timeout=300,
        order_name=name,
        dependencies=dependencies or [],
    )


def test_direct_cycle():
    """A -> B -> A — simple two-node cycle."""
    job = _job([
        _order("A", dependencies=["B"]),
        _order("B", dependencies=["A"]),
    ])
    errors = validate_orders(job)
    assert errors
    assert any("cyclic" in e.lower() for e in errors), errors


def test_indirect_cycle():
    """A -> B -> C -> A — three-node cycle."""
    job = _job([
        _order("A", dependencies=["B"]),
        _order("B", dependencies=["C"]),
        _order("C", dependencies=["A"]),
    ])
    errors = validate_orders(job)
    assert errors
    assert any("cyclic" in e.lower() for e in errors), errors


def test_self_loop():
    """A -> A — order depends on itself."""
    job = _job([_order("A", dependencies=["A"])])
    errors = validate_orders(job)
    assert errors
    assert any("cyclic" in e.lower() for e in errors), errors


def test_diamond_no_cycle():
    """A -> B, A -> C, B -> D, C -> D — diamond DAG, no cycle."""
    job = _job([
        _order("A"),
        _order("B", dependencies=["A"]),
        _order("C", dependencies=["A"]),
        _order("D", dependencies=["B", "C"]),
    ])
    errors = validate_orders(job)
    assert errors == []


def test_linear_chain_no_cycle():
    """A -> B -> C — linear chain, no cycle."""
    job = _job([
        _order("A"),
        _order("B", dependencies=["A"]),
        _order("C", dependencies=["B"]),
    ])
    errors = validate_orders(job)
    assert errors == []


def test_independent_orders_no_cycle():
    """Two orders with no dependency relationship — no cycle."""
    job = _job([
        _order("A"),
        _order("B"),
    ])
    errors = validate_orders(job)
    assert errors == []
