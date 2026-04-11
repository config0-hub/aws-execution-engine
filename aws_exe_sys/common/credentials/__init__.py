"""Credential provider registry.

Pluggable surface for fetching secrets from different backends (AWS SSM,
AWS Secrets Manager, HashiCorp Vault, etc.). The public API is:

- ``CredentialProvider``: abstract base class (subclass it to add a backend)
- ``register_provider(provider)``: plug a provider in at runtime
- ``get_provider(scheme)``: look up a registered provider
- ``resolve_location(location)``: parse a ``vendor:::scheme:path`` URI and
  return the matching provider + path
- ``fetch_location(location, region=None)``: one-shot parse + fetch

Built-in schemes: ``aws_ssm`` (via ``aws:::ssm:``) and ``aws_secretsmanager``
(via ``aws:::secretd:``). Both are registered at import time.
"""

from .base import CredentialProvider, UnknownSchemeError
from .registry import (
    fetch_location,
    get_provider,
    list_providers,
    register_provider,
    resolve_location,
)
from .aws_ssm import AwsSsmProvider
from .aws_secretsmanager import AwsSecretsManagerProvider

# Seed the registry with the built-in providers.
register_provider(AwsSsmProvider())
register_provider(AwsSecretsManagerProvider())

__all__ = [
    "CredentialProvider",
    "UnknownSchemeError",
    "AwsSsmProvider",
    "AwsSecretsManagerProvider",
    "register_provider",
    "get_provider",
    "list_providers",
    "resolve_location",
    "fetch_location",
]
