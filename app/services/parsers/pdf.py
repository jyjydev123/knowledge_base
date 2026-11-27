"""PDF 解析器（pypdf）

按页出块，页码写进 ParsedBlock.page —— 回答里标「第 3 页」全靠它。

提不到字（扫描件/图片型 PDF）时的两条路：
    SCANNED_PDF_VISION=False（默认）→ 直接报错，说清是扫描件；
    SCANNED_PDF_VISION=True        → 页面栅格化后交给视觉模型（A 路线的同一套描述逻辑），
                                     需要可选依赖 pymupdf，且只处理前 SCANNED_PDF_MAX_PAGES 页。
为什么默认关：一页一次付费请求，一份 50 页的扫描件就是 50 次，必须由人明确打开。
"""

from pathlib import Path

from app.core.config import settings
from app.services.parsers.base import DocumentParseError, ParsedBlock, ParsedDocument, clean_text

# 少于这个字符量就认为「没有可提取文本」：一页正文通常远多于 8 个字符，
# 阈值太低会把只有页眉页脚的页误判成空文档，太高会漏掉真正的短文档。
_MIN_TEXT_PER_PAGE = 8
_MIN_TOTAL_TEXT = 16


def parse(path: Path) -> ParsedDocument:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # 依赖没装时给可操作的提示，而不是裸 ImportError
        raise DocumentParseError(f"缺少 pypdf 依赖: {exc}") from exc

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise DocumentParseError(f"PDF 无法打开（可能已损坏）: {exc}") from exc

    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise DocumentParseError("PDF 已加密，请导出无密码版本后再上传")
        except Exception as exc:
            raise DocumentParseError(f"PDF 已加密，无法解析: {exc}") from exc

    blocks: list[ParsedBlock] = []
    for page_number, page in enumerate(reader.pages, start=1):
        try:
            text = clean_text(page.extract_text() or "")
        except Exception as exc:
            # 单页解析失败不该让整篇失败：记下来继续，末尾再判空
            blocks.append(ParsedBlock(text="", page=page_number, kind="text", extra={"error": str(exc)[:200]}))
            continue
        if len(text) >= _MIN_TEXT_PER_PAGE:
            blocks.append(ParsedBlock(text=text, page=page_number))

    total_chars = sum(len(block.text) for block in blocks)
    page_count = len(reader.pages)
    if total_chars < _MIN_TOTAL_TEXT:
        # 文案前缀 "没有从 PDF 中提取到文字" 是测试和 error_msg 对齐的锚点，别改开头
        if not settings.SCANNED_PDF_VISION:
            raise DocumentParseError(
                "没有从 PDF 中提取到文字，可能是扫描件或图片型 PDF（需要 OCR）。"
                "扫描件兜底：.env 打开 SCANNED_PDF_VISION 并安装 pymupdf（uv add pymupdf）"
            )
        blocks, described = _describe_pages_by_vision(Path(path))
        info = reader.metadata or {}
        return ParsedDocument(
            blocks=blocks,
            meta={
                "format": "pdf",
                "page_count": page_count,
                # 诚实记录"只看了前几页"，否则用户以为整本扫描件都进索引了
                "scanned_via": "vision",
                "pages_described": described,
                "title": str(info.title) if getattr(info, "title", None) else None,
                "author": str(info.author) if getattr(info, "author", None) else None,
            },
        )

    info = reader.metadata or {}
    return ParsedDocument(
        blocks=blocks,
        meta={
            "format": "pdf",
            "page_count": page_count,
            "title": str(info.title) if getattr(info, "title", None) else None,
            "author": str(info.author) if getattr(info, "author", None) else None,
        },
    )


def _describe_pages_by_vision(path: Path) -> tuple[list[ParsedBlock], int]:
    """栅格化前 N 页 → 视觉模型描述。单页失败不拖垮整篇，全失败才报错"""
    from app.services import vision
    from app.services.parsers.base import clean_text

    if not settings.vision_ready:
        reason = settings.vision_lane_block_reason
        raise DocumentParseError(f"没有从 PDF 中提取到文字，可能是扫描件；视觉车道未就绪：{reason}")
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:
        raise DocumentParseError(
            "没有从 PDF 中提取到文字，可能是扫描件；页面栅格化需要可选依赖 pymupdf（uv add pymupdf）"
        ) from exc

    limit = max(int(settings.SCANNED_PDF_MAX_PAGES), 1)
    blocks: list[ParsedBlock] = []
    errors: list[str] = []
    try:
        doc = fitz.open(str(path))
    except Exception as exc:  # noqa: BLE001
        raise DocumentParseError(f"PDF 无法栅格化（{type(exc).__name__}: {str(exc)[:120]}）") from exc
    try:
        # 页数取栅格化器看到的 page_count，不取 pypdf 那个：pypdf 是靠文字对象数的页，
        # 扫描件上它偶尔会少报一圈，两处不一致时以"真能渲染出几页"为准
        take = min(limit, int(getattr(doc, "page_count", 0) or limit))
        # 2x ≈ 144dpi：分辨率太低视觉模型读不清小字，太高就是白烧 token
        matrix = fitz.Matrix(2, 2)
        for index in range(take):
            try:
                pix = doc.load_page(index).get_pixmap(matrix=matrix)
                hint = f"扫描件第 {index + 1} 页"
                raw = vision.describe_image_bytes(pix.tobytes("png"), "png", hint=hint)
                text = clean_text(raw)
            except Exception as exc:  # noqa: BLE001 —— 一页读不出来继续下一页
                errors.append(f"第{index + 1}页 {type(exc).__name__}")
                continue
            if text:
                blocks.append(ParsedBlock(
                    text=text, page=index + 1, kind="image",
                    extra={"parser": "vision", "model": settings.VISION_MODEL},
                ))
    finally:
        doc.close()

    if not blocks:
        detail = f"（{'、'.join(errors[:3])}）" if errors else "（模型没有返回内容）"
        raise DocumentParseError(f"没有从 PDF 中提取到文字，视觉兜底也没读出内容{detail}")
    return blocks, take
