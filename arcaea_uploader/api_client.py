"""Small Arcaea management API client. Credentials never enter result records."""

import json
import os
import re
import ssl
from http.client import HTTPException
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class ApiError(ValueError):
    def __init__(self, method, path, status=None, code=None, *, elapsed=None, reason=None, proxy_mode=None, attempts=1):
        self.status, self.code = status, code
        self.elapsed = elapsed
        self.reason, self.proxy_mode, self.attempts = reason, proxy_mode, attempts
        self.uncertain = method != "GET" and (status is None or status >= 500 or status == 200 and code in {None, -999})
        message = f"API {method} {path} 失败：HTTP {status or '未确认'}，code={code}"
        if elapsed is not None:
            message += f"，耗时 {elapsed:.1f} 秒"
        if attempts > 1:
            message += f"，已尝试 {attempts} 次"
        message += "。"
        if reason:
            message += f"连接错误类型：{reason}。"
            if proxy_mode:
                message += "连接方式：" + ("跟随系统代理设置。" if proxy_mode == "system" else "直连。")
        if status == 504:
            message += "网关等待上游响应超时，请检查反向代理的等待上限；延长客户端超时无法避免网关提前返回 504。"
            if self.uncertain:
                message += "服务端可能仍在执行或已经完成，请先核对再重试。"
        super().__init__(message)

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
    def __init__(self, base_url, token, *, timeout=120, operation_timeout=360, use_system_proxy=True):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("管理 API 地址应为 HTTP(S) 前缀，不能含账号、查询参数或片段。")
        if not isinstance(token, str) or not token or any(ord(c) < 32 or ord(c) > 126 for c in token):
            raise ValueError("管理 Token 为空或格式无效。")
        if type(use_system_proxy) is not bool:
            raise ValueError("API 使用系统代理必须是布尔值。")
        self.base_url = base_url.rstrip("/")
        self.__token = token
        self.timeout = timeout
        self.operation_timeout = max(timeout, operation_timeout)
        self.proxy_mode = "system" if use_system_proxy else "direct"
        self._opener = build_opener(ProxyHandler() if use_system_proxy else ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_settings(cls, settings):
        return cls(settings["api"]["base_url"], read_api_token(settings["api"]["token_env"]),
                   use_system_proxy=settings["api"].get("use_system_proxy", True))

    def request(self, method, path, body=None):
        if not path.startswith("/") or path.startswith("//") or ".." in path.split("/"):
            raise ValueError("无效的 API 相对路径。")
        started = monotonic()
        # Only reads can be repeated safely after a connection failure. Never
        # replay POST/PUT/PATCH: the server may already have committed them.
        for attempt in range(1, 3 if method == "GET" else 2):
            try:
                return self._request_once(method, path, body, started, attempt)
            except ApiError as error:
                retryable = error.status is None or error.status in {502, 503, 504}
                if method != "GET" or attempt == 2 or not retryable or error.reason == "SSLCertVerificationError":
                    raise
                sleep(0.3)

    def _request_once(self, method, path, body, started, attempt):
        data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = Request(self.base_url + path, data=data, method=method,
                          headers={"Token": self.__token, "Content-Type": "application/json"})
        timeout = self.operation_timeout if method == "POST" and path.startswith("/system/operations/") else self.timeout
        try:
            try:
                response = self._opener.open(request, timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                try:
                    payload = json.load(response)
                except (ValueError, UnicodeError):
                    raise ApiError(method, path, status, elapsed=monotonic() - started, attempts=attempt) from None
            code = payload.get("code") if isinstance(payload, dict) else None
            code = code if type(code) is int else None
            if status != 200 or type(code) is not int or code != 0:
                raise ApiError(method, path, status, code, elapsed=monotonic() - started, attempts=attempt)
            return payload.get("data")
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            reason = error.reason if isinstance(error, URLError) else error
            # Only the exception type is exposed; exception strings can contain
            # proxy credentials or URLs supplied by the local network stack.
            reason_type = "SSLCertVerificationError" if isinstance(reason, ssl.SSLCertVerificationError) else type(reason).__name__
            raise ApiError(method, path, elapsed=monotonic() - started, reason=reason_type,
                           proxy_mode=self.proxy_mode, attempts=attempt) from None

    def get_optional(self, path):
        try:
            return self.request("GET", path)
        except ApiError as error:
            if error.missing:
                return None
            raise
