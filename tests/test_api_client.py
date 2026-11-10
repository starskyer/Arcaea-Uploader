import json
import ssl
from http.client import IncompleteRead
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest
from unittest.mock import patch
from urllib.error import URLError

from arcaea_uploader.api_client import ApiClient, ApiError


class ApiClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.reply()

            def do_POST(self):
                self.reply()

            def reply(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self.server.calls.append((self.command, self.path, self.headers.get("Token"), body))
                status, payload, headers = self.server.response
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(payload if isinstance(payload, bytes) else json.dumps(payload).encode())

            def log_message(self, *_):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.calls = []
        self.server.response = (200, {"code": 0, "data": {"ok": True}}, {})
        self.token = "private-fixture-token"
        self.api = ApiClient(f"http://127.0.0.1:{self.server.server_port}/api/v1", self.token, use_system_proxy=False)

    def test_token_header_and_json_body_use_actual_wire_format(self):
        result = self.api.request("POST", "/songs", {"name": "测试"})
        self.assertEqual(result, {"ok": True})
        method, path, token, body = self.server.calls[0]
        self.assertEqual((method, path, token), ("POST", "/api/v1/songs", self.token))
        self.assertEqual(json.loads(body), {"name": "测试"})

    def test_only_documented_missing_codes_are_treated_as_absence(self):
        self.server.response = (200, {"code": -2}, {})
        self.assertIsNone(self.api.get_optional("/songs/missing"))
        for status, code in ((404, -2), (401, -1), (200, -999)):
            self.server.response = (status, {"code": code, "msg": self.token}, {})
            with self.subTest(status=status), self.assertRaises(ApiError) as error:
                self.api.get_optional("/songs/a")
            self.assertNotIn(self.token, str(error.exception))

    def test_redirect_does_not_forward_management_credential(self):
        self.server.response = (302, {}, {"Location": "/other"})
        with self.assertRaises(ApiError):
            self.api.request("GET", "/redirect")
        self.assertEqual(len(self.server.calls), 1)

    def test_malformed_and_noninteger_success_codes_are_rejected(self):
        for payload in (b"not json", {"code": False}, {"code": "0"}, {"code": [0]}):
            self.server.response = (200, payload, {})
            with self.subTest(payload=payload), self.assertRaises(ApiError):
                self.api.request("GET", "/system/operations")

    def test_html_504_preserves_status_duration_and_uncertainty_without_retrying(self):
        self.server.response = (504, b"<html>Gateway Time-out private-fixture-token</html>", {})
        with self.assertRaises(ApiError) as caught:
            self.api.request("POST", "/system/operations/refresh_song_file_cache", {})
        error = caught.exception
        self.assertEqual(error.status, 504)
        self.assertIsNone(error.code)
        self.assertTrue(error.uncertain)
        self.assertGreaterEqual(error.elapsed, 0)
        self.assertIn("网关", str(error))
        self.assertIn("服务端可能仍在执行", str(error))
        self.assertNotIn(self.token, str(error))
        self.assertEqual(len(self.server.calls), 1)

    def test_system_posts_allow_longer_wait_without_extending_normal_reads(self):
        with patch.object(self.api._opener, "open", wraps=self.api._opener.open) as request:
            self.api.request("POST", "/system/operations/refresh_song_file_cache", {})
            self.assertEqual(request.call_args.kwargs["timeout"], 360)
            self.api.request("GET", "/system/operations")
            self.assertEqual(request.call_args.kwargs["timeout"], 120)
            self.api.request("POST", "/songs", {})
            self.assertEqual(request.call_args.kwargs["timeout"], 120)

    def test_bad_urls_and_control_characters_are_rejected(self):
        for url in ("https://user:password@example.test/api/v1", "file:///tmp", "https://example.test/?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ApiClient(url, self.token)
        with self.assertRaises(ValueError):
            ApiClient("https://example.test", "x\r\nother: y")

    def test_direct_setting_ignores_detected_proxy_without_changing_global_settings(self):
        proxies = {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"}
        settings = {"api": {"base_url": self.api.base_url, "token_env": "TOKEN", "use_system_proxy": False}}
        with patch("urllib.request.getproxies", return_value=proxies), patch(
                "urllib.request.proxy_bypass", return_value=False), patch(
                "arcaea_uploader.api_client.read_api_token", return_value=self.token):
            direct = ApiClient.from_settings(settings)
            self.assertEqual(direct.request("GET", "/system/operations"), {"ok": True})
        self.assertEqual(direct.proxy_mode, "direct")
        self.assertEqual(proxies["http"], "http://127.0.0.1:1")
        with patch("arcaea_uploader.api_client.read_api_token", return_value=self.token):
            self.assertEqual(ApiClient.from_settings({"api": {"base_url": self.api.base_url, "token_env": "TOKEN"}}).proxy_mode, "system")

    def test_transient_get_failure_retries_once_and_reports_no_raw_error_text(self):
        actual_open = self.api._opener.open
        calls = []
        def reset_once(*args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise URLError(ConnectionResetError(self.token))
            return actual_open(*args, **kwargs)
        with patch.object(self.api._opener, "open", side_effect=reset_once), patch("arcaea_uploader.api_client.sleep"):
            self.assertEqual(self.api.request("GET", "/songs/a"), {"ok": True})
        self.assertEqual(len(calls), 2)
        with patch.object(self.api._opener, "open", side_effect=URLError(ConnectionResetError(self.token))) as request, patch(
                "arcaea_uploader.api_client.sleep"):
            with self.assertRaises(ApiError) as caught:
                self.api.request("GET", "/songs/a")
        self.assertEqual(request.call_count, 2)
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.reason, "ConnectionResetError")
        self.assertEqual(caught.exception.attempts, 2)
        self.assertEqual(caught.exception.proxy_mode, "direct")
        self.assertNotIn(self.token, str(caught.exception))

    def test_connection_loss_never_automatically_replays_a_write(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.subTest(method=method), patch.object(
                    self.api._opener, "open", side_effect=IncompleteRead(b"sensitive partial reply")) as request:
                with self.assertRaises(ApiError) as caught:
                    self.api.request(method, "/songs/a", {})
                self.assertEqual(request.call_count, 1)
                self.assertTrue(caught.exception.uncertain)
                self.assertEqual(caught.exception.reason, "IncompleteRead")
                self.assertNotIn("sensitive", str(caught.exception))

    def test_get_gateway_error_is_bounded_and_authentication_is_not_retried(self):
        self.server.response = (503, {"code": 0}, {})
        with patch("arcaea_uploader.api_client.sleep"), self.assertRaises(ApiError) as caught:
            self.api.request("GET", "/songs/a")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.server.calls), 2)
        self.server.calls = []
        self.server.response = (401, {"code": -1}, {})
        with self.assertRaises(ApiError):
            self.api.request("GET", "/songs/a")
        self.assertEqual(len(self.server.calls), 1)

    def test_certificate_verification_failure_is_not_retried_or_ignored(self):
        failure = URLError(ssl.SSLCertVerificationError(1, self.token))
        with patch.object(self.api._opener, "open", side_effect=failure) as request:
            with self.assertRaises(ApiError) as caught:
                self.api.request("GET", "/songs/a")
        self.assertEqual(request.call_count, 1)
        self.assertEqual(caught.exception.reason, "SSLCertVerificationError")
        self.assertNotIn(self.token, str(caught.exception))
