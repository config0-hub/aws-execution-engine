"""Code source protocol — structural interface for order code providers.

A "code source" is the thing that materializes an order's code onto the
local filesystem before it's bundled into an exec.zip. There are three
built-in kinds — git, s3, commands-only — and the registry is open for
extension so callers can plug in (for example) OCI artifact pulls or
local-tarball sources without touching any of the call sites.

Sources are created fresh per job run by the registry, so it is safe to
stash per-run state on the instance (e.g. cached clones, resolved
credentials). Cleanup happens via ``cleanup()``.
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class CodeSource(Protocol):
    """Structural interface for a code source.

    Implementations must set ``kind`` as a non-empty class attribute and
    provide ``detect``, ``fetch``, and ``cleanup`` methods.
    """

    kind: str

    def detect(self, order: Any, job: Any) -> bool:
        """Return True if this source can handle ``order`` for ``job``."""
        ...

    def fetch(self, order: Any, job: Any) -> str:
        """Materialize the order's code and return the local directory path.

        The returned directory is considered owned by the caller — the
        caller may mutate it (e.g. OrderBundler writes files in-place)
        and is responsible for its final disposal, with the exception
        of any per-source shared state that ``cleanup()`` will wipe.
        """
        ...

    def cleanup(self) -> None:
        """Release any shared state accumulated across fetch() calls.

        Called exactly once per job run, after all orders have been
        fetched and processed.
        """
        ...
