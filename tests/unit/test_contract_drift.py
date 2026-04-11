"""Contract-drift guards.

These tests parse documentation files and compare them to source literals, so
that silent drift between CONTRACT.md / CLAUDE.md / docs/ and the code that
produces the values is caught at CI time instead of at 3 a.m. in a DynamoDB
query.
"""

import pathlib
import re

import pytest

from aws_exe_sys.common.statuses import EXECUTION_TARGETS
from aws_exe_sys.orchestrator import lock

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
CONTRACT_MD = REPO_ROOT / "CONTRACT.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"
ARCHITECTURE_MD = REPO_ROOT / "docs" / "ARCHITECTURE.md"
DYNAMODB_PY = REPO_ROOT / "aws_exe_sys" / "common" / "dynamodb.py"

# Match `sk = "..."` or `sk = f"..."` — single-line quoted string literal.
# The optional `f` prefix lets the same regex cover both the Python f-string in
# aws_exe_sys/common/dynamodb.py and the plain literal documented in CONTRACT.md.
_SK_RE = re.compile(r'sk\s*=\s*f?"([^"]+)"')


def _read(path: pathlib.Path) -> str:
    return path.read_text()


def _extract_sk_literal(path: pathlib.Path) -> str:
    """Return the first `sk = "..."` literal found in ``path``.

    Raises AssertionError if no match — that itself is a drift signal.
    """
    text = path.read_text()
    match = _SK_RE.search(text)
    assert match is not None, (
        f"No `sk = \"...\"` literal found in {path}. "
        "Expected exactly one such line documenting / producing the SK format."
    )
    return match.group(1)


def test_event_sk_format_matches_code():
    """CONTRACT.md SK format must match the literal in put_event()."""
    contract_sk = _extract_sk_literal(CONTRACT_MD)
    code_sk = _extract_sk_literal(DYNAMODB_PY)

    assert contract_sk == code_sk, (
        "SK format drift: CONTRACT.md and aws_exe_sys/common/dynamodb.py disagree.\n"
        f"  CONTRACT.md:  {contract_sk!r}\n"
        f"  dynamodb.py:  {code_sk!r}\n"
        "Fix whichever is stale; the code is authoritative for the wire format."
    )


# ---------------------------------------------------------------------------
# New P4-4 drift guards
# ---------------------------------------------------------------------------


def test_event_ttl_value_matches_docs():
    """CLAUDE.md and docs/ARCHITECTURE.md both claim 90-day TTL; code default matches.

    The 90-day claim is reproduced in three places (two docs + one code
    literal). Any drift here means a future operator will look at one
    file, expect a different cleanup horizon than what the code emits,
    and either be paged at 3 a.m. or write a script that nukes data
    that should still exist. Make all three move together.
    """
    claude_text = _read(CLAUDE_MD)
    arch_text = _read(ARCHITECTURE_MD)
    assert "90 days" in claude_text, "CLAUDE.md missing '90 days' TTL claim"
    assert "90 days" in arch_text, (
        "docs/ARCHITECTURE.md missing '90 days' TTL claim"
    )
    code_text = DYNAMODB_PY.read_text()
    assert "86400 * 90" in code_text, (
        "aws_exe_sys/common/dynamodb.py no longer has the 86400*90 literal — "
        "if the default changed, update CLAUDE.md and docs/ARCHITECTURE.md "
        "to match"
    )


def test_lock_ttl_default_matches_code():
    """CLAUDE.md documents the lock TTL default; code is acquire_lock(..., ttl=3600)."""
    from inspect import signature

    sig = signature(lock.acquire_lock)
    default_ttl = sig.parameters["ttl"].default
    claude_text = _read(CLAUDE_MD)
    assert str(default_ttl) in claude_text, (
        f"CLAUDE.md does not mention lock TTL default ({default_ttl}s); "
        "update the 'DynamoDB Tables' section to document the actual default"
    )


@pytest.mark.parametrize(
    "env_var",
    [
        "AWS_EXE_SYS_ORDERS_TABLE",
        "AWS_EXE_SYS_ORDER_EVENTS_TABLE",
        "AWS_EXE_SYS_LOCKS_TABLE",
        "AWS_EXE_SYS_INTERNAL_BUCKET",
        "AWS_EXE_SYS_DONE_BUCKET",
        "AWS_EXE_SYS_WORKER_LAMBDA",
        "AWS_EXE_SYS_CODEBUILD_PROJECT",
        "AWS_EXE_SYS_WATCHDOG_SFN",
        "AWS_EXE_SYS_EVENTS_DIR",
        "AWS_EXE_SYS_SSM_DOCUMENT",       # added P4-4 doc fix
        "AWS_EXE_SYS_EVENT_TTL_SECONDS",  # added P4-4 doc fix
        "AWS_EXE_SYS_SSM_PREFIX",         # added P4-4 doc fix
        "AWS_EXE_SYS_EVENT_SINKS",        # added P4-1 + P4-4 doc fix
    ],
)
def test_env_var_documented_in_claude_md(env_var):
    """Every AWS_EXE_SYS_ env var used in code must be documented in CLAUDE.md."""
    claude_text = _read(CLAUDE_MD)
    assert env_var in claude_text, (
        f"Env var {env_var} is used in code but not documented in CLAUDE.md "
        f"(Environment Variables section)"
    )


def test_execution_targets_documented_match_code():
    """CONTRACT.md enumerates execution_target values; must match EXECUTION_TARGETS."""
    contract_text = _read(CONTRACT_MD)
    code_targets = set(EXECUTION_TARGETS)
    for target in code_targets:
        assert (
            f'"{target}"' in contract_text
            or f"'{target}'" in contract_text
        ), (
            f"Code has execution_target={target!r} but CONTRACT.md doesn't "
            "mention it as a quoted literal"
        )


def test_ssm_sops_key_path_format_documented():
    """The SSM SOPS key path format is a cross-service contract — must be pinned in docs."""
    code_text = (REPO_ROOT / "aws_exe_sys" / "common" / "sops.py").read_text()
    # Code has: path = f"/{prefix}/sops-keys/{run_id}/{order_num}"
    assert "sops-keys/{run_id}/{order_num}" in code_text, (
        "sops.py path format changed — update CLAUDE.md to match"
    )
    claude_text = _read(CLAUDE_MD)
    assert "sops-keys/{run_id}/{order_num}" in claude_text, (
        "CLAUDE.md must document the exact SSM SOPS key path format because "
        "consumers rely on it for out-of-band key management"
    )
