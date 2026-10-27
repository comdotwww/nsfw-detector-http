"""推理引擎：统一管理 ONNX 会话、图像预处理、HTTP 客户端与二维码检测。

模型：Marqo/nsfw-image-detection-384（ViT-tiny，5.6M 参数，int8 量化后 6.4MB）。
相比原来的 AdamCodd/vit-base（86M 参数，q4 量化 55MB），
CPU 单张推理从 ~178ms 降到 ~16ms（16 线程），体积小 8.6 倍。

设计要点（针对低配部署目标）：
1. ONNX 会话全局唯一，线程数由环境变量控制，避免 worker 之间抢核；
2. 预处理走 draft 缩解码 + uint8 转置 + LUT 归一化，大图快约 2x；
3. httpx 客户端复用连接池，URL 检测只发一次流式 GET（原来 HEAD+GET 两次）；
4. CPU 密集的推理与二维码识别一律丢进线程池，不阻塞事件循环；
5. opencv 依赖已移除，二维码检测直接用 zxingcpp 读 PIL 图像，省 ~14MB 内存和二次解码。
"""

from __future__ import annotations

import asyncio
import io
import math
import os
from typing import Optional, Tuple

import httpx
import numpy as np
import onnxruntime as ort
from PIL import Image

# --------------------------------------------------------------------------
# 常量与配置
# --------------------------------------------------------------------------

MODEL_PATH = os.getenv("MODEL_PATH", "app/onnx/model_nsfw_int8.onnx")
INPUT_SIZE = 384

# 单张图片上限：对 384x384 分类器来说 10MB 已经很宽松，低配机器可调小
MAX_SIZE = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))

# 判定阈值：nsfw 概率大于该值才标记 is_nsfw。
# 注意这是「严格大于」，阈值 0.8 时 0.8 本身不算 NSFW。
# 调高 = 更宽松（漏检变多），调低 = 更严格（误判变多）。
# 只影响 is_nsfw 这个布尔值，返回的 nsfw 概率始终是 0~1 原始值。
#
# 默认 0.8（原来是 0.9）：Marqo 模型的分值分布比原 AdamCodd 偏低，
# 实测 NSFW 样本集中在 0.87~0.95，沿用 0.9 会偏严、漏掉擦边内容。
# 换模型后建议用自己的业务样本重新标定这个值。
NSFW_THRESHOLD = float(os.getenv("NSFW_THRESHOLD", "0.8"))

# 解压炸弹防护：10MB 的 PNG 可以解出上亿像素，直接打爆内存
MAX_PIXELS = int(os.getenv("MAX_IMAGE_PIXELS", "64000000"))  # ~8000x8000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

ALLOWED_MIME = {
    "image/jpeg",
    "image/png",
    "image/bmp",
    "image/webp",
    "image/gif",
}

FORMAT_TO_MIME = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "BMP": "image/bmp",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

# 拉图超时。connect 单独给是因为境外 CDN 建连经常要好几秒：
# 实测 cdn.jliji.com 在拥塞时段建连就要 5.5s，原来 5s 的 connect 会直接
# 误判成超时（报 "Request timed out"）。这里放宽到 10s。
# 总超时（读 body）给 30s，足够拉完 10MB。
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "30"))
HTTP_CONNECT_TIMEOUT = float(os.getenv("HTTP_CONNECT_TIMEOUT", "10"))
# 是否继承系统代理（HTTP_PROXY/HTTPS_PROXY）。默认保持 httpx 的行为。
# 如果服务器上配了代理但拉图不需要走代理，置 0 可以少一跳、也避免内网地址被代理掉。
HTTP_TRUST_ENV = os.getenv("HTTP_TRUST_ENV", "1") == "1"


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "")
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# 每个进程的推理线程数；run.sh 会按 核数/worker 数 算好传进来
ORT_THREADS = _int_env("ORT_NUM_THREADS", 0) or (os.cpu_count() or 1)
# 同时进入推理的请求数上限。超过只是排队，避免线程数翻倍导致互相拖慢。
INFER_CONCURRENCY = _int_env("INFER_CONCURRENCY", 0) or ORT_THREADS
# 空闲时让 ORT 工作线程休眠而不是自旋。实测对单次延迟无实质影响（±7% 内属噪声），
# 但能省下空闲期被自旋吃掉的 CPU —— 低配/共享主机上很值。
ORT_ALLOW_SPINNING = os.getenv("ORT_ALLOW_SPINNING", "0") == "1"

