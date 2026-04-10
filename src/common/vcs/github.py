"""GitHub VCS provider implementation."""

from typing import List, Optional

import requests

from .base import VcsProvider

GITHUB_API_BASE = "https://api.github.com"


class GitHubProvider(VcsProvider):
    """GitHub implementation of the VCS provider.

    Handles GitHub-specific HTTP calls, authentication, and pagination.
    """

    def _auth_headers(self, token: str) -> dict:
        return {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        }

    def get_comments(
        self, repo: str, pr_number: int, token: str,
    ) -> List[dict]:
        """Return all comments for a PR, handling GitHub pagination."""
        url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{pr_number}/comments"
        all_comments = []
        page = 1
        while True:
            response = requests.get(
                url,
                params={"page": page, "per_page": 100},
                headers=self._auth_headers(token),
            )
            response.raise_for_status()
            comments = response.json()
            if not comments:
                break
            all_comments.extend(comments)
            if len(comments) < 100:
                break
            page += 1
        return all_comments

    def create_comment(self, repo: str, pr_number: int, body: str, token: str) -> int:
        """POST to GitHub REST API to create a PR comment."""
        url = f"{GITHUB_API_BASE}/repos/{repo}/issues/{pr_number}/comments"
        response = requests.post(
            url,
            json={"body": body},
            headers=self._auth_headers(token),
        )
        response.raise_for_status()
        return response.json()["id"]

    def update_comment(self, repo: str, comment_id: int, body: str, token: str) -> bool:
        """PATCH to update an existing comment."""
        url = f"{GITHUB_API_BASE}/repos/{repo}/issues/comments/{comment_id}"
        response = requests.patch(
            url,
            json={"body": body},
            headers=self._auth_headers(token),
        )
        return response.status_code == 200

    def delete_comment(self, repo: str, comment_id: int, token: str) -> bool:
        """DELETE a comment."""
        url = f"{GITHUB_API_BASE}/repos/{repo}/issues/comments/{comment_id}"
        response = requests.delete(url, headers=self._auth_headers(token))
        return response.status_code == 204

    def find_comment_by_tag(
        self, repo: str, pr_number: int, tag: str, token: str,
    ) -> Optional[int]:
        """Find a comment containing a tag substring anywhere in the body.

        General-purpose whole-body search. Returns first match or None.
        """
        for comment in self.get_comments(repo, pr_number, token):
            if tag in comment.get("body", ""):
                return comment["id"]
        return None
