"""Commands-only code source.

Orders that ship no code (only shell commands) still need a work
directory for OrderBundler to drop ``cmds.json`` / ``env_vars.json``
into, so this source creates a fresh empty tempdir.
"""

import tempfile
from typing import Any


class CommandsOnlyCodeSource:
    """Code source for orders that have neither git nor S3 backing."""

    kind = "commands_only"

    def detect(self, order: Any, job: Any) -> bool:
        if getattr(order, "s3_location", None):
            return False
        repo = (
            getattr(order, "git_repo", None)
            or getattr(job, "git_repo", None)
            or ""
        )
        return not bool(repo)

    def fetch(self, order: Any, job: Any) -> str:
        return tempfile.mkdtemp(prefix="aws-exe-sys-cmds-")

    def cleanup(self) -> None:
        return None
