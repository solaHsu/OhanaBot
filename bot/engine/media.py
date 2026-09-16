"""图片附件取回、压缩与编码：把 OneBot 图片引用转成模型可用的 data URL。

- 优先按 url 下载（NapCat 给的 CDN 链接），失败回退本地 file 路径；
- 校验魔数（JPEG/PNG/GIF/WebP）与大小上限，不合格直接跳过；
- 长边超过 max_dimension 的静态图压缩到以内并转 JPEG（省 token 与带宽）；
- 不落盘、不做缓存：取回的字节仅在本次生成中使用。
"""

from __future__ import annotations

import base64
import io
import logging
import urllib.request
from pathlib import Path

from bot.engine.onebot.parser import ImageRef

try:
    from PIL import Image as _Image
    from PIL import ImageOps as _ImageOps
except ImportError:  # 未安装 Pillow 时按原图发送
    _Image = None
    _ImageOps = None

logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 单张上限（API 允许 32MB，留余量）
DEFAULT_MAX_DIMENSION = 768  # 压缩后长边上限（0 表示不压缩）
JPEG_QUALITY = 80
FETCH_TIMEOUT_SECONDS = 10.0

_MAGIC = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def sniff_mime(payload: bytes) -> str:
    """按魔数判断图片类型；不认识的返回空串。"""
    for magic, mime in _MAGIC:
        if payload.startswith(magic):
            return mime
    if len(payload) >= 12 and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return ""


def collect_image_data_urls(refs, limit: int = 3, max_dimension: int = DEFAULT_MAX_DIMENSION) -> list[str]:
    """按顺序取图并转为 data URL；取不到的条目跳过；大图先压缩。"""
    data_urls: list[str] = []
    for ref in refs:
        if len(data_urls) >= limit:
            break
        payload = _fetch(ref)
        if payload is None:
            continue
        mime = sniff_mime(payload)
        if not mime:
            logger.warning("图片格式无法识别，已跳过（%s）", ref.url or ref.file)
            continue
        payload, mime = compress_image(payload, mime, max_dimension)
        data_urls.append(f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}")
    return data_urls


def compress_image(payload: bytes, mime: str, max_dimension: int = DEFAULT_MAX_DIMENSION) -> tuple[bytes, str]:
    """长边超过上限的静态图缩到以内并转 JPEG；无需压缩或失败时原样返回。"""
    if max_dimension <= 0 or _Image is None:
        return payload, mime
    try:
        with _Image.open(io.BytesIO(payload)) as image:
            if getattr(image, "n_frames", 1) > 1:  # 动图保持原样
                return payload, mime
            image = _ImageOps.exif_transpose(image) or image
            width, height = image.size
            if max(width, height) <= max_dimension:
                return payload, mime
            ratio = max_dimension / max(width, height)
            new_size = (max(1, round(width * ratio)), max(1, round(height * ratio)))
            resized = image.convert("RGBA").resize(new_size, _Image.LANCZOS)
            flattened = _Image.new("RGB", new_size, (255, 255, 255))
            flattened.paste(resized, mask=resized.split()[-1])
            buffer = io.BytesIO()
            flattened.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
        compressed = buffer.getvalue()
        if len(compressed) >= len(payload):
            return payload, mime
        logger.info(
            "图片已压缩：%dx%d → %dx%d（%.0f KB → %.0f KB）",
            width,
            height,
            new_size[0],
            new_size[1],
            len(payload) / 1024,
            len(compressed) / 1024,
        )
        return compressed, "image/jpeg"
    except Exception as exc:
        logger.warning("图片压缩失败，按原图使用：%s", exc)
        return payload, mime


def _fetch(ref: ImageRef) -> bytes | None:
    """取回图片字节：url 优先，本地 file 兜底。"""
    if ref.url.startswith(("http://", "https://")):
        try:
            with urllib.request.urlopen(ref.url, timeout=FETCH_TIMEOUT_SECONDS) as response:
                payload = response.read(MAX_IMAGE_BYTES + 1)
            if len(payload) > MAX_IMAGE_BYTES:
                logger.warning("图片过大（> %d MB），已跳过：%s", MAX_IMAGE_BYTES // (1024 * 1024), ref.url)
                return None
            return payload
        except Exception as exc:
            logger.warning("图片下载失败（%s）：%s", ref.url, exc)
    payload = _read_local(ref.file)
    if payload is None and ref.file:
        logger.warning("图片本地路径不可用，已跳过：%s", ref.file)
    return payload


def _read_local(file_ref: str) -> bytes | None:
    if not file_ref:
        return None
    local = file_ref[7:] if file_ref.startswith("file://") else file_ref
    path = Path(local)
    if not path.is_file():
        return None
    payload = path.read_bytes()
    if len(payload) > MAX_IMAGE_BYTES:
        logger.warning("图片过大（> %d MB），已跳过：%s", MAX_IMAGE_BYTES // (1024 * 1024), file_ref)
        return None
    return payload
