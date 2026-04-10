"""Tests for bootstrap_handler — cold start code download + delegation."""

import os
import sys
import tarfile
from unittest.mock import MagicMock, patch

import pytest

# Reset module state between tests
import src.bootstrap_handler as bootstrap_mod


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

    def test_no_url_in_event_or_env(self):
        with pytest.raises(RuntimeError, match="No engine_code_url"):
            bootstrap_mod._bootstrap({})

    def test_no_url_none_event(self):
        with pytest.raises(RuntimeError, match="No engine_code_url"):
            bootstrap_mod._bootstrap(None)

    def test_no_url_empty_env(self, monkeypatch):
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)
        monkeypatch.delenv("ENGINE_CODE_SSM_PATH", raising=False)
        with pytest.raises(RuntimeError, match="No engine_code_url"):
            bootstrap_mod._bootstrap({"other_key": "value"})


class TestBootstrapFromEvent:
    """_bootstrap downloads and extracts when URL is in event (priority 1)."""

    def test_downloads_and_extracts(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        event = {"engine_code_url": url}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        # Verify extraction
        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert os.path.isdir(os.path.join(code_dir, "bin"))
        assert bootstrap_mod._loaded is True

    def test_extends_sys_path(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        event = {"engine_code_url": url}

        original_path = sys.path.copy()
        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        assert code_dir in sys.path
        # Cleanup
        sys.path[:] = original_path

    def test_extends_os_path(self, fake_tarball, tmp_path):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        event = {"engine_code_url": url}

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap(event)

        bin_dir = os.path.join(code_dir, "bin")
        assert bin_dir in os.environ["PATH"]


class TestBootstrapFromEnv:
    """_bootstrap falls back to ENGINE_CODE_URL env var (priority 2)."""

    def test_uses_env_var(self, fake_tarball, tmp_path, monkeypatch):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        monkeypatch.setenv("ENGINE_CODE_URL", url)

        with patch.object(bootstrap_mod, "CODE_DIR", code_dir):
            bootstrap_mod._bootstrap({})

        assert os.path.isfile(os.path.join(code_dir, "my_handler.py"))
        assert bootstrap_mod._loaded is True


class TestBootstrapFromSSM:
    """_bootstrap falls back to ENGINE_CODE_SSM_PATH (priority 3)."""

    def test_reads_url_from_ssm(self, fake_tarball, tmp_path, monkeypatch):
        import base64

        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"
        url_b64 = base64.b64encode(url.encode()).decode()

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")
        monkeypatch.delenv("ENGINE_CODE_URL", raising=False)

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": url_b64}
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

    def test_ssm_not_used_when_event_url_present(self, fake_tarball, tmp_path, monkeypatch):
        """SSM should not be called when event provides URL (priority 1 wins)."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"

        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")
        event = {"engine_code_url": url}

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch.object(bootstrap_mod, "_get_presigned_url_from_ssm") as mock_ssm_fn,
        ):
            bootstrap_mod._bootstrap(event)

        mock_ssm_fn.assert_not_called()

    def test_ssm_not_used_when_env_url_present(self, fake_tarball, tmp_path, monkeypatch):
        """SSM should not be called when ENGINE_CODE_URL env is set (priority 2 wins)."""
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"

        monkeypatch.setenv("ENGINE_CODE_URL", url)
        monkeypatch.setenv("ENGINE_CODE_SSM_PATH", "/engine/code-url")

        with (
            patch.object(bootstrap_mod, "CODE_DIR", code_dir),
            patch.object(bootstrap_mod, "_get_presigned_url_from_ssm") as mock_ssm_fn,
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


class TestHandler:
    """handler() bootstraps then delegates to ENGINE_HANDLER."""

    def test_delegates_to_configured_handler(self, fake_tarball, tmp_path, monkeypatch):
        code_dir = str(tmp_path / "engine")
        url = f"file://{fake_tarball}"

        monkeypatch.setenv("ENGINE_HANDLER", "my_handler")
        monkeypatch.setenv("ENGINE_HANDLER_FUNC", "handler")

        event = {"engine_code_url": url}
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
            "src.init_job.handler", fromlist=["handler"]
        )
        assert result == {"ok": True}
