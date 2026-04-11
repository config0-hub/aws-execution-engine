"""Unit tests for the credential provider registry.

Covers the P2-1 requirements from ``plan-to-fix-gaps-04.09.2026.md``:

- Built-in ``aws_ssm`` and ``aws_secretsmanager`` providers resolve the
  documented ``aws:::ssm:`` and ``aws:::secretd:`` prefixes.
- Unknown schemes raise :class:`UnknownSchemeError`.
- Third-party providers can be registered at runtime and dispatched to
  via a custom prefix — this is the architectural point of the phase.
"""

import base64
import json
from typing import Dict, Optional
from unittest.mock import MagicMock, patch

import pytest

from aws_exe_sys.common.credentials import (
    AwsSecretsManagerProvider,
    AwsSsmProvider,
    CredentialProvider,
    UnknownSchemeError,
    fetch_location,
    get_provider,
    list_providers,
    register_provider,
    resolve_location,
)
from aws_exe_sys.common.credentials import registry as registry_module


def _encode_dict(d: dict) -> str:
    """Helper: encode a dict as base64 JSON string (simulates SSM stored value)."""
    return base64.b64encode(json.dumps(d).encode()).decode()


@pytest.fixture
def clean_registry():
    """Snapshot/restore the registry state so tests don't leak providers."""
    saved_providers = dict(registry_module._PROVIDERS)
    saved_prefixes = dict(registry_module.SCHEME_PREFIXES)
    try:
        yield
    finally:
        registry_module._PROVIDERS.clear()
        registry_module._PROVIDERS.update(saved_providers)
        registry_module.SCHEME_PREFIXES.clear()
        registry_module.SCHEME_PREFIXES.update(saved_prefixes)


# ---------------------------------------------------------------------------
# register_and_fetch — core registry contract
# ---------------------------------------------------------------------------


class TestRegisterAndFetch:
    """Validate that built-in providers are registered at import time and
    that ``fetch_location`` dispatches through the registry."""

    def test_register_and_fetch(self):
        """Both built-in providers are in the registry after import."""
        providers = list_providers()
        assert "aws_ssm" in providers
        assert "aws_secretsmanager" in providers
        assert isinstance(providers["aws_ssm"], AwsSsmProvider)
        assert isinstance(providers["aws_secretsmanager"], AwsSecretsManagerProvider)

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_aws_ssm_prefix_resolves(self, mock_boto3):
        """``aws:::ssm:`` prefix routes to the AWS SSM provider."""
        creds = {"AWS_ACCESS_KEY_ID": "AKIA...", "AWS_SECRET_ACCESS_KEY": "secret"}
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict(creds)},
        }

        result = fetch_location("aws:::ssm:/prod/aws", region="us-east-1")

        assert result == creds
        mock_boto3.client.assert_called_once_with("ssm", region_name="us-east-1")
        mock_client.get_parameter.assert_called_once_with(
            Name="/prod/aws", WithDecryption=True,
        )

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_aws_secretsmanager_prefix_resolves(self, mock_boto3):
        """``aws:::secretd:`` prefix routes to the AWS Secrets Manager provider.

        This is the bug from RESEARCH3 §4 / RESEARCH4 gap 3: today
        ``aws:::secretd:`` is documented but never parsed, so the legacy
        ``_strip_location_prefix`` would hand it to SSM and crash.
        """
        secret_payload = json.dumps({"DB_HOST": "db.internal", "DB_USER": "app"})
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": secret_payload}

        result = fetch_location(
            "aws:::secretd:prod/rds/main", region="us-east-1",
        )

        assert result == {"DB_HOST": "db.internal", "DB_USER": "app"}
        mock_boto3.client.assert_called_once_with(
            "secretsmanager", region_name="us-east-1",
        )
        mock_client.get_secret_value.assert_called_once_with(SecretId="prod/rds/main")

    def test_bare_path_defaults_to_aws_ssm(self):
        """Plain paths like ``/prod/token`` default to aws_ssm (historical)."""
        provider, path = resolve_location("/prod/creds")
        assert isinstance(provider, AwsSsmProvider)
        assert path == "/prod/creds"

    def test_empty_location_raises(self):
        with pytest.raises(UnknownSchemeError):
            resolve_location("")


# ---------------------------------------------------------------------------
# unknown scheme — hard failure, clear error
# ---------------------------------------------------------------------------


