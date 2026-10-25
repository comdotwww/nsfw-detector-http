"""URL 图片检测。GET / POST 两条路径共用同一套逻辑。"""

import re

from app import cache, engine
from app.utils.helper import show_json

MODEL_NOT_READY = "Model not initialized, please check model_q4.onnx exists and is loadable."


def _is_valid_url(url) -> bool:
    # 只允许 http/https
    if not isinstance(url, str) or not url:
        return False
    return re.match(r"^https?://", url) is not None


def _parse_qr_flag(qr_detect: str):
    """返回 (是否启用, 错误信息)"""
    flag = (qr_detect or "disable").lower()
    if flag not in ("enable", "disable"):
        return False, "Invalid qr_detect value, only 'enable' or 'disable' are supported."
    return flag == "enable", ""


async def handle(url: str, qr_detect: str = "disable"):
    if not _is_valid_url(url):
        return show_json(-1000, "Invalid URL, only http/https are supported.")

    qr_enabled, err = _parse_qr_flag(qr_detect)
    if err:
        return show_json(-1000, err)

    # 命中缓存直接返回，省掉下载 + 解码 + 推理
    cached = cache.get(url, qr_enabled)
    if cached is not None:
        return show_json(200, "success", cached)

    if not engine.is_ready():
        return show_json(-1000, MODEL_NOT_READY)

    data, err = await engine.fetch_image(url)
    if err:
        return show_json(-1000, err)

    resp = await engine.analyze(data, qr_enabled)
    # 只缓存成功结果：失败不缓存，否则一次网络抖动会污染 7 天
    if resp.get("code") == 200 and resp.get("data"):
        cache.put(url, qr_enabled, resp["data"])
    return resp


class UrlCheckHandler:
    async def check(self, url: str, qr_detect: str = "disable"):
        return await handle(url, qr_detect)

    # POST 方式，参数放在 JSON body 里（URL 含特殊字符时用这个）
    async def post_check(self, req: dict, qr_detect: str = "disable"):
        url = (req or {}).get("url", "")
        return await handle(url, qr_detect)
