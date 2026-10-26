from fastapi import APIRouter

from app import cache
from app.api.index import IndexHandler
from app.api.upload_check import UploadCheckHandler
from app.api.url_check import UrlCheckHandler
from app.utils.helper import show_json

index_handler = IndexHandler()
url_check_handler = UrlCheckHandler()
upload_check_handler = UploadCheckHandler()

router = APIRouter()

router.get("/")(index_handler.index)

# URL 检测：只保留 POST。
# GET 方式已移除 —— 图片 URL 常带 ? & # 等特殊字符，放 query string 要额外转义，
# 历史上就是坑；而且 GET 的完整 URL 会被浏览器、CDN、访问日志原样记录下来。
# 旧的 GET /check 与 GET /api/url_check 一并移除。
router.post("/api/url_check")(url_check_handler.post_check)

# 上传检测（multipart/form-data）
router.post("/api/upload_check")(upload_check_handler.check)


async def cache_stats():
    """缓存命中情况与数据库状态，用于确认缓存是否真的生效。"""
    return show_json(200, "success", await cache.stats())


router.get("/api/cache_stats")(cache_stats)
