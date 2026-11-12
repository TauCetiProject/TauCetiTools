#!/usr/bin/env python3
"""Unit tests for the Zulip alert transport."""

import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zulip  # noqa: E402


class Retry(unittest.TestCase):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"result":"success"}'

    def test_rate_limit_retries_after_server_delay(self):
        error = zulip.urllib.error.HTTPError(
            "https://example.test", 429, "rate limit",
            {"Retry-After": "2.5"},
            io.BytesIO(b'{"code":"RATE_LIMIT_HIT"}'),
        )
        z = zulip.Zulip("bot@example.com", "key", "https://example.test")
        with (
            mock.patch.object(
                zulip.urllib.request, "urlopen",
                side_effect=[error, self.Response()],
            ),
            mock.patch.object(zulip, "sleep_for") as sleep,
        ):
            self.assertEqual(z._call("GET", "/messages", None)["result"], "success")
        sleep.assert_called_once_with(2.5)

    def test_send_message_does_not_retry_lost_responses(self):
        error = zulip.urllib.error.HTTPError(
            "https://example.test", 503, "unavailable", {},
            io.BytesIO(b'{"code":"TEMPORARY"}'),
        )
        z = zulip.Zulip("bot@example.com", "key", "https://example.test")
        with (
            mock.patch.object(
                zulip.urllib.request, "urlopen",
                side_effect=[error, self.Response()],
            ) as urlopen,
            mock.patch.object(zulip, "sleep_for") as sleep,
        ):
            with self.assertRaises(RuntimeError):
                z.send_message("hello")
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