class TestUnknownScheme:
    def test_unknown_scheme_raises(self):
        """An unrecognized vendor prefix raises ``UnknownSchemeError``."""
        with pytest.raises(UnknownSchemeError) as exc:
            resolve_location("gcp:::sm:projects/foo/secrets/bar")
        msg = str(exc.value)
        assert "gcp:::sm:" in msg or "unregistered" in msg.lower()

    def test_get_provider_unknown_raises(self):
        with pytest.raises(UnknownSchemeError) as exc:
            get_provider("nonexistent_scheme")
        assert "nonexistent_scheme" in str(exc.value)


# ---------------------------------------------------------------------------
# Third-party provider registration — the architectural point
# ---------------------------------------------------------------------------


class _FakeVaultProvider(CredentialProvider):
    """Third-party stub that records all fetch calls."""

    scheme = "vault"

    def __init__(self):
        self.calls: list = []

    def fetch(
        self, path: str, region: Optional[str] = None,
    ) -> Dict[str, str]:
        self.calls.append({"path": path, "region": region})
        return {"VAULT_TOKEN": f"vault-value-for-{path}"}


class TestThirdPartyProviderRegistration:
    """Registering a third-party provider is the *point* of P2-1."""

    def test_third_party_provider_registration(self, clean_registry):
        """Register a fake ``vault`` provider and confirm dispatch works."""
        vault = _FakeVaultProvider()
        register_provider(vault, prefix="vault:::kv:")

        # Registry picks up the scheme and prefix
        assert "vault" in list_providers()

        # Prefix form dispatches to the vault provider
        result = fetch_location("vault:::kv:secret/data/myapp")
        assert result == {"VAULT_TOKEN": "vault-value-for-secret/data/myapp"}
        assert len(vault.calls) == 1
        assert vault.calls[0]["path"] == "secret/data/myapp"

        # Explicit scheme:path form also works
        result = fetch_location("vault:another/path", region="us-west-2")
        assert result == {"VAULT_TOKEN": "vault-value-for-another/path"}
        assert vault.calls[1]["path"] == "another/path"
        assert vault.calls[1]["region"] == "us-west-2"

    def test_register_provider_without_scheme_raises(self, clean_registry):
        class Bad(CredentialProvider):
            def fetch(self, path, region=None):
                return {}
        with pytest.raises(ValueError) as exc:
            register_provider(Bad())
        assert "scheme" in str(exc.value).lower()

    def test_registered_provider_cleanup_restores_state(self, clean_registry):
        """The fixture must actually restore built-in providers after test."""
        register_provider(_FakeVaultProvider(), prefix="vault:::kv:")
        # After the fixture tears down, "vault" should be gone and
        # "aws_ssm" / "aws_secretsmanager" should remain.
        # (Nothing to assert here — the next test will.)


class TestRegistryIsolationAcrossTests:
    """Run after the third-party test to verify fixture cleanup worked."""

    def test_vault_is_not_leaked(self):
        assert "vault" not in list_providers()
        assert "aws_ssm" in list_providers()
        assert "aws_secretsmanager" in list_providers()


# ---------------------------------------------------------------------------
# AwsSsmProvider — direct coverage for parsing errors
# ---------------------------------------------------------------------------


class TestAwsSsmProvider:
    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_fetch_returns_decoded_dict(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict({"K": "V"})},
        }
        provider = AwsSsmProvider()
        assert provider.fetch("/p") == {"K": "V"}

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_invalid_base64_raises_with_path(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": "not-base64-!!!"}
        }
        provider = AwsSsmProvider()
        with pytest.raises(ValueError) as exc:
            provider.fetch("/bad")
        msg = str(exc.value)
        assert "/bad" in msg
        assert "base64" in msg.lower()

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_non_dict_json_raises(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict([1, 2, 3])}  # type: ignore[list-item]
        }
        provider = AwsSsmProvider()
        with pytest.raises(ValueError) as exc:
            provider.fetch("/list")
        assert "/list" in str(exc.value)


# ---------------------------------------------------------------------------
# AwsSecretsManagerProvider — direct coverage for the two payload shapes
# ---------------------------------------------------------------------------


class TestAwsSecretsManagerProvider:
    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_json_dict_expansion(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": json.dumps({"USER": "app", "PASS": "s3cret"}),
        }
        provider = AwsSecretsManagerProvider()
        assert provider.fetch("prod/db") == {"USER": "app", "PASS": "s3cret"}

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_plain_string_uses_path_key(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": "ghp_plain_token",
        }
        provider = AwsSecretsManagerProvider()
        assert provider.fetch("prod/github-token") == {
            "GITHUB_TOKEN": "ghp_plain_token",
        }

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_json_list_uses_path_key(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": '["a", "b"]',
        }
        provider = AwsSecretsManagerProvider()
        assert provider.fetch("prod/things-list") == {
            "THINGS_LIST": '["a", "b"]',
        }
