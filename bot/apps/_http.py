"""Small JSON transport shared by application adapters."""

import json
from http.client import HTTPException
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class ServiceError(RuntimeError):
    """A remote service did not return a usable result."""


def request_json(url: str, *, headers: dict | None = None, body: bytes | None = None) -> dict:
    try:
        request = Request(url, data=body, headers=headers or {})
        authorization = request.get_header("Authorization")
        if authorization is not None:
            request.remove_header("Authorization")
            request.add_unredirected_header("Authorization", authorization)
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except HTTPError as exc:
        raise ServiceError(f"HTTP {exc.code}") from exc
    except (OSError, HTTPException) as exc:
        raise ServiceError("网络连接或响应超时") from exc
    except ValueError as exc:
        raise ServiceError("服务返回了无效的 JSON 或请求地址") from exc
    if not isinstance(data, dict):
        raise ServiceError("服务返回了无效的响应结构")
    return data
