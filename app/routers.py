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
# 兼容旧接口
router.get("/check")(url_check_handler.check)
router.get("/api/url_check")(url_check_handler.check)
# 支持POST方式的URL检测
router.post("/api/url_check")(url_check_handler.post_check)
# 上传检测
router.post("/api/upload_check")(upload_check_handler.check)


async def cache_stats():
    """缓存命中情况，用于确认缓存是否真的生效。"""
    return show_json(200, "success", cache.stats())


router.get("/api/cache_stats")(cache_stats)
