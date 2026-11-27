"""解析器注册表：扩展名 → 解析函数

新增一种格式只做两件事：写一个 parse(path) -> ParsedDocument 的模块，然后在这里登记一行。
上传接口的白名单（documentrouter.ALLOWED_EXTENSIONS）也从这里取，
避免出现「解析器支持但上传拒收」或「上传收下了却解析不了」两张皮。
"""

from pathlib import Path
from typing import Callable

from app.services.parsers import docx, image, markdown, pdf, pptx
from app.services.parsers.base import (
    DocumentParseError,
    ParsedDocument,
    Parser,
    UnsupportedFormatError,
)

MIME_BY_EXT: dict[str, str] = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "md": "text/markdown",
    "txt": "text/plain",
}

# 图片车道单独一张表，**不并进 PARSERS**：
# supported_extensions() 是文档上传的白名单唯一来源，png 混进去就等于让
# POST /documents/upload 也能收图片，两条车道（stored / pending）的区别立刻被抹平。
IMAGE_MIME_BY_EXT: dict[str, str] = {
    # 不含 tiff/heic：前者浏览器基本不显示，后者要 libheif 才能解码，
    # 手机相册能直接导出的格式已经够用（口径与 file_types 的嗅探表一致）
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}

PARSERS: dict[str, Parser] = {
    "pdf": pdf.parse,
    "docx": docx.parse,
    "pptx": pptx.parse,
    "md": markdown.parse,
    "txt": markdown.parse,   # 纯文本没有标题语法时，按段落切即可
}


IMAGE_PARSERS: dict[str, Parser] = {ext: image.parse for ext in IMAGE_MIME_BY_EXT}


def supported_extensions() -> list[str]:
    """文档车道支持的扩展名（上传白名单从这里取，图片不在里面）"""
    return sorted(PARSERS)


def image_extensions() -> list[str]:
    """图片车道支持的扩展名（file_types 的白名单从这里取，别再抄一份）"""
    return sorted(IMAGE_MIME_BY_EXT)


def is_image_extension(ext: str) -> bool:
    return (ext or "").lstrip(".").lower() in IMAGE_MIME_BY_EXT


def get_parser(ext: str) -> Parser:
    """按扩展名取解析函数；不支持就 UnsupportedFormatError（调用方转 400）"""
    normalized = (ext or "").lstrip(".").lower()
    parser = PARSERS.get(normalized) or IMAGE_PARSERS.get(normalized)
    if parser is None:
        raise UnsupportedFormatError(
            f"不支持的文件类型 .{normalized or '?'}，目前只支持 "
            f"{'/'.join(supported_extensions())}（图片：{'/'.join(image_extensions())}）"
        )
    return parser


def parse_file(path: str | Path, ext: str | None = None) -> ParsedDocument:
    """解析一个文件，返回结构化文本

    这里是解析层的唯一对外入口，负责三件事：
      1. 文件必须真的存在且可读（M2 的后台任务里，这条错误会写进 documents.error_msg）；
      2. 扩展名决定用哪个解析器（**不看客户端声称的 MIME**，也不嗅探内容，
         因为混着判会给伪装扩展名的文件留下踩坑空间——上传接口已有白名单与体积限制）；
         注意这里也不做「后缀骗人」的纠正：真要把 .exe 伪装成 .md 传上来，
         解析只会得到乱码或空结果，同样报错，不会写进索引；
      3. 把三方库五花八门的异常统一成 DocumentParseError，上层只处理一种错误。
    """
    file_path = Path(path)
    # 顺序有意为之：先判扩展名再碰磁盘，不支持的类型连文件都不用打开
    parser: Callable[[Path], ParsedDocument] = get_parser(ext or file_path.suffix)

    if not file_path.is_file():
        raise DocumentParseError(f"文件不存在或不是普通文件: {file_path.name}")
    try:
        document = parser(file_path)
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 三方库异常种类太多，统一收口
        raise DocumentParseError(f"解析失败: {type(exc).__name__}: {exc}") from exc

    if document.is_empty():
        raise DocumentParseError("解析结果为空，没有可索引的文字")
    return document
