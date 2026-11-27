"""图片 → 文字（A 路线：让图片进现有的文本检索链路，而不是新造一条多模态链路）

为什么是"描述入库"而不是"原图问答"：
    原图喂给模型要同时满足三件事 —— LLM 支持图片输入（deepseek-chat 不支持）、
    对话消息带上 base64/URL、手机侧还得扛住体积。而"图片 → 一段文字"这一步做完，
    后面的切块、chunks 表、pgvector、/search、引用出处全都复用现成的，
    等于用一次模型调用换整条已验证的链路。

三条硬规矩（改这里之前先读完）：
    1. **这一层只产文字**，不碰数据库、不碰 HTTP 状态码（service 层不碰 HTTP，见 DEVELOPMENT.md）；
    2. 车道不可用时抛 DocumentParseError 说清原因，**绝不静默返回空串**：
       空描述会写出一条 indexed 却永远搜不到的僵尸记录，比一条 failed 难查十倍。
       "要不要进解析流"是上传路由的判断（它看 vision_available()），不是这里的降级；
    3. 同步实现。解析层整体同步（registry.parse_file 跑在 asyncio.to_thread 里），
       这里掺 asyncio 只会让调用方多一层线程问题。
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache
from pathlib import Path

from app.core.config import settings
from app.services.parsers.base import DocumentParseError

logger = logging.getLogger("app.services.vision")

# 转录优先的提示词：这个项目的图九成是「票据/截图/图表」，
# 用户要搜的是图里写了什么，而不是"画面里有一个男人"。
DEFAULT_VISION_PROMPT = (
    "你在为知识库做图片文字化，输出会被切块并用于检索。请按顺序输出纯文本，"
    "不要 markdown 标题、不要客套话、不要声明自己是 AI：\n"
    "1) 逐字转录图中所有可见文字（含表格、代码截图、票据的金额与日期、聊天气泡），"
    "按阅读顺序，保留换行与行列关系；\n"
    "2) 一句话说明这是什么（例如：报销单 / 系统架构图 / 微信聊天截图 / 折线图）；\n"
    "3) 补充关键视觉信息（布局、图表趋势、箭头与红框标注、人物动作等）。\n"
    "看不清的字符用 [?] 代替，绝不编造内容。"
)

# 送模型时认定的图片格式（只用于挑 data URL 的 mime，上传白名单在 parsers.registry 里，只有一份）
_STATIC_MIME = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}
_PASSTHROUGH_MIME = {"png", "webp", "bmp"}   # 可能带透明通道，重编码成 JPEG 会把背景糊成黑色


def vision_available() -> bool:
    """视觉车道现在能不能真跑（没 key / 开关关了 都不行）"""
    return settings.vision_ready


def lane_block_reason() -> str:
    return settings.vision_lane_block_reason


@lru_cache(maxsize=4)
def _chat_model(model: str, base_url: str, key_digest: int, timeout: int):
    """按配置缓存客户端（连接池在客户端内部）。

    缓存 key 里放的是 key 的 hash 而不是 key 本身 —— 不能让明文密钥常驻 dict 键里。
    """
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=model,
        api_key=settings.vision_api_key,
        base_url=base_url,
        temperature=0.0,            # 转录要稳定，不要创作
        timeout=timeout,
        max_retries=1,              # 图片解析在后台任务里，重试一次足够，别把配额烧光
        max_tokens=settings.VISION_MAX_TOKENS,
    )


def get_vision_model():
    if not settings.vision_ready:
        raise DocumentParseError(f"视觉车道未就绪：{settings.vision_lane_block_reason}")
    return _chat_model(
        settings.VISION_MODEL,
        settings.VISION_BASE_URL,
        hash(settings.vision_api_key),
        settings.VISION_TIMEOUT_SECONDS,
    )


def reset_vision_cache() -> None:
    """改了 key/端点之后清掉缓存的客户端（测试和 /health 自检都用得上）"""
    _chat_model.cache_clear()


def _target_format(img, suffix: str) -> str:
    """重编码成什么格式：可能带透明通道的一律走 PNG，其余走 JPEG（体积小得多）"""
    if suffix in _PASSTHROUGH_MIME or (img.mode and img.mode not in ("RGB", "L")):
        return "PNG"
    return "JPEG"


def _prepare(raw: bytes, suffix: str) -> tuple[bytes, str]:
    """缩放 + 编码，返回 (要送模型的数据, mime)

    Pillow 是**可选**依赖（本项目现在靠 pypdf 等传递依赖带着它）：
      - 装了 → 等比缩到 VISION_MAX_PIXELS 以内再送，透明通道保 PNG，其余转 JPEG；
      - 没装 → 原样送，只卡字节上限（大图会明确报错，不会悄悄把 11MB 请求体发出去）。
    """
    max_bytes = max(int(settings.VISION_MAX_IMAGE_BYTES), 64 * 1024)
    try:
        import io

        from PIL import Image, ImageOps
    except ImportError:
        if len(raw) > max_bytes:
            raise DocumentParseError(
                f"图片过大（{len(raw) // 1024}KB > {max_bytes // 1024 // 1024}MB），"
                "且本机没装 Pillow 无法压缩后再送视觉模型：pip install pillow 或先手动缩小"
            ) from None
        return raw, _STATIC_MIME.get(suffix, "image/jpeg")

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception as exc:  # noqa: BLE001 —— PIL 的解码异常种类很多，统一成一条人话
        raise DocumentParseError(f"图片无法解码（{type(exc).__name__}: {str(exc)[:120]}），"
                                 f"文件可能损坏或不是真正的 {suffix.upper()}") from exc

    # 手机照片的 EXIF 方向标记不处理的话，视觉模型看到的是一张躺倒的图
    img = ImageOps.exif_transpose(img)
    limit = max(int(settings.VISION_MAX_PIXELS), 256)
    if max(img.size) > limit:
        img.thumbnail((limit, limit))
    fmt = _target_format(img, suffix)
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")

    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=82)
    data = buf.getvalue()
    if len(data) > max_bytes:
        raise DocumentParseError(
            f"图片压缩后仍有 {len(data) // 1024}KB，超过视觉车道的 "
            f"{max_bytes // 1024}KB 上限（VISION_MAX_IMAGE_BYTES）：请把长图截成几段再传"
        )
    return data, "image/png" if fmt == "PNG" else "image/jpeg"


def _invoke(data_url: str, *, hint: str = "") -> str:
    """唯一出网口：data URL → 描述文本。失败一律 DocumentParseError，不返回空串"""
    from langchain_core.messages import HumanMessage

    prompt = settings.VISION_PROMPT or DEFAULT_VISION_PROMPT
    if hint:
        prompt = f"{prompt}\n（补充线索：{hint}）"
    client = get_vision_model()
    try:
        reply = client.invoke([HumanMessage(content=[
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_url}},
        ])])
    except Exception as exc:  # noqa: BLE001 —— 网络/额度/模型名错，对用户都是"这张图没读出来"
        raise DocumentParseError(
            f"视觉模型调用失败（{settings.VISION_MODEL}）：{type(exc).__name__}: {str(exc)[:180]}"
        ) from exc
    text = reply.content if isinstance(reply.content, str) else str(reply.content)
    text = str(text)
    if len(text) > 6000:            # 兜底截断：模型偶尔会开始写论文，超长文本会把切块撑爆
        text = text[:6000]
    if not text or not text.strip():
        raise DocumentParseError("视觉模型返回了空内容（图太糊或全空白？），未写入索引")
    return text.strip()


def describe_image_bytes(raw: bytes, suffix: str = "png", *, hint: str = "") -> str:
    """给一段图片字节，返回可检索的文字（PDF 页面栅格化后也走这里）"""
    data, mime = _prepare(raw, (suffix or "png").lstrip(".").lower())
    return _invoke(f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}", hint=hint)


def describe_image_file(path: str | Path, *, hint: str = "") -> str:
    """给一个磁盘上的图片文件，返回可检索的文字"""
    file_path = Path(path)
    suffix = file_path.suffix.lstrip(".").lower()
    if suffix not in _STATIC_MIME:
        raise DocumentParseError(f"不是受支持的图片格式: .{suffix or '?'}")
    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise DocumentParseError(f"图片读取失败: {exc}") from exc
    if not raw:
        raise DocumentParseError("图片文件是空的")
    return describe_image_bytes(raw, suffix, hint=hint)
