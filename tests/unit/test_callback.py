"""Unit tests for aws_exe_sys/common/callback.py — best-effort completion callback."""

import http.client
import json
from unittest.mock import patch
import urllib.error

from aws_exe_sys.common.callback import post_callback
from aws_exe_sys.common.result_writer import ExecutionResult, StepResult

_RESULT = ExecutionResult(
    trigger_id="t-1",
    status="succeeded",
    steps=[StepResult(step_name="step-0", status="succeeded", exit_code=0, duration_seconds=0.1, output="ok")],
)


class TestPostCallbackAbsentURL:
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_no_url_no_post_attempted(self, mock_urlopen):
        post_callback(None, None, _RESULT)
        mock_urlopen.assert_not_called()

    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_empty_url_no_post_attempted(self, mock_urlopen):
        post_callback("", "tok-abc", _RESULT)
        mock_urlopen.assert_not_called()


class TestPostCallbackPresentURL:
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_posts_with_bearer_token(self, mock_urlopen):
        post_callback("https://caller.example.com/hooks/done", "tok-abc", _RESULT)

        mock_urlopen.assert_called_once()
        request = mock_urlopen.call_args[0][0]
        assert request.full_url == "https://caller.example.com/hooks/done"
        assert request.get_method() == "POST"
        assert request.get_header("Authorization") == "Bearer tok-abc"
        assert json.loads(request.data.decode()) == _RESULT.to_dict()

    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_posts_without_token_omits_auth_header(self, mock_urlopen):
        post_callback("https://caller.example.com/hooks/done", None, _RESULT)

        request = mock_urlopen.call_args[0][0]
        assert request.get_header("Authorization") is None


class TestPostCallbackFailureIsLogOnly:
    @patch("aws_exe_sys.common.callback.logger")
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_url_error_is_logged_and_swallowed(self, mock_urlopen, mock_logger):
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")

        post_callback("https://caller.example.com/hooks/done", "tok-abc", _RESULT)  # must not raise

        mock_logger.warning.assert_called_once()

    @patch("aws_exe_sys.common.callback.logger")
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_http_error_is_logged_and_swallowed(self, mock_urlopen, mock_logger):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://caller.example.com/hooks/done", 500, "Internal Server Error", {}, None
        )

        post_callback("https://caller.example.com/hooks/done", None, _RESULT)  # must not raise

        mock_logger.warning.assert_called_once()

    @patch("aws_exe_sys.common.callback.logger")
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_timeout_is_logged_and_swallowed(self, mock_urlopen, mock_logger):
        mock_urlopen.side_effect = TimeoutError("timed out")

        post_callback("https://caller.example.com/hooks/done", None, _RESULT)  # must not raise

        mock_logger.warning.assert_called_once()

    @patch("aws_exe_sys.common.callback.logger")
    @patch("aws_exe_sys.common.callback.urllib.request.urlopen")
    def test_malformed_url_is_logged_and_swallowed(self, mock_urlopen, mock_logger):
        # http.client.InvalidURL is not a URLError/OSError subclass — this is the
        # exact escape the broad `except Exception` seam exists to close (a
        # malformed callback_url must not fail the already-succeeded execution
        # or trigger an async retry that re-executes commands).
        mock_urlopen.side_effect = http.client.InvalidURL("URL can't contain control characters")

        post_callback("https://caller.example.com/hooks/done", "tok-abc", _RESULT)  # must not raise

        mock_logger.warning.assert_called_once()

    def test_malformed_url_end_to_end_does_not_raise(self):
        # No mocking of urlopen: a control character in the host makes the real
        # http.client machinery raise http.client.InvalidURL. Proves the result
        # is unaffected and nothing propagates out of post_callback.
        post_callback("http://caller.example.com\x00/hooks/done", None, _RESULT)
