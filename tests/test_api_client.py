import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import unittest

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
        self.api = ApiClient(f"http://127.0.0.1:{self.server.server_port}/api/v1", self.token)

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

    def test_bad_urls_and_control_characters_are_rejected(self):
        for url in ("https://user:password@example.test/api/v1", "file:///tmp", "https://example.test/?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                ApiClient(url, self.token)
        with self.assertRaises(ValueError):
            ApiClient("https://example.test", "x\r\nother: y")
