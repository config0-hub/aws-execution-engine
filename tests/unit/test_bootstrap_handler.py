"""Tests for bootstrap_handler — cold start code download + delegation."""

import base64
import hashlib
import json
import os
import sys
import tarfile
from unittest.mock import MagicMock, patch

import pytest

# Reset module state between tests
import aws_exe_sys.bootstrap_handler as bootstrap_mod


def _sha256_of(path) -> str:
    """Compute sha256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


@pytest.fixture(autouse=True)
def _reset_bootstrap():
    """Reset global _loaded state before each test."""
    bootstrap_mod._loaded = False
    yield
    bootstrap_mod._loaded = False


@pytest.fixture
def fake_tarball(tmp_path):
    """Create a real tar.gz with a Python module inside."""
    # Create a fake engine module
    engine_dir = tmp_path / "engine_src"
    engine_dir.mkdir()
    (engine_dir / "my_handler.py").write_text(
        'def handler(event, context): return {"statusCode": 200, "body": "ok"}\n'
    )
    (engine_dir / "bin").mkdir()
    (engine_dir / "bin" / "mytool").write_text("#!/bin/sh\necho hello\n")

    # Create tarball
    tarball_path = tmp_path / "engine-code.tar.gz"
    with tarfile.open(tarball_path, "w:gz") as tar:
        tar.add(engine_dir / "my_handler.py", arcname="my_handler.py")
        tar.add(engine_dir / "bin", arcname="bin")
        tar.add(engine_dir / "bin" / "mytool", arcname="bin/mytool")

    return tarball_path


class TestBootstrapMissingUrl:
    """_bootstrap raises when no URL is provided."""

    def test_no_url_in_event_or_env(self, monkeypatch):
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SSM_PATH", raising=False)
        with pytest.raises(RuntimeError, match="No engine code source"):
            bootstrap_mod._bootstrap({})

    def test_no_url_none_event(self, monkeypatch):
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SSM_PATH", raising=False)
        with pytest.raises(RuntimeError, match="No engine code source"):
            bootstrap_mod._bootstrap(None)

    def test_no_url_empty_env(self, monkeypatch):
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SSM_PATH", raising=False)
        with pytest.raises(RuntimeError, match="No engine code source"):
            bootstrap_mod._bootstrap({"other_key": "value"})


class TestBootstrapFromEvent:
    """_bootstrap downloads and extracts when engine_code is in event (priority 1)."""

    def test_downloads_and_extracts(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)
        event = {"engine_code": {"url": url, "sha256": sha}}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        # Verify extraction
        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert os.path.isdir(os.path.join(code_dir, "bin"))
        assert bootstrap_mod._loaded is True

    def test_extends_sys_path(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)
        event = {"engine_code": {"url": url, "sha256": sha}}

        original_path = sys.path.copy()
        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        assert code_dir in sys.path
        # Cleanup
        sys.path[:] = original_path

    def test_extends_os_path(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)
        event = {"engine_code": {"url": url, "sha256": sha}}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        bin_dir = os.path.join(code_dir, "bin")
        assert bin_dir in os.environ["PATH"]


class TestBootstrapFromEnv:
    """_bootstrap falls back to ENGINE_CODE_URL + ENGINE_CODE_SHA256 env vars (priority 2)."""

    def test_uses_env_var(self, fake_tarball, tmp_path, monkeypatch):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)
        monkeypatch.setenv("ENGINE_CODE_URL", url)
        monkeypatch.setenv("ENGINE_CODE_SHA256", sha)

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap({})

        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is True


class TestBootstrapFromSSM:
    """_bootstrap falls back to ENGINE_CODE_SSM_PATH (priority 3)."""

    def test_reads_payload_from_ssm(self, fake_tarball, tmp_path, monkeypatch):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        real_sha = _sha256_of(fake_tarball)
        payload = json.dumps({"url": url, "sha256": real_sha})
        payload_b64 = base64.b64encode(payload.encode()).decode()

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": payload_b64}
        }

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch("boto3.client", return_value=mock_ssm),
        ):
            bootstrap_mod._bootstrap({})

        mock_ssm.get_parameter.assert_called_once_with(
            Name="/engine/code-url", WithDecryption=True
        )
        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is True

    def test_ssm_not_used_when_event_payload_present(self, fake_tarball, tmp_path, monkeypatch):
        """SSM should not be called when event provides engine_code (priority 1 wins)."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")
        event = {"engine_code": {"url": url, "sha256": sha}}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch.object(bootstrap_mod, "_get_code_source_from_ssm") as mock_ssm_fn,
        ):
            bootstrap_mod._bootstrap(event)

        mock_ssm_fn.assert_not_called()

    def test_ssm_not_used_when_env_vars_present(self, fake_tarball, tmp_path, monkeypatch):
        """SSM should not be called when env-var pair is set (priority 2 wins)."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)

        monkeypatch.setenv("ENGINE_CODE_URL", url)
        monkeypatch.setenv("ENGINE_CODE_SHA256", sha)
        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch.object(bootstrap_mod, "_get_code_source_from_ssm") as mock_ssm_fn,
        ):
            bootstrap_mod._bootstrap({})

        mock_ssm_fn.assert_not_called()


class TestBootstrapSkipsWarmInvocation:
    """_bootstrap skips download on warm invocations."""

    def test_skips_when_loaded_and_dir_exists(self, tmp_path):
        code_dir = str(tmp_path / "engine")
        os.makedirs(code_dir)
        bootstrap_mod._loaded = True

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            # Should not raise even without a URL — it skips entirely
            bootstrap_mod._bootstrap({})


class TestBootstrapIntegrity:
    """SHA256 verification of downloaded tarball (P1-2)."""

    def test_sha_mismatch_raises(self, fake_tarball, tmp_path):
        """Wrong expected sha → BootstrapIntegrityError, no extraction."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        wrong_sha = "0" * 64
        event = {"engine_code": {"url": url, "sha256": wrong_sha}}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            with pytest.raises(bootstrap_mod.BootstrapIntegrityError, match="sha256 mismatch"):
                bootstrap_mod._bootstrap(event)

        # Verify nothing was extracted
        assert not os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is False

    def test_sha_match_loads(self, fake_tarball, tmp_path):
        """Correct sha → normal load succeeds."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        real_sha = _sha256_of(fake_tarball)
        event = {"engine_code": {"url": url, "sha256": real_sha}}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is True

    def test_ssm_payload_json_shape(self, fake_tarball, tmp_path, monkeypatch):
        """SSM parameter value is base64-encoded JSON with url + sha256."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        real_sha = _sha256_of(fake_tarball)
        payload = json.dumps({"url": url, "sha256": real_sha})
        payload_b64 = base64.b64encode(payload.encode()).decode()

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/exe-sys/engine-code")
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": payload_b64}}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch("boto3.client", return_value=mock_ssm),
        ):
            bootstrap_mod._bootstrap({})

        mock_ssm.get_parameter.assert_called_once_with(
            Name="/exe-sys/engine-code", WithDecryption=True
        )
        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is True

    def test_ssm_payload_legacy_rejected(self, tmp_path, monkeypatch):
        """Plain base64-encoded URL (legacy shape) must be rejected — greenfield, no compat."""
        code_dir = str(tmp_path / "engine")
        # Legacy format — just a base64'd URL, no JSON wrapper
        legacy_b64 = base64.b64encode(b"https://s3.example.com/tarball.tar.gz").decode()

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/exe-sys/engine-code")
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": legacy_b64}}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch("boto3.client", return_value=mock_ssm),
        ):
            with pytest.raises(bootstrap_mod.BootstrapIntegrityError, match="base64-encoded JSON"):
                bootstrap_mod._bootstrap({})

        assert bootstrap_mod._loaded is False

    def test_ssm_payload_json_missing_sha(self, tmp_path, monkeypatch):
        """JSON dict without sha256 field must be rejected."""
        code_dir = str(tmp_path / "engine")
        payload = json.dumps({"url": "https://s3.example.com/x.tar.gz"})
        payload_b64 = base64.b64encode(payload.encode()).decode()

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/exe-sys/engine-code")
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": payload_b64}}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch("boto3.client", return_value=mock_ssm),
        ):
            with pytest.raises(bootstrap_mod.BootstrapIntegrityError, match="sha256"):
                bootstrap_mod._bootstrap({})

    def test_event_plain_string_rejected(self, tmp_path):
        """Legacy event.engine_code_url string form must be rejected."""
        code_dir = str(tmp_path / "engine")
        event = {"engine_code_url": "https://s3.example.com/tarball.tar.gz"}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            with pytest.raises(bootstrap_mod.BootstrapIntegrityError, match="engine_code"):
                bootstrap_mod._bootstrap(event)

    def test_env_vars_require_both_url_and_sha(self, fake_tarball, tmp_path, monkeypatch):
        """ENGINE_CODE_URL without ENGINE_CODE_SHA256 (or vice versa) must error."""
        code_dir = str(tmp_path / "engine")
        monkeypatch.setenv("ENGINE_CODE_URL", f"file://{fake_tarball}")
        monkeypatch.delenv("ENGINE_CODE_SHA256", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SSM_PATH", raising=False)

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            with pytest.raises(bootstrap_mod.BootstrapIntegrityError, match="ENGINE_CODE_SHA256"):
                bootstrap_mod._bootstrap({})


class TestHandler:
    """handler() bootstraps then delegates to ENGINE_HANDLER."""

    def test_delegates_to_configured_handler(self, fake_tarball, tmp_path, monkeypatch):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        sha = _sha256_of(fake_tarball)

        monkeypatch.setenv("ENGINE_HANDLER", "my_handler")
        monkeypatch.setenv("ENGINE_HANDLER_FUNC", "handler")

        event = {"engine_code": {"url": url, "sha256": sha}}
        context = MagicMock()

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            result = bootstrap_mod.handler(event, context)

        assert result == {"statusCode": 200, "body": "ok"}

    def test_default_handler_module(self, monkeypatch):
        """Verify default ENGINE_HANDLER value when not set."""
        monkeypatch.delenv("ENGINE_HANDLER", raising=False)
        monkeypatch.delenv("ENGINE_HANDLER_FUNC", raising=False)

        # Pre-load so _bootstrap is a no-op
        bootstrap_mod._loaded = True

        mock_module = MagicMock()
        mock_module.handler.return_value = {"ok": True}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", "/tmp/fake"),
            patch("os.path.exists", return_value=True),
            patch("builtins.__import__", return_value=mock_module) as mock_import,
        ):
            result = bootstrap_mod.handler({}, MagicMock())

        # Should import the default module
        mock_import.assert_called_once_with(
            "aws_exe_sys.init_job.handler", fromlist=["handler"]
        )
        assert result == {"ok": True}
