"""图片解析器（A 路线）：图片 → 视觉模型描述 → 普通文本块

只做转换，不做判断：车道能不能用、要不要解析，由上传路由和 vision 层各自决定
（这里只管"给我一张图，我还你一段文字"，失败就抛 DocumentParseError）。

产出 kind="image" 的单块，交给 chunker 之后和 md/pdf 没有任何区别 ——
这正是 A 路线的意义：检索层、向量层、引用出处都不用为图片写第二套。
"""

from __future__ import annotations

from pathlib import Path

from app.core.config import settings
from app.services import vision
from app.services.parsers.base import ParsedBlock, ParsedDocument, clean_text


def parse(path: Path) -> ParsedDocument:
    text = clean_text(vision.describe_image_file(path))
    if not text:
        # describe_image_file 已经保证非空，这里只是防止以后有人改了 vision 层的空值策略
        raise ValueError("视觉描述为空")
    model = settings.VISION_MODEL
    return ParsedDocument(
        blocks=[ParsedBlock(text=text, page=1, kind="image", extra={"parser": "vision", "model": model})],
        # extra 里记模型名：以后换视觉模型时，能查出哪批描述是旧模型产的、需不需要 reindex
        meta={"parser": "vision", "model": model},
    )