# uint8 -> [-1, 1] 的查找表，等价于 (v/255 - 0.5)/0.5，但只需一次索引
_LUT = (np.arange(256, dtype=np.float32) / 127.5) - 1.0

# --------------------------------------------------------------------------
# ONNX 会话
# --------------------------------------------------------------------------

_session: Optional[ort.InferenceSession] = None
_input_name: Optional[str] = None


def _build_session() -> ort.InferenceSession:
    opts = ort.SessionOptions()
    # 单次请求内部并行用 intra_op；层与层之间顺序执行即可
    opts.intra_op_num_threads = max(1, ORT_THREADS)
    opts.inter_op_num_threads = 1
    opts.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    opts.enable_mem_pattern = True
    opts.enable_cpu_mem_arena = True
    if not ORT_ALLOW_SPINNING:
        opts.add_session_config_entry("session.intra_op.allow_spinning", "0")
    return ort.InferenceSession(
        MODEL_PATH, opts, providers=["CPUExecutionProvider"]
    )


def get_session() -> ort.InferenceSession:
    global _session, _input_name
    if _session is None:
        _session = _build_session()
        _input_name = _session.get_inputs()[0].name
    return _session


def init() -> None:
    """进程启动时调用：加载模型并做一次预热。

    首次 run 会触发 MatMulNBits 权重预打包，耗时可达数百毫秒。
    不预热的话这笔开销会落在第一个真实用户身上。

    模型缺失/损坏时不阻塞启动，后续请求会返回明确的错误信息。
    """
    try:
        sess = get_session()
        dummy = np.zeros((1, 3, INPUT_SIZE, INPUT_SIZE), dtype=np.float32)
        for _ in range(2):
            sess.run(None, {_input_name: dummy})
    except Exception as e:  # noqa: BLE001
        import logging

        logging.getLogger("nsfw").error("模型初始化失败: %s", e)


def is_ready() -> bool:
    return _session is not None


# --------------------------------------------------------------------------
# 预处理与推理
# --------------------------------------------------------------------------


def preprocess(image: Image.Image) -> np.ndarray:
    """RGB -> 短边缩放 384 -> 中心裁切 384 -> CHW -> [-1,1] -> NCHW(float32)

    注意这里是「短边缩放 + 中心裁切」，不是直接拉伸成 384x384。
    Marqo 这个模型是按 timm 标准 transform 训练的（crop_pct=1.0、bicubic、
    中心裁切），直接拉伸会改变长宽比，实测会让分值严重偏移
    （同一张图 0.11 与 0.92 的差距），必须按训练时的预处理来。
    """
    img = image
    # JPEG 用 libjpeg 的 1/2、1/4、1/8 缩放解码，避免解出整张 4000x3000。
    # 目标给 2 倍尺寸：后面还要按短边缩放到 384，留足余量减少二次放大的损失。
    if getattr(img, "format", None) == "JPEG":
        try:
            img.draft("RGB", (INPUT_SIZE * 2, INPUT_SIZE * 2))
        except Exception:
            pass
    if img.mode != "RGB":
        img = img.convert("RGB")

    # 短边缩放到 INPUT_SIZE，保持长宽比。
    # 长边用 int() 截断而不是 round()：torchvision 的 Resize 就是 int(size*long/short)，
    # 用 round 会差 1 像素，中心裁切位置跟着偏移，实测能让分值偏移 0.04~0.08。
    w, h = img.size
    if w <= 0 or h <= 0:
        raise ValueError("empty image")
    if w < h:
        new_w = INPUT_SIZE
        new_h = max(INPUT_SIZE, int(h * INPUT_SIZE / w))
    else:
        new_h = INPUT_SIZE
        new_w = max(INPUT_SIZE, int(w * INPUT_SIZE / h))
    if (new_w, new_h) != (w, h):
        img = img.resize((new_w, new_h), Image.Resampling.BICUBIC)

    # 中心裁切成 INPUT_SIZE x INPUT_SIZE
    left = (new_w - INPUT_SIZE) // 2
    top = (new_h - INPUT_SIZE) // 2
    if left or top:
        img = img.crop((left, top, left + INPUT_SIZE, top + INPUT_SIZE))

    # 先在 uint8 上转置（数据量只有 float32 的 1/4），再查表转 float32
    arr = np.asarray(img, dtype=np.uint8)
    return _LUT[arr.transpose(2, 0, 1)][None]


