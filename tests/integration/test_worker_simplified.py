"""Integration tests for worker handler — full pipeline with mocked AWS SDK.

Exercises: handler() → SimplePayload.from_dict() → validate() → run() →
fetch_code_s3() → handle_sops() → run_commands() → write_result().

AWS SDK calls (S3 get_object/put_object, SSM get_parameter) are mocked at the
boto3.client level.  The SOPS CLI is a stand-in ``sops`` script placed first
on PATH.  run_commands() executes REAL shell commands — that's the
integration boundary.

Note: worker/run.py imports boto3 lazily inside fetch_code_s3(), so we
cannot patch "aws_exe_sys.worker.run.boto3".  Instead we patch
"boto3.client" globally and dispatch by service name.
"""

import base64
import io
import json
import os
from pathlib import Path
import stat
from unittest.mock import MagicMock, patch
import urllib.error
import zipfile

import pytest

from aws_exe_sys.worker.handler import handler


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_event(**overrides) -> dict:
    """Build a valid direct-invoke event dict with the 8 required fields."""
    defaults = {
        "trigger_id": "trg-wkr-001",
        "s3_package_uri": "s3://test-bucket/exec/trg-wkr-001/exec.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-wkr-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return defaults


@pytest.fixture
def code_zip(tmp_path):
    """Create a real zip file containing a simple script."""
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/bash\necho 'script ran'\n")

    zip_path = tmp_path / "exec.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(script, "run.sh")

    return str(zip_path)


@pytest.fixture
def code_zip_with_secrets(tmp_path):
    """Create a zip file containing a script and a secrets.enc.json placeholder."""
    script = tmp_path / "run.sh"
    script.write_text("#!/bin/bash\necho $MY_SECRET\n")

    secrets = tmp_path / "secrets.enc.json"
    secrets.write_text('{"MY_SECRET": "ENC[encrypted-value]"}')

    zip_path = tmp_path / "exec.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(script, "run.sh")
        zf.write(secrets, "secrets.enc.json")

    return str(zip_path)


def _mock_s3_get_object(zip_path: str):
    """Return a side_effect for s3.get_object whose Body streams the real zip."""
    def _get_object(Bucket, Key):
        return {"Body": io.BytesIO(Path(zip_path).read_bytes())}
    return _get_object


@pytest.fixture
def fake_sops(tmp_path, monkeypatch):
    """Install a stand-in ``sops`` first on PATH.

    The returned function sets the JSON the stand-in prints. Every invocation
    appends its argv to ``sops.args``; the stand-in refuses (exit 3) when
    SOPS_AGE_KEY or SOPS_AGE_KEY_FILE is set and ``sops.expect_no_age_key``
    exists, so a test can assert the KMS path passed no age key.
    """
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    args_log = tmp_path / "sops.args"
    output_file = tmp_path / "sops.out"
    no_age_key_flag = tmp_path / "sops.expect_no_age_key"
    script = fake_bin / "sops"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "$@" >> "{args_log}"\n'
        f'if [ -e "{no_age_key_flag}" ] && [ -n "$SOPS_AGE_KEY$SOPS_AGE_KEY_FILE" ]; then exit 3; fi\n'
        f'cat "{output_file}"\n'
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")

    def _configure(decrypted: dict, *, expect_no_age_key: bool = False) -> dict:
        output_file.write_text(json.dumps(decrypted))
        if expect_no_age_key:
            no_age_key_flag.touch()
        return {"args": args_log}

    return _configure


def _make_boto3_dispatcher(service_mocks: dict):
    """Return a side_effect for boto3.client() that dispatches by service name."""
    def _client(service_name, **kwargs):
        if service_name in service_mocks:
            return service_mocks[service_name]
        return MagicMock()
    return _client


class TestWorkerHappyPath:
    """Happy path: download zip, no SOPS, run real commands, write result."""

    def test_echo_command_succeeds(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                commands_b64=_b64_cmds(["echo integration-test-output"]),
            ))

        assert result["status"] == "succeeded"
        mock_s3.put_object.assert_called_once()
        put_kwargs = mock_s3.put_object.call_args[1]
        assert put_kwargs["Bucket"] == "done-bucket"
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "succeeded"
        assert body["trigger_id"] == "trg-wkr-001"
        assert len(body["steps"]) == 1
        assert body["steps"][0]["status"] == "succeeded"
        assert "integration-test-output" in body["steps"][0]["output"]

    def test_multiple_commands_succeed(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                commands_b64=_b64_cmds(["echo step1", "echo step2", "echo step3"]),
            ))

        assert result["status"] == "succeeded"
        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert len(body["steps"]) == 3
        assert all(s["status"] == "succeeded" for s in body["steps"])


