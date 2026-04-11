"""Versioned result schemas for worker / watchdog / init result.json payloads.

``v1`` is the current format. Future versions add a new dataclass and
bump :data:`SCHEMA_VERSION_CURRENT`. Readers dispatch on the
``schema_version`` field, so callers can never silently consume a
mismatched payload — :meth:`ResultV1.from_dict` raises
:class:`ValueError` if the version is missing or wrong.

Implementation note: this module deliberately uses a plain dataclass +
:func:`dataclasses.asdict` rather than Pydantic. The rest of the engine
uses the same ``dataclass + DictMixin`` pattern (see
``aws_exe_sys/common/models.py``) and Pydantic is not in ``requirements.txt``.
Adding a dependency for one schema would be disproportionate.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Dict

SCHEMA_VERSION_CURRENT = "v1"


@dataclass
class ResultV1:
    """``result.json`` schema v1 — worker / watchdog / init writers."""

    status: str
    log: str
    schema_version: str = SCHEMA_VERSION_CURRENT

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to a plain dict suitable for :func:`json.dumps`."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ResultV1":
        """Construct a :class:`ResultV1` from a parsed JSON dict.

        Raises :class:`ValueError` if ``schema_version`` is missing or
        does not match :data:`SCHEMA_VERSION_CURRENT` — callers must
        upgrade or migrate before reading the payload.
        """
        version = data.get("schema_version")
        if version != SCHEMA_VERSION_CURRENT:
            raise ValueError(
                f"expected result schema_version={SCHEMA_VERSION_CURRENT!r}, "
                f"got {version!r} — caller must upgrade or migrate"
            )
        return cls(
            status=data["status"],
            log=data["log"],
            schema_version=version,
        )


__all__ = ["ResultV1", "SCHEMA_VERSION_CURRENT"]
