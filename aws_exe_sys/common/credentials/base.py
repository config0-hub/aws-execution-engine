"""Abstract base class and errors for credential providers."""

from abc import ABC, abstractmethod
from typing import Dict, Optional


class UnknownSchemeError(ValueError):
    """Raised when a credential location references an unregistered scheme."""


class CredentialProvider(ABC):
    """Pluggable backend for fetching secrets.

    Each subclass binds a ``scheme`` (e.g. ``"aws_ssm"``) to a concrete
    fetch implementation. Schemes are referenced in credential locations
    using the ``vendor:::scheme_prefix:path`` format — see
    ``registry.SCHEME_PREFIXES`` for the canonical mapping.

    Example:
        class VaultProvider(CredentialProvider):
            scheme = "vault"

            def fetch(self, path, region=None):
                client = hvac.Client(...)
                return client.secrets.kv.read_secret(path)["data"]["data"]
    """

    scheme: str = ""

    @abstractmethod
    def fetch(
        self, path: str, region: Optional[str] = None,
    ) -> Dict[str, str]:
        """Fetch a credential by path and return a dict of env var key/value.

        Args:
            path: Backend-specific identifier (SSM parameter name, secret
                ARN or name, etc). Must already have any ``vendor:::scheme:``
                prefix stripped — see ``resolve_location``.
            region: Optional AWS region (ignored by non-AWS providers).

        Returns:
            A dict of string keys to string values. Providers that expose
            a single value should synthesize a key — typically derived from
            the last path segment — so callers always get a uniform shape.
        """
        ...
