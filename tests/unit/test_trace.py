"""Unit tests for src/common/trace.py."""

import time

from src.common.trace import generate_trace_id, create_leg, parse_leg


class TestGenerateTraceId:
    def test_length(self):
        tid = generate_trace_id()
        assert len(tid) == 8

    def test_hex_format(self):
        tid = generate_trace_id()
        # Should be valid hex
        int(tid, 16)

    def test_unique(self):
        ids = {generate_trace_id() for _ in range(100)}
        assert len(ids) == 100


class TestCreateLeg:
    def test_format(self):
        leg = create_leg("abcd1234")
        parts = leg.split(":")
        assert len(parts) == 2
        assert parts[0] == "abcd1234"
        # epoch should be an integer
        int(parts[1])

    def test_uses_current_time(self):
        before = int(time.time())
        leg = create_leg("aaaa0000")
        after = int(time.time())
        _, epoch = parse_leg(leg)
        assert before <= epoch <= after


class TestParseLeg:
    def test_roundtrip(self):
        leg = create_leg("deadbeef")
        trace_id, epoch = parse_leg(leg)
        assert trace_id == "deadbeef"
        assert isinstance(epoch, int)

    def test_parse_known(self):
        trace_id, epoch = parse_leg("a3f7b2c1:1708099200")
        assert trace_id == "a3f7b2c1"
        assert epoch == 1708099200
