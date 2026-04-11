"""Code source registry — extension point for third-party providers.

Sources are registered as *factories* (usually the class itself) so
that each job run gets a fresh instance. This matters for stateful
sources like :class:`GitCodeSource`, which caches clone directories
across multiple ``fetch()`` calls within one run.

Typical lifecycle inside a repackage pass:

    sources = new_sources()
    try:
        for order in orders:
            kind = detect_kind(order, job, sources)
            code_dir = sources[kind].fetch(order, job)
            ...
    finally:
        for source in sources.values():
            source.cleanup()
"""

from typing import Any, Callable, Dict, Optional

from aws_exe_sys.common.code_sources.base import CodeSource


class UnknownCodeSourceError(ValueError):
    """Raised when no registered source matches an order."""


_FACTORIES: Dict[str, Callable[[], CodeSource]] = {}


def register_code_source(
    factory: Callable[[], CodeSource],
    *,
    kind: Optional[str] = None,
) -> None:
    """Register a code source factory.

    The factory is called zero-arg and must return a fresh
    ``CodeSource`` instance.

    Args:
        factory: A zero-arg callable returning a new ``CodeSource``
            instance. When registering a class, pass the class itself
            (classes are callables that produce instances).
        kind: Optional override for the source kind. If omitted, it is
            read from ``factory.kind`` — which works when ``factory`` is
            a class with a ``kind`` class attribute.

    Raises:
        ValueError: if no kind can be determined (neither ``kind``
            argument nor ``factory.kind`` attribute).
    """
    resolved = kind or getattr(factory, "kind", "") or ""
    if not resolved:
        raise ValueError(
            "code source kind must be a non-empty string "
            "(set a 'kind' class attribute or pass kind=...)"
        )
    _FACTORIES[resolved] = factory


def list_code_sources() -> Dict[str, Callable[[], CodeSource]]:
    """Return a shallow copy of the registered factory map."""
    return dict(_FACTORIES)


def new_sources() -> Dict[str, CodeSource]:
    """Instantiate every registered source for one job run.

    The returned dict is insertion-ordered. :func:`detect_kind`
    iterates in the same order, so more specific sources should be
    registered first (S3 before git before commands-only).
    """
    return {kind: factory() for kind, factory in _FACTORIES.items()}


def detect_kind(order: Any, job: Any, sources: Dict[str, CodeSource]) -> str:
    """Return the kind of the first source whose ``detect()`` matches.

    Raises:
        UnknownCodeSourceError: if no source claims the order.
    """
    for kind, source in sources.items():
        if source.detect(order, job):
            return kind
    raise UnknownCodeSourceError(
        f"No registered code source matched order "
        f"{getattr(order, 'order_name', None) or '<unnamed>'}"
    )
