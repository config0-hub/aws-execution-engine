"""Unit tests for src/common/vcs/github.py — GitHub provider layer."""

import json

import pytest
import responses

from src.common.vcs.github import GitHubProvider, GITHUB_API_BASE


@pytest.fixture
def github():
    return GitHubProvider()


# ---------------------------------------------------------------------------
# get_comments — pagination
# ---------------------------------------------------------------------------


class TestGetComments:
    @responses.activate
    def test_single_page(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "first"},
                {"id": 2, "body": "second"},
            ],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert len(comments) == 2
        assert comments[0]["id"] == 1

    @responses.activate
    def test_pagination(self, github):
        # Full page of 100
        page1 = [{"id": i, "body": f"comment {i}"} for i in range(100)]
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=page1,
            status=200,
        )
        # Partial second page
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 200, "body": "page 2"}],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert len(comments) == 101
        assert len(responses.calls) == 2

    @responses.activate
    def test_empty(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert comments == []


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCreateComment:
    @responses.activate
    def test_create_comment(self, github):
        responses.add(
            responses.POST,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json={"id": 12345, "body": "test comment"},
            status=201,
        )

        comment_id = github.create_comment("org/repo", 42, "test comment", "token123")
        assert comment_id == 12345

        req = responses.calls[0].request
        assert "Bearer token123" in req.headers["Authorization"]
        assert json.loads(req.body)["body"] == "test comment"


class TestUpdateComment:
    @responses.activate
    def test_success(self, github):
        responses.add(
            responses.PATCH,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            json={"id": 12345, "body": "updated"},
            status=200,
        )
        assert github.update_comment("org/repo", 12345, "updated", "token") is True

    @responses.activate
    def test_failure(self, github):
        responses.add(
            responses.PATCH,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            json={"message": "Not Found"},
            status=404,
        )
        assert github.update_comment("org/repo", 12345, "updated", "token") is False


class TestDeleteComment:
    @responses.activate
    def test_success(self, github):
        responses.add(
            responses.DELETE,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            status=204,
        )
        assert github.delete_comment("org/repo", 12345, "token") is True

    @responses.activate
    def test_failure(self, github):
        responses.add(
            responses.DELETE,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/99999",
            status=404,
        )
        assert github.delete_comment("org/repo", 99999, "token") is False


# ---------------------------------------------------------------------------
# find_comment_by_tag — whole-body substring search
# ---------------------------------------------------------------------------


class TestFindCommentByTag:
    @responses.activate
    def test_finds_tag_anywhere_in_body(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "unrelated"},
                {"id": 2, "body": "contains #tag-123 in the middle\nmore text"},
            ],
            status=200,
        )
        assert github.find_comment_by_tag("org/repo", 42, "#tag-123", "token") == 2

    @responses.activate
    def test_not_found(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 1, "body": "no match"}],
            status=200,
        )
        assert github.find_comment_by_tag("org/repo", 42, "#missing", "token") is None
