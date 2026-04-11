"""Git-backed code source.

Instances cache clone directories keyed by ``(repo, commit_hash)`` so
multiple orders that point at the same commit share one underlying
clone. Credentials are resolved once on first ``fetch()`` and re-used
for all subsequent clones within the same job run.
"""

import shutil
from typing import Any, Dict, Optional, Tuple

from aws_exe_sys.common.code_source import (
    clone_repo,
    extract_folder,
    resolve_git_credentials,
)


class GitCodeSource:
    """Code source for git-backed orders.

    The ``detect`` rule is: not S3, and either ``order.git_repo`` or
    ``job.git_repo`` is set. ``fetch`` clones on demand (sharing clones
    across orders with the same ``(repo, commit)`` key) and returns an
    isolated per-order copy of ``order.git_folder``.
    """

    kind = "git"

    def __init__(self) -> None:
        self._clones: Dict[Tuple[str, Optional[str]], str] = {}
        self._credentials_resolved = False
        self._token: str = ""
        self._ssh_key_path: Optional[str] = None

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def detect(self, order: Any, job: Any) -> bool:
        if getattr(order, "s3_location", None):
            return False
        return bool(self._resolve_repo(order, job))

    def fetch(self, order: Any, job: Any) -> str:
        self._ensure_credentials(job)

        repo = self._resolve_repo(order, job)
        commit = (
            getattr(order, "commit_hash", None)
            or getattr(job, "commit_hash", None)
        )
        cache_key = (repo, commit)

        if cache_key not in self._clones:
            self._clones[cache_key] = clone_repo(
                repo=repo,
                token=self._token,
                commit_hash=commit,
                ssh_key_path=self._ssh_key_path,
                provider=getattr(job, "git_provider", "github"),
            )

        return extract_folder(self._clones[cache_key], order.git_folder)

    def cleanup(self) -> None:
        for clone_dir in self._clones.values():
            shutil.rmtree(clone_dir, ignore_errors=True)
        self._clones.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_credentials(self, job: Any) -> None:
        if self._credentials_resolved:
            return
        self._token, self._ssh_key_path = resolve_git_credentials(
            token_location=getattr(job, "git_token_location", None) or "",
            ssh_key_location=getattr(job, "git_ssh_key_location", None),
        )
        self._credentials_resolved = True

    @staticmethod
    def _resolve_repo(order: Any, job: Any) -> str:
        return (
            getattr(order, "git_repo", None)
            or getattr(job, "git_repo", None)
            or ""
        )
