import asyncio
import hashlib
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

spec = importlib.util.spec_from_file_location(
    "snapshot_network",
    Path(__file__).parents[1] / ".github/scripts/refresh_unity_gems.py",
)
refresh = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = refresh
spec.loader.exec_module(refresh)


class NetworkTests(unittest.TestCase):
    def check(self, outcomes, *, expected=None, error=None):
        calls = []
        original = httpx.AsyncClient

        def handle(request):
            item = outcomes[min(len(calls), len(outcomes) - 1)]
            calls.append(request)
            if isinstance(item, BaseException):
                raise item
            status, body = item
            return httpx.Response(status, content=body)

        def client(**kwargs):
            self.assertEqual(kwargs["timeout"].connect, 15)
            return original(**kwargs, transport=httpx.MockTransport(handle))

        with (
            patch.object(refresh.httpx, "AsyncClient", side_effect=client),
            patch.object(refresh.asyncio, "sleep", new=AsyncMock()),
        ):
            if error:
                with self.assertRaises(error):
                    refresh._download_bytes(
                        "https://official.invalid/config", expected=expected
                    )
            else:
                self.assertEqual(
                    refresh._download_bytes(
                        "https://official.invalid/config", expected=expected
                    ),
                    b"ok",
                )
        return len(calls)

    def test_third_success_and_permanent_http_errors(self):
        for status in (408, 429, 503):
            self.assertEqual(
                self.check([(status, b""), (status, b""), (200, b"ok")]), 3
            )
            self.assertEqual(
                self.check([(status, b"")], error=httpx.HTTPStatusError), 3
            )
        self.assertEqual(self.check([(404, b"")], error=httpx.HTTPStatusError), 1)
        self.assertEqual(self.check([(200, b"ok")]), 1)

    def test_timeout_and_interrupted_read_are_bounded(self):
        for error in (httpx.ReadTimeout("timeout"), httpx.ReadError("broken body")):
            self.assertEqual(self.check([error], error=type(error)), 3)
            self.assertEqual(self.check([error, error, (200, b"ok")]), 3)

    def test_integrity_is_inside_attempt_and_errors_are_not_swallowed(self):
        bundle = refresh.BundleInfo("config", hashlib.md5(b"ok").hexdigest(), 2)
        self.assertEqual(
            self.check([(200, b"bad"), (200, b"bad"), (200, b"ok")], expected=bundle), 3
        )
        self.assertEqual(self.check([(200, b"bad")], expected=bundle, error=OSError), 3)
        self.assertEqual(self.check([ValueError("bug")], error=ValueError), 1)
        self.assertEqual(
            self.check([asyncio.CancelledError()], error=asyncio.CancelledError), 1
        )

    def test_invalid_attempt_budget_fails_before_request(self):
        for attempts in (0, 4, -1, True):
            with (
                patch.object(refresh, "HTTP_RETRY_ATTEMPTS", attempts),
                self.assertRaisesRegex(ValueError, "between 1 and 3"),
            ):
                refresh._download_bytes("https://official.invalid/config")
