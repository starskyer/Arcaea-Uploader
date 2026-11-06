"""Small Arcaea management API client. Credentials never enter result records."""

import json
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ApiError(ValueError):
    def __init__(self, method, path, status=None, code=None):
        self.status, self.code = status, code
        self.uncertain = method != "GET" and (status is None or status >= 500 or status == 200 and code in {None, -999})
        super().__init__(f"API {method} {path} 失败：HTTP {status or '未确认'}，code={code}。")

    @property
    def missing(self):
        return self.status == 200 and self.code in {-2, -121}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_api_token(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("Token 环境变量名称不合法。")
    token = os.environ.get(name)
    if not token and os.name == "nt":
        # A running IDE may predate a user environment update. Read the persisted
        # user value only when this process has no explicit environment override.
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                token, _ = winreg.QueryValueEx(key, name)
        except FileNotFoundError:
            pass
    if not isinstance(token, str) or not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
        raise ValueError(f"未找到可用的环境变量 {name}，请先配置管理 Token。")
    return token


class ApiClient:
    def __init__(self, base_url, token, *, timeout=120):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("管理 API 地址应为 HTTP(S) 前缀，不能含账号、查询参数或片段。")
        if not isinstance(token, str) or not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ValueError("管理 Token 为空或格式无效。")
        self.base_url = base_url.rstrip("/")
        self.__token = token
        self.timeout = timeout
        self._opener = build_opener(_NoRedirect())

    @classmethod
    def from_settings(cls, settings):
        return cls(settings["api"]["base_url"], read_api_token(settings["api"]["token_env"]))

    def request(self, method, path, body=None):
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise ValueError("无效的 API 相对路径。")
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Token": self.__token, "Content-Type": "application/json"})
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                try:
                    payload = json.load(response)
                except (ValueError, UnicodeError):
                    raise ApiError(method, path, status) from None
            code = payload.get("code") if isinstance(payload, dict) else None
            code = code if type(code) is int else None
            if status != 200 or type(code) is not int or code != 0:
                raise ApiError(method, path, status, code)
            return payload.get("data")
        except (URLError, TimeoutError, OSError):
            raise ApiError(method, path) from None

    def get_optional(self, path):
        try:
            return self.request("GET", path)
        except ApiError as error:
            if error.missing:
                return None
            raise
