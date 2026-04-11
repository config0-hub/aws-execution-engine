"""VCS provider registry and helper facade.

Public API:

- ``VcsProvider``: abstract base class for new providers
- ``GitHubProvider``: built-in GitHub implementation
- ``VcsHelper``: facade that wraps a provider for PR comment management
- ``register_provider``: plug a new VCS provider in at runtime
- ``get_provider`` / ``list_providers``: introspect the registry
"""

from .base import VcsProvider
from .github import GitHubProvider
from .helper import VcsHelper
from .registry import (
    UnknownVcsProviderError,
    get_provider,
    list_providers,
    register_provider,
)

__all__ = [
    "VcsHelper",
    "VcsProvider",
    "GitHubProvider",
    "UnknownVcsProviderError",
    "register_provider",
    "get_provider",
    "list_providers",
]