def _softmax2(a: float, b: float) -> Tuple[float, float]:
    m = a if a > b else b
    ea = math.exp(a - m)
    eb = math.exp(b - m)
    s = ea + eb
    return ea / s, eb / s


def build_result(sfw: float, nsfw: float, qr_status: str = "unknown") -> dict:
    """组装返回体，按当前 NSFW_THRESHOLD 判定 is_nsfw。

    推理结果和缓存命中都走这里，保证两条路径的阈值语义完全一致；
    同时因为阈值是读取时才算的，改了阈值不用清缓存。
    """
    return {
        "sfw": round(float(sfw), 4),
        "nsfw": round(float(nsfw), 4),
        "is_nsfw": float(nsfw) > NSFW_THRESHOLD,
        "qr_status": qr_status,
    }


def infer_sync(image: Image.Image) -> dict:
    sess = get_session()
    logits = np.asarray(sess.run(None, {_input_name: preprocess(image)})[0])
    if logits.ndim >= 2:
        logits = logits[0]
    logits = logits.reshape(-1)
    # ⚠️ label 顺序：本模型是 ["NSFW", "SFW"]，logits[0]=NSFW、logits[1]=SFW。
    # 原来的 AdamCodd 模型是反的（0=sfw）。搞反会让结果完全颠倒，改模型时必须核对。
    nsfw_raw, sfw_raw = _softmax2(float(logits[0]), float(logits[1]))
    return build_result(sfw_raw, nsfw_raw)


_sem: Optional[asyncio.Semaphore] = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, INFER_CONCURRENCY))
    return _sem


async def infer(image: Image.Image) -> dict:
    """异步推理：在线程池里跑，不占用事件循环。"""
    async with _semaphore():
        return await asyncio.to_thread(infer_sync, image)


# --------------------------------------------------------------------------
# 图像打开 / GIF 取中间帧
# --------------------------------------------------------------------------


