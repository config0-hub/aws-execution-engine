"""Unit tests for aws_exe_sys/common/payload.py."""

import base64
import json

import pytest

from aws_exe_sys.common.payload import PayloadValidationError, SimplePayload


def _b64_cmds(cmds: list[str]) -> str:
    """Helper: base64-encode a JSON array of commands."""
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_payload(**overrides) -> SimplePayload:
    """Return a valid SimplePayload, with optional field overrides."""
    defaults = {
        "trigger_id": "trg-001",
        "s3_package_uri": "s3://my-bucket/exec/trg-001/exec.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return SimplePayload(**defaults)


class TestSimplePayloadValidPayloads:
    def test_valid_no_sops(self):
        p = _valid_payload()
        p.validate()  # should not raise

    def test_valid_sops_ssm(self):
        p = _valid_payload(sops_type="ssm", sops_path="/exe-sys/sops-keys/run1/001")
        p.validate()

    def test_valid_sops_kms(self):
        p = _valid_payload(sops_type="kms", sops_path=None)
        p.validate()

    def test_valid_execution_targets(self):
        for target in ("lambda", "codebuild"):
            p = _valid_payload(execution_target=target)
            p.validate()


class TestSimplePayloadFromDict:
    def test_round_trip(self):
        data = {
            "trigger_id": "t-99",
            "s3_package_uri": "s3://b/k",
            "sops_type": "kms",
            "sops_path": "/key/path",
            "commands_b64": _b64_cmds(["ls"]),
            "done_endpoint": "s3://done/result",
            "execution_target": "codebuild",
        "timeout_seconds": 3600,
        }
        p = SimplePayload.from_dict(data)
        assert p.trigger_id == "t-99"
        assert p.execution_target == "codebuild"
        assert p.sops_type == "kms"

    def test_missing_keys_default_to_empty(self):
        p = SimplePayload.from_dict({})
        assert p.trigger_id == ""
        assert p.sops_type is None


class TestSimplePayloadNullSopsCoercion:
    """The null SOPS path: absent/placeholder sops values collapse to None.

    The dispatcher stringifies every field for the Lambda/CodeBuild
    transports, so a None sops_type arrives at the worker as "" and a JSON
    caller may send the literal "null". The three-path lifecycle keys on
    ``sops_type is None`` (skip decrypt) — these placeholders must coerce back.
    """

    def test_empty_string_sops_type_becomes_none(self):
        p = SimplePayload.from_dict(_dict_with(sops_type="", sops_path=""))
        assert p.sops_type is None
        assert p.sops_path is None
        p.validate()  # null path validates with no sops_path required

    def test_literal_null_string_becomes_none(self):
        p = SimplePayload.from_dict(_dict_with(sops_type="null"))
        assert p.sops_type is None
        p.validate()

    def test_literal_none_string_becomes_none(self):
        p = SimplePayload.from_dict(_dict_with(sops_type="None"))
        assert p.sops_type is None

    def test_whitespace_sops_type_becomes_none(self):
        p = SimplePayload.from_dict(_dict_with(sops_type="  "))
        assert p.sops_type is None

    def test_real_sops_type_preserved(self):
        p = SimplePayload.from_dict(_dict_with(sops_type="ssm", sops_path="/key"))
        assert p.sops_type == "ssm"
        assert p.sops_path == "/key"


def _dict_with(**overrides) -> dict:
    data = {
        "trigger_id": "trg-001",
        "s3_package_uri": "s3://my-bucket/exec/trg-001/exec.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    data.update(overrides)
    return data


class TestSimplePayloadInvalidTriggerID:
    def test_empty_trigger_id(self):
        p = _valid_payload(trigger_id="")
        with pytest.raises(PayloadValidationError, match="trigger_id"):
            p.validate()


class TestSimplePayloadInvalidS3URI:
    def test_missing_scheme(self):
        p = _valid_payload(s3_package_uri="my-bucket/key")
        with pytest.raises(PayloadValidationError, match="s3_package_uri"):
            p.validate()

    def test_no_key_after_bucket(self):
        p = _valid_payload(s3_package_uri="s3://bucket")
        with pytest.raises(PayloadValidationError, match="s3_package_uri"):
            p.validate()

    def test_no_bucket(self):
        p = _valid_payload(s3_package_uri="s3:///key")
        with pytest.raises(PayloadValidationError, match="s3_package_uri"):
            p.validate()


class TestSimplePayloadInvalidSopsType:
    def test_unknown_sops_type(self):
        p = _valid_payload(sops_type="gpg")
        with pytest.raises(PayloadValidationError, match="sops_type"):
            p.validate()

    def test_sops_ssm_requires_path(self):
        p = _valid_payload(sops_type="ssm", sops_path=None)
        with pytest.raises(PayloadValidationError, match="sops_path"):
            p.validate()

    def test_sops_ssm_empty_path(self):
        p = _valid_payload(sops_type="ssm", sops_path="")
        with pytest.raises(PayloadValidationError, match="sops_path"):
            p.validate()


class TestSimplePayloadInvalidCommandsB64:
    def test_empty_commands(self):
        p = _valid_payload(commands_b64="")
        with pytest.raises(PayloadValidationError, match="commands_b64"):
            p.validate()

    def test_not_base64(self):
        p = _valid_payload(commands_b64="!!!not-base64!!!")
        with pytest.raises(PayloadValidationError, match="commands_b64"):
            p.validate()

    def test_not_json_array(self):
        raw = base64.b64encode(b'"just a string"').decode()
        p = _valid_payload(commands_b64=raw)
        with pytest.raises(PayloadValidationError, match="JSON array"):
            p.validate()

    def test_empty_array(self):
        p = _valid_payload(commands_b64=_b64_cmds([]))
        with pytest.raises(PayloadValidationError, match="non-empty array"):
            p.validate()

    def test_array_with_empty_string(self):
        p = _valid_payload(commands_b64=_b64_cmds(["echo ok", ""]))
        with pytest.raises(PayloadValidationError, match="non-empty string"):
            p.validate()

    def test_array_with_non_string(self):
        raw = base64.b64encode(json.dumps([42]).encode()).decode()
        p = _valid_payload(commands_b64=raw)
        with pytest.raises(PayloadValidationError, match="non-empty string"):
            p.validate()


class TestSimplePayloadInvalidDoneEndpoint:
    def test_invalid_done_endpoint(self):
        p = _valid_payload(done_endpoint="https://example.com/result")
        with pytest.raises(PayloadValidationError, match="done_endpoint"):
            p.validate()


class TestSimplePayloadInvalidExecutionTarget:
    def test_removed_ssm_target(self):
        p = _valid_payload(execution_target="ssm")
        with pytest.raises(PayloadValidationError, match="execution_target"):
            p.validate()

    def test_empty_target(self):
        p = _valid_payload(execution_target="")
        with pytest.raises(PayloadValidationError, match="execution_target"):
            p.validate()


class TestSimplePayloadDecodeCommands:
    def test_decode(self):
        cmds = ["echo 1", "echo 2"]
        p = _valid_payload(commands_b64=_b64_cmds(cmds))
        assert p.decode_commands() == cmds
