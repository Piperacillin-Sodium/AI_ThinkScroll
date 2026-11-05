"""Small GUI-independent WebDAV client for config-only backup and restore."""
from __future__ import annotations

from urllib.parse import quote, urlsplit
from xml.etree import ElementTree


MAX_CONFIG_BYTES = 5_000_000


class WebDAVClient:
    def __init__(self, base_url, username, password):
        import requests

        self.base_url = str(base_url).strip().rstrip("/") + "/"
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("WebDAV 地址必须是完整的 http:// 或 https:// 地址。")
        if parsed.username or parsed.password or parsed.fragment or parsed.query:
            raise ValueError("WebDAV 地址不能包含登录信息、查询参数或片段；请使用单独的用户名和密码。")
        self.session = requests.Session()
        self.session.auth = (str(username), str(password))
        self.session.headers.update({"User-Agent": "AI-NovelGenerator-Web/1.0", "Accept": "*/*"})

    def close(self):
        self.session.close()

    def _request(self, method, path="", **kwargs):
        import requests

        url = self.base_url + quote(path.lstrip("/"), safe="/")
        try:
            return self.session.request(method, url, timeout=(5, 30), **kwargs)
        except requests.exceptions.RequestException as exc:
            # Requests exception text may contain a URL with credentials.
            raise RuntimeError(f"WebDAV 请求失败（{type(exc).__name__}），请检查地址、代理和登录信息。") from None

    @staticmethod
    def _check(response, expected):
        if response.status_code not in expected:
            raise RuntimeError(f"WebDAV 返回 HTTP {response.status_code}，请检查地址、权限和登录信息。")

    def test(self):
        response = self._request("PROPFIND", headers={"Depth": "0"})
        try:
            self._check(response, {200, 207})
        finally:
            response.close()

    def ensure_directory(self, path):
        response = self._request("PROPFIND", path.rstrip("/") + "/", headers={"Depth": "0"})
        try:
            if response.status_code in {200, 207}:
                try:
                    root = ElementTree.fromstring(response.content)
                except ElementTree.ParseError:
                    raise ValueError("WebDAV 返回的目录信息无效。") from None
                if root.find(".//{DAV:}collection") is None:
                    raise ValueError("WebDAV 备份路径存在，但不是目录。")
                return
            self._check(response, {404})
        finally:
            response.close()
        response = self._request("MKCOL", path.rstrip("/") + "/")
        try:
            self._check(response, {200, 201, 204})
        finally:
            response.close()

    def upload(self, path, content):
        response = self._request("PUT", path, data=content, headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            self._check(response, {200, 201, 204})
        finally:
            response.close()

    def download(self, path, check_cancel=lambda: None):
        response = self._request("GET", path, stream=True)
        try:
            self._check(response, {200})
            chunks, total = [], 0
            for chunk in response.iter_content(chunk_size=8192):
                check_cancel()
                total += len(chunk)
                if total > MAX_CONFIG_BYTES:
                    raise ValueError("远程配置超过 5 MB，已取消恢复。")
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            response.close()
