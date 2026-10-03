"""
Tests for awning_controller.py — the Bond Local API v2 client.

Until now nothing exercised this module: the automation tests replace the whole
controller with a MagicMock, so the URL, HTTP method, auth header, retry policy
and error wrapping were all untested. These tests run the real controller against
a throwaway HTTP server on 127.0.0.1, which is the only honest way to test the
urllib3 retry configuration without adding a dependency.

Run:  python3 -m unittest test_awning_controller -v
"""
import json
import os
import tempfile
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import awning_controller
from awning_controller import (
    BondAPIError,
    BondAwningController,
    ConfigurationError,
    create_controller_from_env,
    load_config,
)


class _FakeBond:
    """A scriptable stand-in for a Bond Bridge. Records every request it gets."""

    def __init__(self):
        self.requests = []          # (method, path, headers, body)
        self.script = []            # status codes to return, in order; then default
        self.default_status = 200
        self.state_body = b'{"open": 1}'
        self.server = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append((self.command, self.path, dict(self.headers), body))
                status = outer.script.pop(0) if outer.script else outer.default_status
                payload = outer.state_body if self.path.endswith("/state") else b"{}"
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_PUT = _handle

            def log_message(self, *args):  # silence the test output
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f"127.0.0.1:{self.server.server_address[1]}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


class BondControllerTestCase(unittest.TestCase):
    def setUp(self):
        # Retries are real, but with no sleeping between them.
        patcher = unittest.mock.patch.object(awning_controller, "_BOND_RETRY_BACKOFF_FACTOR", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bond = _FakeBond()
        self.host = self.bond.start()
        self.addCleanup(self.bond.stop)
        self.controller = BondAwningController(self.host, "secret-token", "dev42", timeout=5)


class TestRequestShape(BondControllerTestCase):
    def test_open_is_a_put_with_token_and_empty_json_body(self):
        self.controller.open()
        method, path, headers, body = self.bond.requests[0]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "/v2/devices/dev42/actions/Open")
        self.assertEqual(headers.get("BOND-Token"), "secret-token")
        self.assertEqual(json.loads(body), {})

    def test_every_action_maps_to_its_endpoint(self):
        for call, action in (("open", "Open"), ("close", "Close"),
                             ("stop", "Stop"), ("toggle", "ToggleOpen")):
            with self.subTest(call=call):
                self.bond.requests.clear()
                getattr(self.controller, call)()
                self.assertEqual(
                    self.bond.requests[0][1], f"/v2/devices/dev42/actions/{action}"
                )

    def test_state_and_info_are_gets_on_the_right_paths(self):
        self.controller.get_state()
        self.controller.get_info()
        self.assertEqual([(r[0], r[1]) for r in self.bond.requests],
                         [("GET", "/v2/devices/dev42/state"), ("GET", "/v2/devices/dev42")])
        self.assertTrue(all(r[2].get("BOND-Token") == "secret-token" for r in self.bond.requests))


class TestGetState(BondControllerTestCase):
    def test_open_closed_and_missing(self):
        for body, expected in ((b'{"open": 1}', 1), (b'{"open": 0}', 0), (b"{}", None)):
            with self.subTest(body=body):
                self.bond.state_body = body
                self.assertEqual(self.controller.get_state(), expected)

    def test_non_dict_reply_is_a_bond_error_not_an_attribute_error(self):
        for body in (b"[1, 2]", b'"open"', b"null"):
            with self.subTest(body=body):
                self.bond.state_body = body
                with self.assertRaises(BondAPIError):
                    self.controller.get_state()

    def test_unparseable_reply_is_a_bond_error(self):
        self.bond.state_body = b"<html>not json</html>"
        with self.assertRaises(BondAPIError):
            self.controller.get_state()


