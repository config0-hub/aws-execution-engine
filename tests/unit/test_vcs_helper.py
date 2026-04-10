"""Unit tests for src/common/vcs/helper.py — VcsHelper facade."""

import json

import pytest
import responses

from src.common.vcs.helper import VcsHelper
from src.common.vcs.github import GITHUB_API_BASE


@pytest.fixture
def vcs():
    return VcsHelper(provider="github")


# ---------------------------------------------------------------------------
# Factory / init
# ---------------------------------------------------------------------------


class TestVcsHelperInit:
    def test_github_provider(self):
        vcs = VcsHelper(provider="github")
        from src.common.vcs.github import GitHubProvider
        assert isinstance(vcs.provider, GitHubProvider)

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown VCS provider 'bitbucket'"):
            VcsHelper(provider="bitbucket")

    def test_default_is_github(self):
        vcs = VcsHelper()
        from src.common.vcs.github import GitHubProvider
        assert isinstance(vcs.provider, GitHubProvider)


# ---------------------------------------------------------------------------
# format_tags / has_tag_block_at_last_line — static helpers
# ---------------------------------------------------------------------------


class TestFormatTags:
    def test_search_str_only(self):
        assert VcsHelper.format_tags("my-run") == "###my-run###"

    def test_with_tags(self):
        assert VcsHelper.format_tags("my-run", ["deploy", "vpc"]) == "###my-run### #deploy #vpc"

    def test_empty_tags_list(self):
        assert VcsHelper.format_tags("my-run", []) == "###my-run###"


class TestHasTagBlockAtLastLine:
    def test_match_last_line(self):
        body = "Some update\nMore details\n###my-run### #deploy"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run") is True

    def test_match_with_required_tags(self):
        body = "Status\n###my-run### #deploy #vpc"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run", ["deploy", "vpc"]) is True

    def test_missing_required_tag(self):
        body = "Status\n###my-run### #deploy"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run", ["deploy", "vpc"]) is False

    def test_no_tag_block(self):
        assert VcsHelper.has_tag_block_at_last_line("Just text", "my-run") is False

    def test_tag_in_middle_not_last_line(self):
        body = "###my-run### #deploy\nSome other text after"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run") is False

    def test_trailing_whitespace(self):
        body = "Status\n###my-run### #deploy  \n  "
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run") is True

    def test_search_str_only_no_tags(self):
        body = "Status\n###my-run###"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run") is True

    def test_wrong_search_str(self):
        body = "Status\n###other-run### #deploy"
        assert VcsHelper.has_tag_block_at_last_line(body, "my-run") is False

    def test_empty_body(self):
        assert VcsHelper.has_tag_block_at_last_line("", "my-run") is False


# ---------------------------------------------------------------------------
# search_comments — last-line tag block search
# ---------------------------------------------------------------------------


class TestSearchComments:
    @responses.activate
    def test_finds_matching_tag_block(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "unrelated comment"},
                {"id": 2, "body": "Deploy status\n\n###my-run### #deploy"},
            ],
            status=200,
        )

        ids = vcs.search_comments("org/repo", 42, "my-run", "token")
        assert ids == [2]

    @responses.activate
    def test_ignores_tag_not_on_last_line(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "###my-run### #deploy\nExtra text after"},
            ],
            status=200,
        )

        ids = vcs.search_comments("org/repo", 42, "my-run", "token")
        assert ids == []

    @responses.activate
    def test_filters_by_required_tags(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "VPC plan\n\n###my-run### #deploy #vpc"},
                {"id": 2, "body": "RDS plan\n\n###my-run### #deploy #rds"},
            ],
            status=200,
        )

        ids = vcs.search_comments("org/repo", 42, "my-run", "token", tags=["vpc"])
        assert ids == [1]

    @responses.activate
    def test_returns_empty_when_no_match(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 1, "body": "no tag block here"}],
            status=200,
        )

        ids = vcs.search_comments("org/repo", 42, "my-run", "token")
        assert ids == []

    @responses.activate
    def test_returns_multiple_matches(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 10, "body": "First\n\n###run-1### #deploy"},
                {"id": 20, "body": "Second\n\n###run-1### #deploy"},
            ],
            status=200,
        )

        ids = vcs.search_comments("org/repo", 42, "run-1", "token")
        assert ids == [10, 20]


# ---------------------------------------------------------------------------
# upsert_comment
# ---------------------------------------------------------------------------


class TestUpsertComment:
    @responses.activate
    def test_creates_when_not_found(self, vcs):
        # search returns no matches
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[],
            status=200,
        )
        # create comment
        responses.add(
            responses.POST,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json={"id": 555, "body": "new"},
            status=201,
        )

        result = vcs.upsert_comment(
            "org/repo", 42, "my-run", "Deploy report", ["deploy"], "token"
        )

        assert result["id"] == 555
        assert result["action"] == "created"
        assert result["status"] is True

        created_body = json.loads(responses.calls[1].request.body)["body"]
        assert created_body.endswith("###my-run### #deploy")

    @responses.activate
    def test_updates_when_found(self, vcs):
        # search returns a match
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 100, "body": "Old content\n\n###my-run### #deploy"},
            ],
            status=200,
        )
        # update comment
        responses.add(
            responses.PATCH,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/100",
            json={"id": 100, "body": "updated"},
            status=200,
        )

        result = vcs.upsert_comment(
            "org/repo", 42, "my-run", "New deploy report", ["deploy"], "token"
        )

        assert result["id"] == 100
        assert result["action"] == "updated"
        assert result["status"] is True

        updated_body = json.loads(responses.calls[1].request.body)["body"]
        assert "New deploy report" in updated_body
        assert updated_body.endswith("###my-run### #deploy")

    @responses.activate
    def test_upsert_with_multiple_tags(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[],
            status=200,
        )
        responses.add(
            responses.POST,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json={"id": 777, "body": "created"},
            status=201,
        )

        result = vcs.upsert_comment(
            "org/repo", 42, "run-abc", "Plan output", ["plan", "vpc"], "token"
        )

        assert result["id"] == 777
        created_body = json.loads(responses.calls[1].request.body)["body"]
        assert created_body.endswith("###run-abc### #plan #vpc")


# ---------------------------------------------------------------------------
# Delegated operations pass through to provider
# ---------------------------------------------------------------------------


class TestDelegation:
    @responses.activate
    def test_create_comment_delegates(self, vcs):
        responses.add(
            responses.POST,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json={"id": 99, "body": "hi"},
            status=201,
        )
        assert vcs.create_comment("org/repo", 42, "hi", "token") == 99

    @responses.activate
    def test_delete_comment_delegates(self, vcs):
        responses.add(
            responses.DELETE,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/99",
            status=204,
        )
        assert vcs.delete_comment("org/repo", 99, "token") is True

    @responses.activate
    def test_find_comment_by_tag_delegates(self, vcs):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 5, "body": "has #mytag here"}],
            status=200,
        )
        assert vcs.find_comment_by_tag("org/repo", 42, "#mytag", "token") == 5
