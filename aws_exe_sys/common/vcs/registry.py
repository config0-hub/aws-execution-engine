"""VCS provider registry.

Job configs reference providers by short name via ``job.git_provider``
(default ``"github"``). This module holds the name → provider-instance
lookup and the public ``register_provider`` API for plugging in new
providers at runtime.

Third-party providers live outside this module — they call
``register_provider(MyProvider())`` at import time to wire themselves
into clone dispatch and VcsHelper.
"""

from typing import Dict, Optional

from .base import VcsProvider
from .github import GitHubProvider


class UnknownVcsProviderError(ValueError):
    """Raised when a job references an unregistered VCS provider."""


_PROVIDERS: Dict[str, VcsProvider] = {}


def register_provider(
    provider: VcsProvider, name: Optional[str] = None,
) -> None:
    """Register a VCS provider instance under its short name.

    Args:
        provider: Concrete ``VcsProvider`` subclass instance.
        name: Optional override for the registry key. Defaults to
            ``provider.name``.

    Raises:
        ValueError: if neither ``provider.name`` nor ``name`` is set.
    """
    key = name or provider.name
    if not key:
        raise ValueError(
            f"VCS provider {type(provider).__name__} has no 'name' and no "
            f"explicit name was passed to register_provider()."
        )
    _PROVIDERS[key] = provider


def get_provider(name: str) -> VcsProvider:
    """Look up a VCS provider by name.

    Raises:
        UnknownVcsProviderError: if no provider is registered for ``name``.
    """
    try:
        return _PROVIDERS[name]
    except KeyError:
        supported = ", ".join(sorted(_PROVIDERS)) or "<none>"
        raise UnknownVcsProviderError(
            f"No VCS provider registered for name {name!r}. "
            f"Registered providers: {supported}."
        ) from None


def list_providers() -> Dict[str, VcsProvider]:
    """Return a copy of the registered provider map (for introspection)."""
    return dict(_PROVIDERS)


# Seed the registry with the built-in GitHub provider.
register_provider(GitHubProvider())
