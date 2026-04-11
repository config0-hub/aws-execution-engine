"""Unit tests for aws_exe_sys/worker/callback.py."""

import json
import sys
import types
from unittest.mock import patch, MagicMock

import pytest

from aws_exe_sys.worker.callback import send_callback


class TestSendCallback:
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_successful_put(self, mock_put):
        mock_put.return_value = MagicMock(status_code=200)

        result = send_callback("https://presigned.url", "succeeded", "output log")

        assert result is True
        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        payload = json.loads(call_kwargs[1]["data"])
        assert payload["status"] == "succeeded"
        assert payload["log"] == "output log"

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_retry_on_failure(self, mock_put, mock_sleep):
        mock_put.side_effect = [
            MagicMock(status_code=500),  # first fails
            MagicMock(status_code=200),  # second succeeds
        ]

        result = send_callback("https://presigned.url", "succeeded", "log")

        assert result is True
        assert mock_put.call_count == 2

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_all_retries_exhausted(self, mock_put, mock_sleep):
        mock_put.return_value = MagicMock(status_code=500)

        result = send_callback("https://presigned.url", "failed", "error log")

        assert result is False
        assert mock_put.call_count == 4  # 1 initial + 3 retries

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_retry_on_exception(self, mock_put, mock_sleep):
        mock_put.side_effect = [
            ConnectionError("network error"),
            MagicMock(status_code=200),
        ]

        result = send_callback("https://presigned.url", "succeeded", "log")

        assert result is True
        assert mock_put.call_count == 2


class TestCallbackDynamoDBFallback:
    """P3-1: direct DynamoDB fallback when presigned S3 PUT is unreachable."""

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_fallback_writes_dynamodb_on_exhausted_retries(
        self, mock_put, mock_sleep,
    ):
        """All retries exhausted -> fallback calls update_order_status once."""
        mock_put.return_value = MagicMock(status_code=500)
        mock_update = MagicMock()

        with patch(
            "aws_exe_sys.common.dynamodb.update_order_status", mock_update,
        ):
            result = send_callback(
                "https://presigned.url",
                "failed",
                "error log",
                run_id="run-x",
                order_num="0001",
            )

        assert result is False
        mock_update.assert_called_once_with(
            run_id="run-x",
            order_num="0001",
            status="failed",
            extra_fields={"error": "callback_failed"},
        )

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_fallback_skipped_when_no_run_id(self, mock_put, mock_sleep):
        """Missing run_id/order_num -> logged no-op, never fabricate ids."""
        mock_put.return_value = MagicMock(status_code=500)
        mock_update = MagicMock()

        with patch(
            "aws_exe_sys.common.dynamodb.update_order_status", mock_update,
        ):
            # No run_id/order_num kwargs at all
            result = send_callback("https://presigned.url", "failed", "log")

        assert result is False
        mock_update.assert_not_called()

    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_success_does_not_trigger_fallback(self, mock_put):
        """Happy path -> fallback never consulted even when ids supplied."""
        mock_put.return_value = MagicMock(status_code=200)
        mock_update = MagicMock()

        with patch(
            "aws_exe_sys.common.dynamodb.update_order_status", mock_update,
        ):
            result = send_callback(
                "https://presigned.url",
                "succeeded",
                "log",
                run_id="run-x",
                order_num="0001",
            )

        assert result is True
        mock_update.assert_not_called()

    @patch("aws_exe_sys.worker.callback.time.sleep")
    @patch("aws_exe_sys.worker.callback.requests.put")
    def test_fallback_swallows_dynamodb_exception(self, mock_put, mock_sleep):
        """DynamoDB raising must not propagate; send_callback still returns False."""
        mock_put.return_value = MagicMock(status_code=500)
        mock_update = MagicMock(side_effect=RuntimeError("throttled"))

        with patch(
            "aws_exe_sys.common.dynamodb.update_order_status", mock_update,
        ):
            # Should NOT raise
            result = send_callback(
                "https://presigned.url",
                "failed",
                "log",
                run_id="run-x",
                order_num="0001",
            )

        assert result is False
        mock_update.assert_called_once()
