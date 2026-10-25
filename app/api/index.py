"""首页。WEBUI 的 HTML 在启动时读一次进内存，不再每个请求都读盘。"""

import os

from fastapi.responses import HTMLResponse

INDEX_HTML_PATH = os.getenv("INDEX_HTML_PATH", "app/static/html/index.html")

FALLBACK_HTML = (
    "<h3>NSFW Detection API is running.</h3>"
    "<p>Please visit <a href='https://github.com/comdotwww/nsfw-detector-http' target='_blank'>"
    "Github</a> to view the usage instructions.</p>"
)

_html_cache: str | None = None


def webui_enabled() -> bool:
    return os.getenv("WEBUI", "off").lower() == "on"


def preload() -> None:
    """启动时调用，把 WEBUI 页面读进内存。"""
    global _html_cache
    if not webui_enabled():
        return
    try:
        with open(INDEX_HTML_PATH, "r", encoding="utf-8") as f:
            _html_cache = f.read()
    except OSError:
        _html_cache = None


def get_html() -> str | None:
    global _html_cache
    if _html_cache is None:
        preload()
    return _html_cache


class IndexHandler:
    async def index(self):
        if webui_enabled():
            html = get_html()
            if html:
                return HTMLResponse(content=html)
        return HTMLResponse(content=FALLBACK_HTML, status_code=200)
