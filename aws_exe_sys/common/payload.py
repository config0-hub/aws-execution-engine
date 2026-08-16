"""SimplePayload — lightweight validated payload for the simplified execution path."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import re

_S3_URI_RE = re.compile(r"^s3://[a-zA-Z0-9.\-_]+/.+$")
_VALID_SOPS_TYPES = frozenset({"ssm", "kms"})
_VALID_EXECUTION_TARGETS = frozenset({"lambda", "codebuild"})


class PayloadValidationError(ValueError):
    """Raised when a SimplePayload fails validation."""


@dataclass
class SimplePayload:
    """Minimal payload for the simplified execution path.

    Fields:
        trigger_id:       Unique identifier for this execution trigger.
        s3_package_uri:   S3 URI pointing to the exec.zip package.
        sops_type:        Secrets backend — "ssm", "kms", or None.
        sops_path:        Path to the SOPS key (required when sops_type is "ssm").
        commands_b64:     Base64-encoded JSON array of shell commands.
        done_endpoint:    S3 URI where the result should be written.
        execution_target: One of "lambda" or "codebuild".
        timeout_seconds:  The execution's overall timeout in seconds. Required
                          and must be a positive integer - there is no default.
        callback_url:     Optional. When set, the worker POSTs the terminal
                          ExecutionResult here after writing the done-marker.
                          Absent = today's behavior (no callback).
        callback_token:   Optional. Sent as a bearer token on the callback
                          POST. Requires callback_url to be set.
        execution_mode:   Optional. None (absent) = engine-image mode (the
                          default). "direct" = the CodeBuild delivery runs the
                          static dispatcher-owned buildspec on
                          aws/codebuild/standard:7.0 privileged instead of the
                          engine ECR image. Dispatch-only discriminator: the
                          Step Functions Choice state reads it; the worker
                          never does. Requires execution_target "codebuild".
    """

    trigger_id: str
    s3_package_uri: str
    sops_type: str | None
    sops_path: str | None
    commands_b64: str
    done_endpoint: str
    execution_target: str
    timeout_seconds: int
    callback_url: str | None = None
    callback_token: str | None = None
    execution_mode: str | None = None

    @staticmethod
    def _coerce_null(value: object) -> str | None:
        """Coerce an absent/null SOPS value to a real ``None``.

        The dispatcher serialises every field to a string for the
        Lambda/CodeBuild transports, so a ``None`` sops_type arrives as
        ``""`` and a JSON caller may send the literal ``"null"``/``"none"``.
        The three-path lifecycle keys on ``sops_type is None`` (skip decrypt),
        so those placeholders MUST collapse back to ``None`` here — otherwise
        the null path validates and decrypts as if a backend were named.
        """
        if value is None:
            return None
        if isinstance(value, str) and value.strip().lower() in ("", "null", "none"):
            return None
        return value  # type: ignore[return-value]

    @staticmethod
    def _coerce_int(value: object) -> int:
        """Coerce timeout_seconds from its transport shapes to an int.

        The dispatcher serialises fields to strings for the CodeBuild env
        transport, so a JSON int may arrive as ``"3600"``. Anything absent or
        non-numeric collapses to 0, which validate() rejects - the field is
        required with no default.
        """
        if isinstance(value, bool):
            return 0
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.lstrip("-").isdigit():
                return int(stripped)
        return 0

    @classmethod
    def from_dict(cls, data: dict) -> SimplePayload:
        return cls(
            trigger_id=data.get("trigger_id", ""),
            s3_package_uri=data.get("s3_package_uri", ""),
            sops_type=cls._coerce_null(data.get("sops_type")),
            sops_path=cls._coerce_null(data.get("sops_path")),
            commands_b64=data.get("commands_b64", ""),
            done_endpoint=data.get("done_endpoint", ""),
            execution_target=data.get("execution_target", ""),
            timeout_seconds=cls._coerce_int(data.get("timeout_seconds")),
            callback_url=cls._coerce_null(data.get("callback_url")),
            callback_token=cls._coerce_null(data.get("callback_token")),
            execution_mode=cls._coerce_null(data.get("execution_mode")),
        )

    def validate(self) -> None:
        """Validate all fields. Raises PayloadValidationError on failure."""
        errors: list[str] = []

        if not self.trigger_id:
            errors.append("trigger_id must be non-empty")

        if not _S3_URI_RE.match(self.s3_package_uri or ""):
            errors.append(f"s3_package_uri is not a valid S3 URI: {self.s3_package_uri!r}")

        if self.sops_type is not None and self.sops_type not in _VALID_SOPS_TYPES:
            errors.append(f"sops_type must be one of {sorted(_VALID_SOPS_TYPES)} or None, got {self.sops_type!r}")

        if self.sops_type == "ssm" and not self.sops_path:
            errors.append("sops_path is required when sops_type is 'ssm'")

        # Validate commands_b64 decodes to a JSON array of non-empty strings
        if not self.commands_b64:
            errors.append("commands_b64 must be non-empty")
        else:
            try:
                decoded = base64.b64decode(self.commands_b64)
                cmds = json.loads(decoded)
                if not isinstance(cmds, list):
                    errors.append("commands_b64 must decode to a JSON array")
                elif not cmds:
                    errors.append("commands_b64 must decode to a non-empty array")
                else:
                    for i, cmd in enumerate(cmds):
                        if not isinstance(cmd, str) or not cmd.strip():
                            errors.append(f"commands_b64[{i}] must be a non-empty string")
            except Exception as exc:
                errors.append(f"commands_b64 failed to decode: {exc}")

        if not _S3_URI_RE.match(self.done_endpoint or ""):
            errors.append(f"done_endpoint is not a valid S3 URI: {self.done_endpoint!r}")

        if self.execution_target not in _VALID_EXECUTION_TARGETS:
            errors.append(
                f"execution_target must be one of {sorted(_VALID_EXECUTION_TARGETS)}, got {self.execution_target!r}"
            )

        if not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds <= 0:
            errors.append(
                f"timeout_seconds must be a positive integer, got {self.timeout_seconds!r}"
            )

        if self.callback_token is not None and self.callback_url is None:
            errors.append("callback_url is required when callback_token is set")

        if self.callback_url is not None and not self.callback_url.startswith(("http://", "https://")):
            errors.append(f"callback_url must be an http(s) URL: {self.callback_url!r}")

        if self.execution_mode is not None and self.execution_mode != "direct":
            errors.append(
                f"execution_mode must be 'direct' or None, got {self.execution_mode!r}"
            )

        if self.execution_mode == "direct" and self.execution_target != "codebuild":
            errors.append(
                "execution_mode 'direct' requires execution_target 'codebuild', "
                f"got execution_target {self.execution_target!r}"
            )

        if errors:
            raise PayloadValidationError("; ".join(errors))

    def decode_commands(self) -> list[str]:
        """Decode commands_b64 into a list of shell command strings."""
        return json.loads(base64.b64decode(self.commands_b64))