class TestWorkerCallback:
    """callback_url / callback_token end-to-end: event -> SimplePayload -> run() -> POST."""

    def test_callback_posted_with_bearer_token_after_marker_write(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with (
            patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})),
            patch("aws_exe_sys.common.callback.urllib.request.urlopen") as mock_urlopen,
        ):
            result = handler(_valid_event(
                commands_b64=_b64_cmds(["echo integration-test-output"]),
                callback_url="https://caller.example.com/hooks/done",
                callback_token="tok-abc",
            ))

        assert result["status"] == "succeeded"
        mock_s3.put_object.assert_called_once()
        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://caller.example.com/hooks/done"
        assert request.get_header("Authorization") == "Bearer tok-abc"

        marker_body = json.loads(mock_s3.put_object.call_args[1]["Body"].decode())
        callback_body = json.loads(request.data.decode())
        assert callback_body == marker_body

    def test_absent_callback_url_no_post_attempted(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with (
            patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})),
            patch("aws_exe_sys.common.callback.urllib.request.urlopen") as mock_urlopen,
        ):
            result = handler(_valid_event(commands_b64=_b64_cmds(["echo no-callback"])))

        assert result["status"] == "succeeded"
        mock_urlopen.assert_not_called()

    def test_callback_post_failure_does_not_affect_execution_result(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with (
            patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})),
            patch("aws_exe_sys.common.callback.urllib.request.urlopen") as mock_urlopen,
        ):
            mock_urlopen.side_effect = urllib.error.URLError("connection refused")

            result = handler(_valid_event(
                commands_b64=_b64_cmds(["echo callback-fails"]),
                callback_url="https://caller.example.com/hooks/done",
                callback_token="tok-abc",
            ))

        assert result["status"] == "succeeded"
        mock_s3.put_object.assert_called_once()
        marker_body = json.loads(mock_s3.put_object.call_args[1]["Body"].decode())
        assert marker_body["status"] == "succeeded"


class TestWorkerSopsSSMPath:
    """age + SSM path: fetch key from SSM, decrypt secrets, run commands."""

    def test_ssm_sops_happy_path(self, code_zip_with_secrets, fake_sops):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip_with_secrets)
        mock_s3.put_object.return_value = {}

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "AGE-SECRET-KEY-1FAKE"}
        }
        mock_ssm.exceptions.ParameterNotFound = type("ParameterNotFound", (Exception,), {})
        mock_ssm.delete_parameter.return_value = {}

        sops_calls = fake_sops({"MY_SECRET": "decrypted-value"})

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({
                    "s3": mock_s3, "ssm": mock_ssm})):
            result = handler(_valid_event(
                sops_type="ssm",
                sops_path="/exe-sys/sops-keys/run1/001",
                commands_b64=_b64_cmds(["echo $MY_SECRET"]),
            ))

        assert result["status"] == "succeeded"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/exe-sys/sops-keys/run1/001", WithDecryption=True
        )
        sops_invocations = sops_calls["args"].read_text().splitlines()
        assert len(sops_invocations) == 1
        assert "--decrypt" in sops_invocations[0]

        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "succeeded"
        assert "decrypted-value" in body["steps"][0]["output"]


