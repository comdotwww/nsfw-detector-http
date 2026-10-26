"""鉴权中间件（SQLite 多 token 版）。

校验逻辑：
1. 白名单路径与 /api/auth/* 管理 API 不鉴权（管理 API 自己的鉴权在 admin 端口处理）。
2. 如果有遗留的 TOKEN 环境变量，且请求携带的 Bearer 与之一致 → 放行（兼容旧配置）。
3. 否则查 tokens 表：校验哈希 + 启用状态 + 有效期 + 每日/总量上限，
   通过后**记账**（total_used+1、当日 count+1）。

fail-closed：token 存储异常时拒绝所有受保护请求，避免 DB 故障导致无鉴权放行。
"""

import hmac
import logging
import os
from typing import Callable

from fastapi import Request
from fastapi.responses import JSONResponse

from app import tokens

log = logging.getLogger("nsfw.auth")

# 遗留的单一 TOKEN 环境变量；为空则完全禁用这种兼容方式
_LEGACY_TOKEN = os.getenv("TOKEN", "")

# 不鉴权的路径
_PUBLIC_PREFIXES = ("/docs", "/openapi.json", "/redoc",
                    "/api/cache_stats", "/api/auth")

_INVALID_BODY = {"code": 401, "msg": "Token invalid"}
_LIMIT_BODY = {"code": 403, "msg": "Token limit exceeded"}


def _resp(status: int, body: dict) -> JSONResponse:
    return JSONResponse(status_code=status, content=body)


def _extract_bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization")
    if not header:
        return None
    parts = header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1]


async def auth(request: Request, call_next: Callable):
    path = request.url.path

    # 白名单直接放行
    if path == "/" or path.startswith(_PUBLIC_PREFIXES):
        return await call_next(request)

    raw = _extract_bearer(request)

    # 1) 遗留 TOKEN 兼容（常量时间比较）
    if raw is not None and _LEGACY_TOKEN and hmac.compare_digest(raw, _LEGACY_TOKEN):
        return await call_next(request)

    # 2) SQLite 多 token 鉴权 + 记账。
    # 注意 raw 可能是 None：tokens.authenticate 内部会判断
    # 「表里没有任何 token」→ 公开模式放行（兼容旧版不鉴权语义）。
    result = await tokens.authenticate(raw)
    if result is None:
        return _resp(401, _INVALID_BODY)
    if not result["ok"]:
        reason = result.get("reason", "")
        if reason in ("daily_limit", "total_limit"):
            return _resp(403, _LIMIT_BODY)
        # disabled / expired / invalid 统一 401
        return _resp(401, _INVALID_BODY)

    return await call_next(request)