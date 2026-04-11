"""Flow ID generation and parsing."""


def generate_flow_id(username: str, trace_id: str, flow_label: str = "exec") -> str:
    """Generate a flow ID: <username>:<trace_id>-<flow_label>."""
    return f"{username}:{trace_id}-{flow_label}"


def parse_flow_id(flow_id: str) -> tuple:
    """Parse a flow ID into (username, trace_id, flow_label) tuple."""
    username, rest = flow_id.split(":", 1)
    trace_id, flow_label = rest.rsplit("-", 1)
    return username, trace_id, flow_label