class TestWorkerSopsKMSPath:
    """KMS path: SOPS decrypts using KMS ARN embedded in file metadata."""

    def test_kms_sops_happy_path(self, code_zip_with_secrets, fake_sops):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip_with_secrets)
        mock_s3.put_object.return_value = {}

        # The stand-in exits 3 if the KMS path passes SOPS_AGE_KEY or SOPS_AGE_KEY_FILE.
        sops_calls = fake_sops({"KMS_SECRET": "kms-decrypted"}, expect_no_age_key=True)

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                sops_type="kms",
                sops_path=None,
                commands_b64=_b64_cmds(["echo kms-test"]),
            ))

        assert result["status"] == "succeeded"
        assert len(sops_calls["args"].read_text().splitlines()) == 1


class TestWorkerNoEncryption:
    """No encryption path: sops_type=None, skip SOPS entirely."""

    def test_no_sops_skips_decryption(self, code_zip, fake_sops):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        sops_calls = fake_sops({"NEVER": "used"})

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                sops_type=None,
                sops_path=None,
                commands_b64=_b64_cmds(["echo no-sops"]),
            ))

        assert result["status"] == "succeeded"
        assert not sops_calls["args"].exists()


class TestWorkerS3DownloadFailure:
    """S3 download failure → failed result written to done_endpoint."""

    def test_download_failure_writes_failed_result(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("NoSuchKey: bucket not found")
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event())

        assert result["status"] == "failed"
        mock_s3.put_object.assert_called_once()
        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "failed"
        assert "NoSuchKey" in body["error"]
        assert body["steps"] == []


class TestWorkerSopsKeyExpired:
    """SOPS key expired → SopsKeyExpired → specific failed result."""

    def test_expired_key_writes_sops_key_expired_error(self, code_zip_with_secrets):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip_with_secrets)
        mock_s3.put_object.return_value = {}

        mock_ssm = MagicMock()
        pnf = type("ParameterNotFound", (Exception,), {})
        mock_ssm.exceptions.ParameterNotFound = pnf
        mock_ssm.get_parameter.side_effect = pnf("key expired")

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({
                    "s3": mock_s3, "ssm": mock_ssm})):
            result = handler(_valid_event(
                sops_type="ssm",
                sops_path="/exe-sys/sops-keys/expired/001",
            ))

        assert result["status"] == "failed"
        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "failed"
        assert "sops_key_expired" in body["error"]


class TestWorkerCommandFailure:
    """Command failure → partial steps, failed status."""

    def test_first_command_fails(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                commands_b64=_b64_cmds(["exit 1"]),
            ))

        assert result["status"] == "failed"
        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "failed"
        assert len(body["steps"]) == 1
        assert body["steps"][0]["status"] == "failed"
        assert body["steps"][0]["exit_code"] == 1

    def test_second_command_fails_with_partial_steps(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            result = handler(_valid_event(
                commands_b64=_b64_cmds(["echo step1-ok", "exit 42", "echo unreachable"]),
            ))

        assert result["status"] == "failed"
        put_kwargs = mock_s3.put_object.call_args[1]
        body = json.loads(put_kwargs["Body"].decode())
        assert body["status"] == "failed"
        assert len(body["steps"]) == 2  # Third command never ran
        assert body["steps"][0]["status"] == "succeeded"
        assert body["steps"][0]["output"].strip() == "step1-ok"
        assert body["steps"][1]["status"] == "failed"
        assert body["steps"][1]["exit_code"] == 42


class TestWorkerAlwaysWritesResult:
    """Key invariant: result is ALWAYS written to done_endpoint."""

    def test_result_written_on_download_failure(self):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = Exception("boom")
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            handler(_valid_event())

        mock_s3.put_object.assert_called_once()

    def test_result_written_on_success(self, code_zip):
        mock_s3 = MagicMock()
        mock_s3.get_object.side_effect = _mock_s3_get_object(code_zip)
        mock_s3.put_object.return_value = {}

        with patch("boto3.client", side_effect=_make_boto3_dispatcher({"s3": mock_s3})):
            handler(_valid_event(commands_b64=_b64_cmds(["echo ok"])))

        mock_s3.put_object.assert_called_once()
