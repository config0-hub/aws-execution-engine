"""Credential provider registry and location parsing.

Credential *locations* are ``vendor:::scheme:path`` strings. The
``vendor:::scheme:`` prefix selects a registered provider; the remainder
is handed to the provider's ``fetch()`` method verbatim.

Built-in prefixes:

    aws:::ssm:<path>          -> aws_ssm provider
    aws:::secretd:<path>      -> aws_secretsmanager provider

Third-party providers register their own prefix via ``register_provider``.

Why a prefix map (not just ``scheme:path`` parsing)? Because real-world
engine orders already use the historical ``aws:::ssm:/foo`` form, and
the ``:::`` delimiter is load-bearing for disambiguating from actual
path content.
"""

from typing import Dict, Optional, Tuple

from .base import CredentialProvider, UnknownSchemeError


# Canonical prefix → scheme mapping. Adding a prefix here is how the
# short ``aws:::ssm:`` form gets routed to the ``aws_ssm`` scheme.
SCHEME_PREFIXES: Dict[str, str] = {
    "aws:::ssm:": "aws_ssm",
    "aws:::secretd:": "aws_secretsmanager",
}

_PROVIDERS: Dict[str, CredentialProvider] = {}


def register_provider(
    provider: CredentialProvider,
    prefix: Optional[str] = None,
) -> None:
    """Register a credential provider under its scheme.

    Args:
        provider: Concrete ``CredentialProvider`` subclass instance. Must
            have a non-empty ``scheme`` attribute.
        prefix: Optional ``vendor:::scheme:`` shorthand to register in
            ``SCHEME_PREFIXES``. If omitted, only the raw scheme name is
            usable in locations — ``fetch("<scheme>:<path>")``.

    Raises:
        ValueError: if the provider's scheme is empty.
    """
    if not provider.scheme:
        raise ValueError(
            f"Provider {type(provider).__name__} has no 'scheme' attribute."
        )
    _PROVIDERS[provider.scheme] = provider
    if prefix:
        SCHEME_PREFIXES[prefix] = provider.scheme


def get_provider(scheme: str) -> CredentialProvider:
    """Look up a provider by its scheme name.

    Raises:
        UnknownSchemeError: if no provider is registered for ``scheme``.
    """
    try:
        return _PROVIDERS[scheme]
    except KeyError:
        supported = ", ".join(sorted(_PROVIDERS)) or "<none>"
        raise UnknownSchemeError(
            f"No credential provider registered for scheme {scheme!r}. "
            f"Registered schemes: {supported}."
        ) from None


def list_providers() -> Dict[str, CredentialProvider]:
    """Return a copy of the registered provider map (for introspection)."""
    return dict(_PROVIDERS)


def resolve_location(
    location: str,
) -> Tuple[CredentialProvider, str]:
    """Parse a credential location into a (provider, path) tuple.

    Accepted forms:

    1. Registered shorthand prefix: ``aws:::ssm:/foo/bar`` →
       (aws_ssm provider, ``/foo/bar``).
    2. Explicit scheme: ``<scheme>:<path>`` where ``scheme`` is any
       registered provider name.
    3. Bare path (no ``:::``, no ``:``): routed to the default ``aws_ssm``
       provider for backwards compatibility with raw SSM paths like
       ``/exec/my-creds``. This is the historical default — operators
       have been storing plain SSM paths in job configs since the
       pluggable surface did not exist.

    Raises:
        UnknownSchemeError: if the location references a scheme/prefix
            that is not registered.
    """
    if not location:
        raise UnknownSchemeError("Credential location is empty.")

    # Shorthand prefix (aws:::ssm:, aws:::secretd:, third-party, ...)
    for prefix, scheme in SCHEME_PREFIXES.items():
        if location.startswith(prefix):
            return get_provider(scheme), location[len(prefix):]

    # Explicit ``scheme:path`` — only if the scheme is actually registered.
    if ":" in location:
        head, tail = location.split(":", 1)
        if head in _PROVIDERS:
            return _PROVIDERS[head], tail
        # A ``:::`` delimiter with an unknown vendor prefix is a hard error.
        # Otherwise, a colon in the middle of an SSM path like
        # ``/foo:bar/baz`` is legal and we fall through to the bare-path
        # default below.
        if ":::" in location:
            raise UnknownSchemeError(
                f"Credential location {location!r} uses an unregistered "
                f"vendor prefix. Registered prefixes: "
                f"{', '.join(sorted(SCHEME_PREFIXES)) or '<none>'}."
            )

    # Bare path — default to aws_ssm for historical compatibility with
    # raw SSM paths in job configs.
    return get_provider("aws_ssm"), location


def fetch_location(
    location: str, region: Optional[str] = None,
) -> Dict[str, str]:
    """Parse a location, dispatch to the registered provider, return env vars."""
    provider, path = resolve_location(location)
    return provider.fetch(path, region=region)
