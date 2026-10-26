"""URL 图片检测。只支持 POST，URL 放在 JSON body 里。"""

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

    # 命中缓存直接返回，省掉下载 + 解码 + 推理。
    # 缓存里只有模型输出，is_nsfw 在这里按当前阈值现算，
    # 所以改 NSFW_THRESHOLD 之后历史缓存也会立刻按新阈值判定。
    cached = await cache.get(url, qr_enabled)
    if cached is not None:
        return show_json(
            200,
            "success",
            engine.build_result(cached["sfw"], cached["nsfw"], cached["qr_status"]),
        )

    if not engine.is_ready():
        return show_json(-1000, MODEL_NOT_READY)

    data, err = await engine.fetch_image(url)
    if err:
        return show_json(-1000, err)

    resp = await engine.analyze(data, qr_enabled)
    # 只缓存成功结果：失败不缓存，否则一次网络抖动会污染 7 天
    if resp.get("code") == 200 and resp.get("data"):
        await cache.put(url, qr_enabled, resp["data"])
    return resp


class UrlCheckHandler:
    async def post_check(self, req: dict, qr_detect: str | None = None):
        """POST /api/url_check

        body: {"url": "https://...", "qr_detect": "disable"}   # qr_detect 可省略
        qr_detect 也可以放在 query string 里，body 优先。
        """
        body = req or {}
        url = body.get("url", "")
        # body 里没给就用 query 参数，都没给则默认 disable
        flag = qr_detect if qr_detect is not None else body.get("qr_detect", "disable")
        return await handle(url, flag)
