"""Abstract base class for VCS providers."""

from abc import ABC, abstractmethod
from typing import List, Optional


class VcsProvider(ABC):
    """Abstract base class for Version Control System providers.

    Providers implement raw VCS API operations (HTTP calls, auth, pagination).
    Shared business logic (upsert, tag-block search) lives in VcsHelper.
    """

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
