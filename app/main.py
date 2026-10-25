import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import engine
from app.api import index as index_api
from app.middleware.auth import auth
from app.routers import router as my_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：加载模型 + 预热，避免首次请求承担权重预打包开销
    engine.init()
    index_api.preload()
    try:
        yield
    finally:
        await engine.close_http()


# 默认关闭 /docs 与 /openapi.json：低配机器上没必要为它生成并驻留 schema
_DOCS_ON = os.getenv("ENABLE_DOCS", "off").lower() == "on"

app = FastAPI(
    lifespan=lifespan,
    docs_url="/docs" if _DOCS_ON else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _DOCS_ON else None,
    # 自带 Server 头对性能无影响，但少一点指纹信息
    swagger_ui_oauth2_redirect_url=None,
)

app.middleware("http")(auth)  # 鉴权中间件

# CORS：默认保持原来的全放开行为，可用 CORS_ORIGINS 收窄（如 https://a.com,https://b.com）
_cors = os.getenv("CORS_ORIGINS", "*")
if _cors.strip() and _cors.strip() != "off":
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=False,
        allow_origins=[o.strip() for o in _cors.split(",") if o.strip()],
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        max_age=86400,
    )

app.include_router(my_router)
