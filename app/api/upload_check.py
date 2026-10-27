"""上传图片检测。"""

from fastapi import Request, UploadFile

from app import engine
from app.utils.helper import show_json

MODEL_NOT_READY = "Model not initialized, please check model_nsfw_int8.onnx exists and is loadable."


class UploadCheckHandler:
    async def check(self, request: Request, file: UploadFile, qr_detect: str = "disable"):
        qr_flag = (qr_detect or "disable").lower()
        if qr_flag not in ("enable", "disable"):
            return show_json(
                -1000,
                "Invalid qr_detect value, only 'enable' or 'disable' are supported.",
            )

        # 1) 头部大小预检，能省掉一次完整的 multipart 读取
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > engine.MAX_SIZE:
                    return show_json(
                        -1000, "File too large (>10MB), refusing to process."
                    )
            except ValueError:
                pass

        if not engine.is_ready():
            return show_json(-1000, MODEL_NOT_READY)

        # 2) 读取（最多 MAX_SIZE+1 字节，超出即判定超限）
        try:
            data = await file.read(engine.MAX_SIZE + 1)
        except Exception:
            return show_json(-1000, "Read uploaded file failed.")
        finally:
            try:
                await file.close()
            except Exception:
                pass

        # 3) 解码 + 真实格式校验 + 推理
        return await engine.analyze(data, qr_flag == "enable")