def _extract_middle_frame(image: Image.Image) -> Image.Image:
    if getattr(image, "format", None) == "GIF" and getattr(image, "is_animated", False):
        frames = getattr(image, "n_frames", 1)
        # seek 到第 n 帧会把前面的帧全部解码一遍，所以对超长 GIF 做截断
        mid = min(max(0, frames // 2), 20)
        try:
            image.seek(mid)
        except Exception:
            try:
                image.seek(0)
            except Exception:
                pass
    return image


def open_image(data: bytes) -> Image.Image:
    """解码字节流，GIF 动图取中间帧。调用方负责 close。"""
    image = Image.open(io.BytesIO(data))
    return _extract_middle_frame(image)


def detect_format(image: Image.Image) -> str:
    """返回实际解码出的格式名（大写），取不到时返回空串。"""
    fmt = getattr(image, "format", None)
    if fmt:
        return fmt
    info = getattr(image, "info", None) or {}
    return info.get("orig_format") or ""


# --------------------------------------------------------------------------
# 二维码检测（不再依赖 opencv）
# --------------------------------------------------------------------------


def detect_qr_sync(image: Image.Image) -> str:
    try:
        import zxingcpp  # 延迟导入：只有开启二维码检测时才加载
    except Exception:
        return "unknown"
    try:
        arr = np.asarray(image.convert("RGB"))
        results = zxingcpp.read_barcodes(arr)
        if not results:
            return "not_qr"
        for res in results:
            if res.format == zxingcpp.BarcodeFormat.QRCode:
                return "qr"
        return "not_qr"
    except Exception:
        return "error"


async def detect_qr(image: Image.Image) -> str:
    return await asyncio.to_thread(detect_qr_sync, image)


# --------------------------------------------------------------------------
# 共享 HTTP 客户端
# --------------------------------------------------------------------------

_client: Optional[httpx.AsyncClient] = None


def get_http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT, connect=HTTP_CONNECT_TIMEOUT),
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            follow_redirects=True,
            headers={"User-Agent": UA, "Accept": "image/*"},
            trust_env=HTTP_TRUST_ENV,
        )
    return _client


async def close_http() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        finally:
            _client = None


async def fetch_image(url: str) -> Tuple[Optional[bytes], str]:
    """单次流式 GET 拉取图片，边下边校验大小。

    相比原来的 HEAD + GET 双请求：
    - 少一次完整往返（公网 HTTPS 省掉一次 TCP+TLS 握手）；
    - Content-Type / Content-Length 仍然在读完 body 之前就能判断并提前中止；
    - 超限时立即断开，不会先把 10MB 全读进内存。
    """
    client = get_http()
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                return None, f"Download failed, status code: {resp.status_code}"

            ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype and ctype not in ALLOWED_MIME:
                return None, (
                    f"Unsupported file type: {ctype}, "
                    "only jpg/png/bmp/webp/gif allowed."
                )

            cl = resp.headers.get("Content-Length")
            if cl:
                try:
                    if int(cl) > MAX_SIZE:
                        return None, "File too large (>10MB), refusing to download."
                except ValueError:
                    pass

            buf = bytearray()
            async for chunk in resp.aiter_bytes(65536):
                buf += chunk
                if len(buf) > MAX_SIZE:
                    return None, "File too large (>10MB), refusing to process."
            if not buf:
                return None, "Empty response body."
            return bytes(buf), ""

    except httpx.TimeoutException:
        return None, "Request timed out."
    except httpx.RequestError as e:
        return None, f"Network request error: {e}"
    except Exception as e:  # noqa: BLE001
        return None, f"Unknown error: {e}"


# --------------------------------------------------------------------------
# 统一分析入口
# --------------------------------------------------------------------------


async def analyze(data: bytes, qr_enabled: bool) -> dict:
    """字节流 -> 检测结果。URL 检测与上传检测共用这一条路径。"""
    if not data:
        return {"code": -1000, "msg": "Empty image data.", "data": None}
    if len(data) > MAX_SIZE:
        return {"code": -1000, "msg": "File too large (>10MB), refusing to process.",
                "data": None}

    try:
        image = open_image(data)
    except Image.DecompressionBombError:
        return {"code": -1000, "msg": "Image too large to decode.", "data": None}
    except Exception:
        return {"code": -1000, "msg": "Image parsing failed.", "data": None}

    try:
        mime = FORMAT_TO_MIME.get(detect_format(image).upper(), "")
        if mime not in ALLOWED_MIME:
            return {
                "code": -1000,
                "msg": (
                    f"Unsupported file type: {mime or 'unknown'}, "
                    "only jpg/png/bmp/webp/gif allowed."
                ),
                "data": None,
            }

        qr_status = await detect_qr(image) if qr_enabled else "unknown"
        result = await infer(image)
        result["qr_status"] = qr_status
        return {"code": 200, "msg": "success", "data": result}
    finally:
        try:
            image.close()
        except Exception:
            pass
