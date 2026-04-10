"""VCS helper — provider-agnostic facade for PR comment management.

Usage:
    vcs = VcsHelper(provider="github")
    vcs.upsert_comment("org/repo", 42, "run-123", "Deploy OK", ["deploy"], token)
"""

import re
from typing import Dict, Any, List, Optional

from .base import VcsProvider
from .github import GitHubProvider

# Registry of available providers.  Add new ones here.
PROVIDERS: Dict[str, type] = {
    "github": GitHubProvider,
}


class VcsHelper:
    """Provider-agnostic facade that wraps a VcsProvider.

    Responsibilities:
    - Factory: instantiate the right provider via provider="github" etc.
    - Delegates raw VCS operations (create/update/delete) to the provider.
    - Adds shared business logic that works across all providers:
      search_comments, upsert_comment, format_tags, has_tag_block_at_last_line.

    Tag block format (always the last line of a managed comment):
        ###search_str### #tag1 #tag2
    """

    def __init__(self, provider: str = "github"):
        provider_cls = PROVIDERS.get(provider)
        if provider_cls is None:
            supported = ", ".join(sorted(PROVIDERS))
            raise ValueError(
                f"Unknown VCS provider '{provider}'. Supported: {supported}"
            )
        self._provider: VcsProvider = provider_cls()

    @property
    def provider(self) -> VcsProvider:
        """Access the underlying provider directly if needed."""
        return self._provider

    # ------------------------------------------------------------------
    # Delegated provider operations
    # ------------------------------------------------------------------

    def create_comment(self, repo: str, pr_number: int, body: str, token: str) -> int:
        return self._provider.create_comment(repo, pr_number, body, token)

    def update_comment(self, repo: str, comment_id: int, body: str, token: str) -> bool:
        return self._provider.update_comment(repo, comment_id, body, token)

    def delete_comment(self, repo: str, comment_id: int, token: str) -> bool:
        return self._provider.delete_comment(repo, comment_id, token)

    def find_comment_by_tag(
        self, repo: str, pr_number: int, tag: str, token: str,
    ) -> Optional[int]:
        return self._provider.find_comment_by_tag(repo, pr_number, tag, token)

    def get_comments(
        self, repo: str, pr_number: int, token: str,
    ) -> List[dict]:
        return self._provider.get_comments(repo, pr_number, token)

    # ------------------------------------------------------------------
    # Shared business logic — works across all providers
    # ------------------------------------------------------------------

    def search_comments(
        self, repo: str, pr_number: int, search_str: str, token: str,
        tags: Optional[List[str]] = None,
    ) -> List[int]:
        """Find all comments whose last line contains a matching tag block.

        Strict last-line search using ###search_str### #tag format.
        Returns list of matching comment IDs.
        """
        comments = self._provider.get_comments(repo, pr_number, token)
        return [
            c["id"] for c in comments
            if self.has_tag_block_at_last_line(c.get("body", ""), search_str, tags)
        ]

    def upsert_comment(
        self, repo: str, pr_number: int, search_str: str,
        comment_body: str, tags: List[str], token: str,
    ) -> Dict[str, Any]:
        """Create comment if not found, update if found.

        Uses last-line tag block search to find existing comments.
        Appends the tag block to comment_body automatically.
        """
        full_body = f"{comment_body}\n\n{self.format_tags(search_str, tags)}"

        existing_ids = self.search_comments(repo, pr_number, search_str, token, tags)

        if existing_ids:
            comment_id = existing_ids[0]
            success = self._provider.update_comment(repo, comment_id, full_body, token)
            return {"id": comment_id, "status": success, "action": "updated"}
        else:
            comment_id = self._provider.create_comment(repo, pr_number, full_body, token)
            return {"id": comment_id, "status": True, "action": "created"}

    # ------------------------------------------------------------------
    # Static helpers — usable without an instance
    # ------------------------------------------------------------------

    @staticmethod
    def format_tags(search_str: str, tags: Optional[List[str]] = None) -> str:
        """Format the tag block that goes on the last line of a comment.

        Example: format_tags("my-run", ["deploy", "vpc"]) -> "###my-run### #deploy #vpc"
        """
        tag_line = f"###{search_str}###"
        if tags:
            tag_suffix = " ".join(f"#{t}" for t in tags if t)
            tag_line = f"{tag_line} {tag_suffix}"
        return tag_line

    @staticmethod
    def has_tag_block_at_last_line(
        comment_body: str, search_str: str, tags: Optional[List[str]] = None,
    ) -> bool:
        """Check if the last non-empty line is a matching tag block.

        Matches: ###search_str### optionally followed by #tag1 #tag2 ...
        If tags are provided, all must be present.
        """
        lines = comment_body.strip().split("\n")
        last_line = None
        for line in reversed(lines):
            stripped = line.strip()
            if stripped:
                last_line = stripped
                break

        if not last_line:
            return False

        pattern = rf"^###{re.escape(search_str)}###\s*(.*)$"
        match = re.match(pattern, last_line)
        if not match:
            return False

        if tags:
            tags_str = match.group(1).strip()
            existing = {
                t.lstrip("#") for t in tags_str.split() if t.startswith("#")
            }
            return all(t in existing for t in tags if t)

        return True
