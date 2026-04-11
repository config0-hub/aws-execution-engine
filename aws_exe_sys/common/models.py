"""Data models for aws-execution-engine."""

from __future__ import annotations

import base64
import json
import time
import uuid
from dataclasses import dataclass, field, fields, asdict
from typing import Any, Dict, List, Optional, Self

from aws_exe_sys.common.statuses import (
    QUEUED, RUNNING, SUCCEEDED, FAILED, TIMED_OUT,
    JOB_ORDER_NAME, EXECUTION_TARGETS,
)


class DictMixin:
    """Mixin for dataclass dict serialization with None-exclusion."""

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> Self:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


def format_order_num(index: int) -> str:
    """Format a 0-based order index as a zero-padded 4-digit string."""
    return str(index + 1).zfill(4)


# ---------------------------------------------------------------------------
# Order hierarchy
# ---------------------------------------------------------------------------


@dataclass
class BaseOrder(DictMixin):
    """Shared fields for all order types."""

    cmds: List[str]
    timeout: int
    order_name: Optional[str] = None
    git_repo: Optional[str] = None
    git_folder: Optional[str] = None
    commit_hash: Optional[str] = None
    s3_location: Optional[str] = None
    env_vars: Optional[Dict[str, str]] = None
    ssm_paths: Optional[List[str]] = None
    secret_manager_paths: Optional[List[str]] = None
    queue_id: Optional[str] = None
    dependencies: Optional[List[str]] = None
    must_succeed: bool = True
    callback_url: Optional[str] = None


@dataclass
class Order(BaseOrder):
    """Per-order fields for init_job (Lambda/CodeBuild execution)."""

    execution_target: str = "codebuild"
    sops_key: Optional[str] = None
    ssm_targets: Optional[Dict[str, Any]] = None


@dataclass
class SsmOrder(BaseOrder):
    """Per-order fields for SSM execution."""

    ssm_targets: Dict[str, Any] = field(default_factory=dict)
    ssm_document_name: Optional[str] = None


# ---------------------------------------------------------------------------
# Job hierarchy
# ---------------------------------------------------------------------------


@dataclass
class BaseJob(DictMixin):
    """Shared fields for all job types."""

    username: str
    orders: List  # Typed in subclasses
    git_provider: str = "github"
    git_ssh_key_location: Optional[str] = None
    commit_hash: Optional[str] = None
    flow_label: str = "exec"
    presign_expiry: int = 7200
    job_timeout: int = 3600

    def to_dict(self) -> dict:
        d = asdict(self)
        d["orders"] = [o.to_dict() for o in self.orders]
        return {k: v for k, v in d.items() if v is not None}

    def to_b64(self) -> str:
        return base64.b64encode(json.dumps(self.to_dict()).encode()).decode()


@dataclass
class Job(BaseJob):
    """Job for init_job Lambda (Lambda/CodeBuild orders)."""

    git_repo: str = ""
    git_token_location: str = ""
    orders: List[Order] = field(default_factory=list)
    pr_number: Optional[int] = None
    issue_number: Optional[int] = None

    @classmethod
    def from_dict(cls, data: dict) -> Job:
        orders_data = data.get("orders", [])
        orders = [Order.from_dict(o) for o in orders_data]
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known and k != "orders"}
        return cls(orders=orders, **filtered)

    @classmethod
    def from_b64(cls, b64_str: str) -> Job:
        data = json.loads(base64.b64decode(b64_str).decode())
        return cls.from_dict(data)


@dataclass
class SsmJob(BaseJob):
    """Job for ssm_config Lambda (SSM orders)."""

    git_repo: Optional[str] = None
    git_token_location: Optional[str] = None
    orders: List[SsmOrder] = field(default_factory=list)
    flow_label: str = "ssm"

    @classmethod
    def from_dict(cls, data: dict) -> SsmJob:
        orders_data = data.get("orders", [])
        orders = [SsmOrder.from_dict(o) for o in orders_data]
        known = {f.name for f in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known and k != "orders"}
        return cls(orders=orders, **filtered)

    @classmethod
    def from_b64(cls, b64_str: str) -> SsmJob:
        data = json.loads(base64.b64decode(b64_str).decode())
        return cls.from_dict(data)


# ---------------------------------------------------------------------------
# Event / Lock / OrderRecord — flat models with DictMixin
# ---------------------------------------------------------------------------


@dataclass
class OrderEvent(DictMixin):
    """Event record for the order_events DynamoDB table."""

    trace_id: str
    order_name: str
    epoch: float
    event_type: str
    status: str
    log_location: Optional[str] = None
    execution_url: Optional[str] = None
    message: Optional[str] = None
    flow_id: Optional[str] = None
    run_id: Optional[str] = None


@dataclass
class LockRecord(DictMixin):
    """Lock record for the orchestrator_locks DynamoDB table."""

    run_id: str
    orchestrator_id: str
    status: str
    acquired_at: float
    ttl: int
    flow_id: Optional[str] = None
    trace_id: Optional[str] = None


@dataclass
class OrderRecord(DictMixin):
    """DynamoDB record representation for the orders table.

    PK format: <run_id>:<order_num>
    """

    run_id: str
    order_num: str
    trace_id: str
    flow_id: str
    order_name: str
    cmds: List[str]
    status: str = QUEUED
    queue_id: Optional[str] = None
    s3_location: Optional[str] = None
    callback_url: Optional[str] = None
    execution_target: str = "codebuild"
    git_b64: Optional[str] = None
    dependencies: Optional[List[str]] = None
    must_succeed: bool = True
    timeout: int = 300
    created_at: Optional[float] = None
    last_update: Optional[float] = None
    execution_url: Optional[str] = None
    step_function_url: Optional[str] = None
    ttl: Optional[int] = None
    ssm_targets: Optional[Dict[str, Any]] = None
    ssm_document_name: Optional[str] = None
    sops_key_ssm_path: Optional[str] = None

    @property
    def pk(self) -> str:
        return f"{self.run_id}:{self.order_num}"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["pk"] = self.pk
        return {k: v for k, v in d.items() if v is not None}
