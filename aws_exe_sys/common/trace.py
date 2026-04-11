"""Trace ID generation and parsing."""

import secrets
import time


def generate_trace_id() -> str:
    """Generate a random hex trace ID (8 chars)."""
    return secrets.token_hex(4)


def create_leg(trace_id: str) -> str:
    """Create a new leg string: <trace_id>:<current_epoch_time>."""
    return f"{trace_id}:{int(time.time())}"


def parse_leg(leg_str: str) -> tuple:
    """Parse a leg string into (trace_id, epoch_time) tuple."""
    parts = leg_str.split(":", 1)
    return parts[0], int(parts[1])
