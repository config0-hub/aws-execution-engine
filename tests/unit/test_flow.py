"""Unit tests for src/common/flow.py."""

from src.common.flow import generate_flow_id, parse_flow_id


class TestGenerateFlowId:
    def test_default_label(self):
        fid = generate_flow_id("gear", "abc123")
        assert fid == "gear:abc123-exec"

    def test_custom_label(self):
        fid = generate_flow_id("gear", "abc123", "plan")
        assert fid == "gear:abc123-plan"

    def test_format(self):
        fid = generate_flow_id("user1", "deadbeef", "deploy")
        assert ":" in fid
        assert "-" in fid


class TestParseFlowId:
    def test_roundtrip_default(self):
        fid = generate_flow_id("gear", "abc123")
        username, trace_id, flow_label = parse_flow_id(fid)
        assert username == "gear"
        assert trace_id == "abc123"
        assert flow_label == "exec"

    def test_roundtrip_custom(self):
        fid = generate_flow_id("admin", "deadbeef", "plan")
        username, trace_id, flow_label = parse_flow_id(fid)
        assert username == "admin"
        assert trace_id == "deadbeef"
        assert flow_label == "plan"

    def test_parse_known(self):
        username, trace_id, flow_label = parse_flow_id("gear:a3f7b2c1-exec")
        assert username == "gear"
        assert trace_id == "a3f7b2c1"
        assert flow_label == "exec"
