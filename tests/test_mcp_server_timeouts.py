"""A slow engine read-timeout must not be reported as 'engine unreachable'."""

from __future__ import annotations

import importlib.util
import io
import json
import socket
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

_SRC = Path(__file__).resolve().parent.parent / "mcp_server.py"


def _load():
    spec = importlib.util.spec_from_file_location("mcp_server_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Timeouts(unittest.TestCase):
    def setUp(self):
        self.ms = _load()
        self.sleeps: list[float] = []
        p = mock.patch("time.sleep", lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)

    def _fail_with(self, exc):
        calls = []

        def fake(req, timeout=None):
            calls.append(timeout)
            raise exc

        p = mock.patch.object(self.ms.urllib.request, "urlopen", fake)
        p.start()
        self.addCleanup(p.stop)
        return calls

    def test_read_timeout_no_retry(self):
        for exc in (TimeoutError("timed out"), socket.timeout("timed out")):
            with self.subTest(exc=exc):
                self.sleeps.clear()
                calls = self._fail_with(exc)
                res = self.ms._post("/api/library/ask", {"q": "x"})
                self.assertEqual(len(calls), 1)
                self.assertEqual(self.sleeps, [])
                self.assertIn("engine reachable", res["_error"])
                self.assertIn("180s", res["_error"])
                self.assertNotIn("unreachable", res["_error"])

    def test_connection_failure_retries_then_down(self):
        for exc in (
            ConnectionRefusedError(10061, "refused"),
            urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
            urllib.error.URLError(TimeoutError("timed out")),
        ):
            with self.subTest(exc=exc):
                self.sleeps.clear()
                calls = self._fail_with(exc)
                res = self.ms._post("/api/library/ask", {"q": "x"})
                self.assertEqual(len(calls), 4)
                self.assertEqual(self.sleeps, [2, 4, 8])
                self.assertIn("engine unreachable", res["_error"])

    def test_http_error_no_retry(self):
        calls = self._fail_with(urllib.error.HTTPError("http://x", 500, "boom", {}, None))
        res = self.ms._get("/api/library/status")
        self.assertEqual(len(calls), 1)
        self.assertIn("engine answered 500", res["_error"])

    def test_url_from_env(self):
        with mock.patch.dict("os.environ", {"LIBRARIAN_URL": "http://example.test:9/"}):
            self.assertEqual(_load().ENGINE_URL, "http://example.test:9")


class _Resp:
    def __init__(self, body: bytes):
        self._b = body

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Formatting(unittest.TestCase):
    def setUp(self):
        self.ms = _load()

    def test_call_body_passes_4xx_reason_through(self):
        seen = {}

        def fake(req, timeout=None):
            seen.update(method=req.get_method(), body=req.data, timeout=timeout)
            raise urllib.error.HTTPError(
                "http://x", 400, "bad", {}, io.BytesIO(b'{"detail": {"reason": "no PII"}}')
            )

        with mock.patch.object(self.ms.urllib.request, "urlopen", fake):
            res = self.ms._call_body("POST", "/api/library/guide", {"text": "t"})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(json.loads(seen["body"]), {"text": "t"})
        self.assertEqual(seen["timeout"], 30)
        self.assertEqual(res["reason"], "no PII")
        self.assertEqual(res["_status"], 400)

    def test_guide_rejection_text_reaches_caller(self):
        with mock.patch.object(
            self.ms, "_call_body", return_value={"stored": False, "reason": "rejected"}
        ):
            self.assertEqual(self.ms.librarian_guide("some text"), "rejected")

    def test_busy_text(self):
        self.assertIsNone(self.ms._busy_text({}))
        out = self.ms._busy_text({"busy": True, "position": 2, "eta_s": 12.0})
        self.assertIn("position 2", out)
        self.assertIn("~12s", out)

    def test_locate_formatting(self):
        payload = {
            "source": "index",
            "hits": [
                {"kind": "function", "name": "require_session", "file": "src/auth.py",
                 "line": 24, "signature": "(req)", "stale": True},
                {"kind": "entry", "name": "auth-middleware", "file": "", "depends_on": "session-store"},
            ],
            "excluded_hits": 2,
        }
        with mock.patch.object(self.ms, "_get", return_value=payload):
            out = self.ms.librarian_locate("auth")
        self.assertIn("- [function] require_session -> src/auth.py:24  (req) [changed - re-verify]", out)
        self.assertIn("depends_on: session-store", out)
        self.assertIn("2 hit(s) withheld", out)


if __name__ == "__main__":
    unittest.main()
