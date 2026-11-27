"""上传文件的类型策略：白名单 + 内容嗅探（唯一来源）

为什么单独一个模块，而不是继续放在路由里：
    文档车道和图片车道要的是**同一套判断**（扩展名合法吗、类型支持吗、内容和后缀对得上吗），
    放在路由里会长出两份 if，而两份 if 迟早不一致 —— 上一轮就踩过：
    上传白名单从解析器注册表取，但图片那条是新写的，很容易变成"上传收下了却没人能解析"。

两条车道分开的理由：
    document = 能被解析成文字、进切块和向量库（白名单唯一来源仍是 parsers.registry）
    image    = 只落盘，不参与文本解析（status=stored），给多模态问答/OCR 预留接口

关于"看后缀还是看内容"：**两个都看，但后缀优先**。
    只看后缀 → 一个 .exe 改名 .md 也能进来（解析得到乱码，浪费一次后台任务）；
    只看内容 → 真实场景里手机相册/微信转存的扩展名经常大小写混写或带 .JPEG，
              甚至 HEIC 直接写成 .jpg，判得太死会把正常用户挡在门外。
    所以：后缀决定"允许不允许 + 用哪个解析器"，内容只用来挡"后缀完全骗人"这一类。
"""

from __future__ import annotations

import re

from app.services.parsers.base import UnsupportedFormatError
from app.services.parsers.registry import IMAGE_MIME_BY_EXT as MIME_BY_EXT_IMAGES
from app.services.parsers.registry import MIME_BY_EXT

# 文档车道：直接引用解析器注册表，**不在这里抄一份清单**
DOCUMENT_MIME_BY_EXT: dict[str, str] = dict(MIME_BY_EXT)

# 图片车道：清单的唯一来源是 parsers.registry.IMAGE_MIME_BY_EXT（那里同时登记了图片解析器）。
# 上一轮的教训就是这里抄了一份：上传收下了却没人能解析，或者解析器支持但上传拒收。
# 现在加一种图片格式只需要改 registry 那一处，白名单、嗅探、解析器自然对齐。
IMAGE_MIME_BY_EXT: dict[str, str] = dict(MIME_BY_EXT_IMAGES)

def is_image_extension(ext: str) -> bool:
    """这个扩展名属于图片车道吗（路径复算/重索引判断都用它，别在调用方抄一份清单）"""
    return (ext or "").lstrip(".").lower() in IMAGE_MIME_BY_EXT


# 扩展名字符白名单（和 process_document.SAFE_EXT 同口径，别在这里放宽）
_EXT_PATTERN = re.compile(r"^[A-Za-z0-9]{1,10}$")

# magic number：只比对前若干字节，绝不为嗅探读完整文件
# 值 = (必须出现在偏移 0 的前缀, ...)；webp 特例见 _MAGIC_CHECKS 的说明
_MAGIC_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "pdf": (b"%PDF",),
    "docx": (b"PK\x03\x04",),      # docx/pptx 都是 zip，Office 的老 .doc 不是 —— 正好被挡住
    "pptx": (b"PK\x03\x04",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
    "bmp": (b"BM",),
    "webp": (b"RIFF",),            # 真正确认还要看第 8..12 字节是 WEBP，见 _webp_ok
}

# 纯文本类不嗅探：任何字节序列都能"看起来像文本"，嗅探在这里毫无意义
NO_SNIFF_EXTENSIONS = ("md", "markdown", "txt", "text")

KIND_DOCUMENT = "document"
KIND_IMAGE = "image"


def normalize_extension(filename: str | None) -> str:
    """从（不可信的）文件名里取出小写扩展名

    客户端的 filename 可能是 Windows 全路径、带 ../ 的名字，甚至空串
    —— 这里只做取后缀和字符白名单，展示用的文件名由调用方另外 sanitize。
    """
    name = (filename or "").replace("\\", "/").split("/")[-1].strip()
    if "." not in name:
        raise UnsupportedFormatError("文件名没有扩展名，无法判断类型")
    ext = name.rsplit(".", 1)[-1].lower()
    if not _EXT_PATTERN.match(ext):
        raise UnsupportedFormatError("文件扩展名不合法")
    return ext


def mime_of(ext: str, kind: str = KIND_DOCUMENT) -> str:
    """白名单派生的 MIME（**不要**用客户端声称的 Content-Type，那个可以随便伪造）"""
    table = IMAGE_MIME_BY_EXT if kind == KIND_IMAGE else DOCUMENT_MIME_BY_EXT
    normalized = (ext or "").lstrip(".").lower()
    if normalized not in table:
        allowed = "/".join(sorted(table))
        label = "图片" if kind == KIND_IMAGE else "文档"
        raise UnsupportedFormatError(f"不支持的{label}类型 .{normalized or '?'}，目前只支持 {allowed}")
    return table[normalized]


def is_supported(ext: str, kind: str = KIND_DOCUMENT) -> bool:
    normalized = (ext or "").lstrip(".").lower()
    table = IMAGE_MIME_BY_EXT if kind == KIND_IMAGE else DOCUMENT_MIME_BY_EXT
    return normalized in table


def _webp_ok(head: bytes) -> bool:
    """WEBP 的格式是 `RIFF` + 4 字节长度 + `WEBP`，光看前 4 字节会把任意 RIFF 文件放进来"""
    return head.startswith(b"RIFF") and len(head) >= 12 and head[8:12] == b"WEBP"


def sniff_mismatch(ext: str, head: bytes) -> str | None:
    """内容和后缀对不对得上。对得上返回 None，对不上返回一句人话（调用方转 400）

    只检查"有 magic number 可查"的类型；返回的描述里**不写文件的真实类型**，
    免得这个接口被当成免费的类型探测服务用。
    """
    normalized = (ext or "").lstrip(".").lower()
    if normalized in NO_SNIFF_EXTENSIONS:
        return None
    if normalized == "webp":
        return None if _webp_ok(head) else "文件内容与 .webp 后缀不符，已拒收"

    expected = _MAGIC_PREFIXES.get(normalized)
    if not expected:
        return None                      # 没登记 magic 的类型不拦（宁可放过，不误伤正常文件）
    if any(head.startswith(prefix) for prefix in expected):
        return None
    return f"文件内容与 .{normalized} 后缀不符，已拒收"


def supported_extensions(kind: str = KIND_DOCUMENT) -> list[str]:
    table = IMAGE_MIME_BY_EXT if kind == KIND_IMAGE else DOCUMENT_MIME_BY_EXT
    return sorted(table)


__all__ = [
    "DOCUMENT_MIME_BY_EXT",
    "IMAGE_MIME_BY_EXT",
    "KIND_DOCUMENT",
    "KIND_IMAGE",
    "is_supported",
    "mime_of",
    "normalize_extension",
    "sniff_mismatch",
    "supported_extensions",
]
