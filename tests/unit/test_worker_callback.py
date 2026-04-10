"""Unit tests for src/worker/callback.py."""

import json
from unittest.mock import patch, MagicMock

import pytest

from src.worker.callback import send_callback


class TestSendCallback:
    @patch("src.worker.callback.requests.put")
    def test_successful_put(self, mock_put):
        mock_put.return_value = MagicMock(status_code=200)

        result = send_callback("https://presigned.url", "succeeded", "output log")

        assert result is True
        mock_put.assert_called_once()
        call_kwargs = mock_put.call_args
        payload = json.loads(call_kwargs[1]["data"])
        assert payload["status"] == "succeeded"
        assert payload["log"] == "output log"

    @patch("src.worker.callback.time.sleep")
    @patch("src.worker.callback.requests.put")
    def test_retry_on_failure(self, mock_put, mock_sleep):
        mock_put.side_effect = [
            MagicMock(status_code=500),  # first fails
            MagicMock(status_code=200),  # second succeeds
        ]

        result = send_callback("https://presigned.url", "succeeded", "log")

        assert result is True
        assert mock_put.call_count == 2

    @patch("src.worker.callback.time.sleep")
    @patch("src.worker.callback.requests.put")
    def test_all_retries_exhausted(self, mock_put, mock_sleep):
        mock_put.return_value = MagicMock(status_code=500)

        result = send_callback("https://presigned.url", "failed", "error log")

        assert result is False
        assert mock_put.call_count == 4  # 1 initial + 3 retries

    @patch("src.worker.callback.time.sleep")
    @patch("src.worker.callback.requests.put")
    def test_retry_on_exception(self, mock_put, mock_sleep):
        mock_put.side_effect = [
            ConnectionError("network error"),
            MagicMock(status_code=200),
        ]

        result = send_callback("https://presigned.url", "succeeded", "log")

        assert result is True
        assert mock_put.call_count == 2
