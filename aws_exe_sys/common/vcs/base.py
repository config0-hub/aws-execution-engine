"""Abstract base class for VCS providers.

Providers bundle three categories of VCS operations:

1. **Clone** — fetching code from the remote into a local directory.
2. **PR comment CRUD** — `create_comment`, `update_comment`, `delete_comment`,
   `find_comment_by_tag`, `get_comments`. Historically the only thing this
   ABC covered.
3. **URL construction** — `get_clone_url` for building authenticated HTTPS
   URLs so callers can compose clone commands.

Clone is parameterized by a ``host`` attribute so subclasses can override it
(e.g. GitHub Enterprise, self-hosted Bitbucket) instead of hardcoding
``github.com`` throughout the codebase.
"""

from abc import ABC, abstractmethod
from typing import List, Optional


class VcsProvider(ABC):
    """Abstract base class for Version Control System providers.

    Attributes:
        name: Short identifier used in job configs (``job.git_provider``).
            Subclasses must set this — e.g. ``"github"``, ``"bitbucket"``.
        https_host: Host name used when building HTTPS clone URLs.
            Subclasses override for GitHub Enterprise or self-hosted forges.
        ssh_host: Host name used when building SSH clone URLs. Defaults to
            ``https_host`` — override when the SSH host differs.
    """

    name: str = ""
    https_host: str = ""
    ssh_host: str = ""

    # ------------------------------------------------------------------
    # Clone operations — code fetching
    # ------------------------------------------------------------------

    @abstractmethod
    def get_clone_url(self, repo: str, token: Optional[str] = None) -> str:
        """Build an HTTPS clone URL for ``repo``.

        If ``token`` is set, embed it as an HTTPS basic-auth credential.
        Providers that use a different embedding format (``x-access-token``
        for GitHub, ``x-token-auth`` for Bitbucket) override this.
        """
        ...

    @abstractmethod
    def clone(
        self,
        repo: str,
        dest: str,
        token: str = "",
        commit_hash: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
    ) -> None:
        """Clone ``repo`` into ``dest``.

        Providers pick the transport:

        - HTTPS with token (primary) when ``token`` is set
        - SSH (fallback) when ``ssh_key_path`` is set and HTTPS fails or
          is unavailable
        - Public HTTPS (no auth) when neither is set

        After clone, the provider must check out ``commit_hash`` if given,
        including a ``git fetch`` fallback when the commit is not in a
        shallow clone.
        """
        ...

    @abstractmethod
    def clone_ssh(
        self,
        repo: str,
        dest: str,
        ssh_key_path: str,
        commit_hash: Optional[str] = None,
    ) -> None:
        """Clone ``repo`` via SSH using a specific private key file."""
        ...

    # ------------------------------------------------------------------
    # PR comment CRUD
    # ------------------------------------------------------------------

    @abstractmethod
    def create_comment(self, repo: str, pr_number: int, body: str, token: str) -> int:
        """Create a comment on a PR. Returns comment_id."""
        ...

    @abstractmethod
    def update_comment(self, repo: str, comment_id: int, body: str, token: str) -> bool:
        """Update an existing comment. Returns True if successful."""
        ...

    @abstractmethod
    def delete_comment(self, repo: str, comment_id: int, token: str) -> bool:
        """Delete a comment. Returns True if successful."""
        ...

    @abstractmethod
    def find_comment_by_tag(
        self, repo: str, pr_number: int, tag: str, token: str,
    ) -> Optional[int]:
        """Find a comment containing a tag substring anywhere in the body.

        General-purpose whole-body search.
        Returns the first matching comment_id, or None.
        """
        ...

    @abstractmethod
    def get_comments(
        self, repo: str, pr_number: int, token: str,
    ) -> List[dict]:
        """Return all comments for a PR.

        Each dict must contain at least 'id' and 'body' keys.
        Providers handle pagination internally.
        """
        ...