class TestRetryPolicy(BondControllerTestCase):
    def test_transient_503_is_retried_until_it_succeeds(self):
        self.bond.script = [503, 503]
        self.controller.open()
        self.assertEqual(len(self.bond.requests), 3)

    def test_put_is_retried_too(self):
        """Open/Close/Stop are idempotent at the limit switch, so retrying PUT is safe."""
        self.bond.script = [500]
        self.controller.close()
        self.assertEqual(len(self.bond.requests), 2)
        self.assertTrue(all(r[0] == "PUT" for r in self.bond.requests))

    def test_exhausted_retries_raise_bond_api_error(self):
        self.bond.default_status = 503
        with self.assertRaises(BondAPIError) as ctx:
            self.controller.open()
        self.assertIn("Open", str(ctx.exception))
        self.assertEqual(len(self.bond.requests), awning_controller._BOND_RETRY_TOTAL + 1)

    def test_log_never_claims_a_retry_beyond_the_budget(self):
        """The old log said 'retrying (attempt 6/5)' on the final failure."""
        self.bond.default_status = 503
        with self.assertLogs(awning_controller.logger, level="WARNING") as logs:
            with self.assertRaises(BondAPIError):
                self.controller.open()
        retry_lines = [m for m in logs.output if "retrying" in m]
        total = awning_controller._BOND_RETRY_TOTAL
        self.assertEqual(len(retry_lines), total)
        self.assertTrue(any(f"attempt {total}/{total}" in m for m in retry_lines))
        self.assertFalse(any(f"attempt {total + 1}/" in m for m in retry_lines))

    def test_client_errors_are_not_retried(self):
        """A wrong token (401) will not fix itself; retrying only delays the alert."""
        for status in (401, 404):
            with self.subTest(status=status):
                self.bond.requests.clear()
                self.bond.default_status = status
                with self.assertRaises(BondAPIError):
                    self.controller.open()
                self.assertEqual(len(self.bond.requests), 1)

    def test_unreachable_bridge_raises_bond_api_error(self):
        self.bond.stop()
        controller = BondAwningController(self.host, "t", "d", timeout=2)
        with self.assertRaises(BondAPIError):
            controller.get_state()


class TestLoadConfig(unittest.TestCase):
    def _env_file(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = Path(d.name) / ".env"
        path.write_text(text)
        return path

    def test_loads_all_three_from_an_explicit_file(self):
        path = self._env_file("BOND_TOKEN=tok\nBOND_HOST=10.0.0.9\nDEVICE_ID=abc\n")
        with unittest.mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(load_config(path), ("10.0.0.9", "tok", "abc"))

    def test_each_missing_or_blank_variable_is_named_in_the_error(self):
        base = {"BOND_TOKEN": "tok", "BOND_HOST": "h", "DEVICE_ID": "d"}
        for missing in base:
            for value in (None, "   "):
                with self.subTest(missing=missing, value=value):
                    env = dict(base)
                    if value is None:
                        del env[missing]
                    else:
                        env[missing] = value
                    with unittest.mock.patch.dict(os.environ, env, clear=True):
                        with self.assertRaises(ConfigurationError) as ctx:
                            load_config(Path("/nonexistent/.env"))
                    self.assertIn(missing, str(ctx.exception))

    def test_values_are_stripped(self):
        env = {"BOND_TOKEN": " tok ", "BOND_HOST": " 10.0.0.9 ", "DEVICE_ID": " abc "}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_config(Path("/nonexistent/.env")), ("10.0.0.9", "tok", "abc"))

    def test_an_already_exported_variable_beats_the_file(self):
        """python-dotenv does not override; a stale shell export would win. Pinned."""
        path = self._env_file("BOND_TOKEN=from-file\nBOND_HOST=h\nDEVICE_ID=d\n")
        with unittest.mock.patch.dict(os.environ, {"BOND_TOKEN": "from-shell"}, clear=True):
            self.assertEqual(load_config(path)[1], "from-shell")

    def test_create_controller_builds_the_documented_base_url(self):
        env = {"BOND_TOKEN": "tok", "BOND_HOST": "10.0.0.9", "DEVICE_ID": "abc"}
        with unittest.mock.patch.dict(os.environ, env, clear=True):
            controller = create_controller_from_env(Path("/nonexistent/.env"))
        self.assertEqual(controller.base_url, "http://10.0.0.9/v2/devices/abc")


if __name__ == "__main__":
    unittest.main()
