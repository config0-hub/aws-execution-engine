"""GitHub VCS provider implementation."""

import os
import shutil
import subprocess
from typing import List, Optional

import requests

from .base import VcsProvider

GITHUB_API_BASE = "https://api.github.com"


class GitHubProvider(VcsProvider):
    """GitHub implementation of the VCS provider.

    Handles GitHub-specific HTTP calls (PR comments), clone operations,
    and URL construction. The ``https_host`` / ``ssh_host`` attributes can
    be set on a subclass to support GitHub Enterprise deployments.
    """

    name = "github"
    https_host = "github.com"
    ssh_host = "github.com"

    # ------------------------------------------------------------------
    # Clone operations
    # ------------------------------------------------------------------

    def get_clone_url(self, repo: str, token: Optional[str] = None) -> str:
        """Build an HTTPS clone URL, embedding ``token`` via ``x-access-token``.

        GitHub's fine-grained / classic tokens both accept the
        ``x-access-token`` username convention for Git-over-HTTPS.
        """
        if token:
            return f"https://x-access-token:{token}@{self.https_host}/{repo}.git"
        return f"https://{self.https_host}/{repo}.git"

    def _get_ssh_url(self, repo: str) -> str:
        return f"git@{self.ssh_host}:{repo}.git"

    def clone(
        self,
        repo: str,
        dest: str,
        token: str = "",
        commit_hash: Optional[str] = None,
        ssh_key_path: Optional[str] = None,
    ) -> None:
        """Clone ``repo`` into ``dest``, HTTPS-with-token primary, SSH fallback.

        Raises:
            subprocess.CalledProcessError: if the clone (and the SSH
                fallback, when applicable) fails.
        """
        depth = "2" if commit_hash else "1"

        if token:
            clone_url = self.get_clone_url(repo, token=token)
            try:
                subprocess.run(
                    ["git", "clone", "--depth", depth, clone_url, dest],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError:
                if ssh_key_path:
                    # Fallback: SSH. Wipe the half-populated dest first.
                    shutil.rmtree(dest, ignore_errors=True)
                    os.makedirs(dest, exist_ok=False)
                    self._clone_via_ssh(repo, ssh_key_path, dest, depth)
                else:
                    raise
        elif ssh_key_path:
            self._clone_via_ssh(repo, ssh_key_path, dest, depth)
        else:
            clone_url = self.get_clone_url(repo)
            subprocess.run(
                ["git", "clone", "--depth", depth, clone_url, dest],
                check=True, capture_output=True, text=True,
            )

        if commit_hash:
            self._checkout_commit(dest, commit_hash)

    def clone_ssh(
        self,
        repo: str,
        dest: str,
        ssh_key_path: str,
        commit_hash: Optional[str] = None,
    ) -> None:
        """Clone ``repo`` via SSH using ``ssh_key_path`` as the private key."""
        depth = "2" if commit_hash else "1"
        self._clone_via_ssh(repo, ssh_key_path, dest, depth)
        if commit_hash:
            self._checkout_commit(dest, commit_hash)

    def _clone_via_ssh(
        self, repo: str, ssh_key_path: str, dest: str, depth: str,
    ) -> None:
        ssh_url = self._get_ssh_url(repo)
        env = {
            **os.environ,
            "GIT_SSH_COMMAND": f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no",
        }
        subprocess.run(
            ["git", "clone", "--depth", depth, ssh_url, dest],
            check=True, capture_output=True, text=True, env=env,
        )

    @staticmethod
    def _checkout_commit(work_dir: str, commit_hash: str) -> None:
        """Checkout ``commit_hash`` inside ``work_dir``.

        Falls back to an explicit fetch if the commit isn't in the
        shallow clone.
        """
        try:
            subprocess.run(
                ["git", "checkout", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )
        except subprocess.CalledProcessError:
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )
            subprocess.run(
                ["git", "checkout", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )

    # ------------------------------------------------------------------
    # PR comment CRUD
    # ------------------------------------------------------------------

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
