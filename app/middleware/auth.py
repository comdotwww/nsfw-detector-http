"""鉴权中间件。"""

import hmac
import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse

# 启动时读一次，避免每个请求都查环境变量
_TOKEN = os.getenv("TOKEN", "")
# 首页与健康检查不鉴权
_PUBLIC_PREFIXES = ("/docs", "/openapi.json", "/redoc")

_INVALID_BODY = {"code": 401, "msg": "Token invalid"}


def _invalid() -> JSONResponse:
    return JSONResponse(status_code=401, content=_INVALID_BODY)


async def auth(request: Request, call_next: Callable):
    if not _TOKEN:
        return await call_next(request)

    path = request.url.path
    if path == "/" or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    header = request.headers.get("Authorization")
    if not header:
        return _invalid()

    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return _invalid()

    # 定长比较，避免时序侧信道
    if not hmac.compare_digest(parts[1], _TOKEN):
        return _invalid()

    return await call_next(request)
