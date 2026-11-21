"""Minimal HTTP client for the local gateway."""

from __future__ import annotations

import http.client
import json as jsonlib
import math
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING, Any

from agent_router.statusline import _NoRedirect

if TYPE_CHECKING:
    import httpx

# the server caps a wait at 110 s; the read timeout needs a margin above that
WAIT_CAP_S = 110.0
WAIT_READ_MARGIN_S = 15.0


class GatewayUnavailable(Exception): ...


class GatewayError(Exception):
    status: int
    code: str | None
    message: str

    def __init__(self, status: int, code: str | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class _UrllibResponse:
    def __init__(self, status_code: int, body: bytes) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> Any:
        return jsonlib.loads(self._body)


class _UrllibClient:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: tuple[float, float] = (5.0, 5.0),
    ) -> _UrllibResponse:
        if params:
            query = urllib.parse.urlencode(
                {name: value for name, value in params.items() if value is not None}
            )
            if query:
                url += ("&" if "?" in url else "?") + query
        request_headers = dict(headers)
        data = None
        if json is not None:
            data = jsonlib.dumps(json, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            response = self._opener.open(request, timeout=timeout[1])
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read()
            finally:
                exc.close()
            return _UrllibResponse(exc.code, body)
        try:
            return _UrllibResponse(response.status, response.read())
        finally:
            response.close()


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        client: httpx.Client | None = None,
        timeout: float = 5,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.client = client or _UrllibClient()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict:
        timeout = kwargs.pop("timeout", None)
        httpx = sys.modules.get("httpx")
        if isinstance(self.client, _UrllibClient):
            kwargs["timeout"] = timeout or (self.timeout, self.timeout)
        elif timeout is not None:
            # the injected client (httpx, TestClient) keeps its own timeout; only the
            # read timeout for a long wait is adjusted
            connect, read = timeout
            kwargs["timeout"] = timeout if httpx is None else httpx.Timeout(connect, read=read)
        errors = (urllib.error.URLError, OSError, TimeoutError, http.client.HTTPException)
        if httpx is not None:
            errors += (httpx.HTTPError,)
        try:
            response = self.client.request(
                method,
                self.base_url + path,
                headers={"Authorization": f"Bearer {self.token}"},
                **kwargs,
            )
        except errors as exc:
            raise GatewayUnavailable("gateway unavailable") from exc
        if 300 <= response.status_code < 400:
            raise GatewayError(
                response.status_code,
                None,
                f"gateway rejected the request: HTTP {response.status_code}",
            )
        if response.status_code >= 400:
            code = None
            message = f"gateway rejected the request: HTTP {response.status_code}"
            try:
                error = response.json().get("error", {})
            except (ValueError, AttributeError):
                error = {}
            if isinstance(error, dict):
                raw_code = error.get("code")
                raw_message = error.get("message")
                code = raw_code if isinstance(raw_code, str) else None
                if isinstance(raw_message, str):
                    message = raw_message
            raise GatewayError(response.status_code, code, message)
        try:
            payload = response.json()
        except ValueError as exc:
            raise GatewayError(response.status_code, None, "gateway returned non-JSON") from exc
        if not isinstance(payload, dict):
            raise GatewayError(response.status_code, None, "gateway returned non-object JSON")
        return payload

    def get(self, path: str, **params: Any) -> dict:
        query = {name: value for name, value in params.items() if value is not None}
        extra: dict[str, Any] = {}
        wait_s = query.get("timeout")
        if path.endswith("/wait") and isinstance(wait_s, int | float) and math.isfinite(wait_s):
            # long wait: the default read timeout (5 s) will not be enough
            read = min(max(float(wait_s), 0.0), WAIT_CAP_S) + WAIT_READ_MARGIN_S
            extra["timeout"] = (self.timeout, read)
        return self._request("GET", path, params=query, **extra)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self._request("POST", path, json={} if body is None else body)
