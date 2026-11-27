"""视觉车道测试：图片 → 描述 → 复用现有切块/入库链路

**全程不出网**：真正调模型的只有 vision._invoke 这一处，用例把它换掉。
（和 README 里"对话接口测试不出网"是同一条规矩：需要联网的测试等于没有测试。）
"""

import base64
import io
import sys
import types
import uuid
from pathlib import Path

import pytest
from PIL import Image

import app.api.v1.documentrouter as documentrouter
from app.core.config import settings
from app.db.models import Chunk, DocStatus, Document
from app.services import file_types, vision
from app.services.parsers import registry
from app.services.parsers.base import DocumentParseError
from app.services.process_document import document_storage_path, process_document
from tests.test_parsers import minimal_pdf
from tests.test_schema_models import _add_doc, _add_kb

DESCRIPTION = "报销单\n金额 1200 元\n日期 2026-11-01\n出差交通票粘贴页"


def png_bytes(size=(64, 64), mode="RGB") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def decode_data_url(url: str) -> tuple[str, bytes]:
    header, payload = url.split(",", 1)
    mime = header.split(":", 1)[1].split(";")[0]
    return mime, base64.b64decode(payload)


@pytest.fixture(autouse=True)
def _绝不出网(monkeypatch):
    """本文件的硬兜底：漏打桩也必须炸在测试里，而不是悄悄发一次真请求

    这条不是洁癖：写这份用例时我就漏打过一次桩，测试真连了阿里云、拿回 401 ——
    现象是"偶发红一条"，配着真 key 的机器上还会顺手烧掉配额。
    """
    def boom(*_args, **_kwargs):
        raise AssertionError("测试里不许真调视觉模型：请 monkeypatch vision._invoke")

    monkeypatch.setattr(vision, "_invoke", boom)


@pytest.fixture()
def lane(monkeypatch):
    """打开视觉车道。conftest 把两个 key 都硬置空了，所以这里必须显式造"""
    monkeypatch.setattr(settings, "IMAGE_PARSE_ENABLED", True)
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-fake-dashscope-key")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    vision.reset_vision_cache()
    yield settings
    vision.reset_vision_cache()


@pytest.fixture()
def spy(monkeypatch):
    """拦下唯一出网口，记录送出去的东西"""
    calls = []

    def fake(data_url, *, hint: str = ""):
        calls.append({"url": data_url, "hint": hint})
        return DESCRIPTION

    monkeypatch.setattr(vision, "_invoke", fake)
    return calls


# ---------------- 车道就绪判定（降级必须可见） ----------------
def test_没配key时车道不就绪且原因说得清(monkeypatch):
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    monkeypatch.setattr(settings, "IMAGE_PARSE_ENABLED", True)
    assert vision.vision_available() is False
    assert "未配置" in settings.vision_lane_block_reason


def test_占位key不算配好了(monkeypatch):
    """sk-xxxx 这种模板值在开发机上到处都是，当"已配置"就会每次上传都白跑一次网络"""
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-xxxxxxxxxxxxxxxx")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    assert vision.vision_available() is False


def test_没单独配就复用百炼那把embedding_key(lane):
    assert settings.vision_api_key == "sk-fake-dashscope-key"
    assert vision.vision_available() is True


def test_开关关掉就退回只存不解析(lane, monkeypatch):
    monkeypatch.setattr(settings, "IMAGE_PARSE_ENABLED", False)
    assert vision.vision_available() is False
    assert "stored" in settings.vision_lane_block_reason


def test_未就绪时直接问模型会明确报错而不是静默返回空(monkeypatch):
    """空描述 = 一条 indexed 却永远搜不到的僵尸记录，比 failed 难查得多"""
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    with pytest.raises(DocumentParseError, match="视觉车道未就绪"):
        vision.get_vision_model()


# ---------------- 送模型前的准备（体积/格式） ----------------
def test_大图先等比缩放再送(lane, monkeypatch):
    """手机原图直接 base64 上去就是十几 MB 请求体：慢、贵、还容易 413

    png 源保 PNG 不转 JPEG —— 票据/截图里那些小字经不起二次有损压缩，
    而缩放之后本来就到了体积上限以内，没必要为省一点字节牺牲识别率。
    """
    captured = []
    monkeypatch.setattr(vision, "_invoke", lambda url, **kw: captured.append(url) or "文字")
    raw = png_bytes((3000, 2000))
    vision.describe_image_bytes(raw, "png")

    mime, data = decode_data_url(captured[0])
    assert mime == "image/png"
    with Image.open(io.BytesIO(data)) as img:
        assert max(img.size) <= settings.VISION_MAX_PIXELS


def test_jpeg源重编码成jpeg(lane, monkeypatch):
    """本来就有损的 jpg 没必要充成体积大三倍的 PNG"""
    captured = []
    monkeypatch.setattr(vision, "_invoke", lambda url, **kw: captured.append(url) or "文字")
    buf = io.BytesIO()
    Image.new("RGB", (2400, 1800), (10, 90, 200)).save(buf, format="JPEG", quality=95)
    vision.describe_image_bytes(buf.getvalue(), "jpg")
    mime, data = decode_data_url(captured[0])
    assert mime == "image/jpeg" and len(data) < len(buf.getvalue())


def test_透明通道不会被压成黑底jpeg(lane, monkeypatch):
    captured = []
    monkeypatch.setattr(vision, "_invoke", lambda url, **kw: captured.append(url) or "文字")
    vision.describe_image_bytes(png_bytes(mode="RGBA"), "png")
    assert decode_data_url(captured[0])[0] == "image/png"


def test_后缀骗人的垃圾字节明确报错(lane):
    with pytest.raises(DocumentParseError, match="无法解码"):
        vision.describe_image_bytes(b"this is definitely not a png", "png")


def test_没有Pillow且超限时给出可操作提示(lane, monkeypatch):
    """没装可选依赖时不硬编，但也不能把超限的原始字节悄悄发出去"""
    monkeypatch.setitem(sys.modules, "PIL", None)
    # 下限是代码里刻意留的（配成 1KB 会让所有图都送不出去），所以这里要超过那个下限才轮得到报错
    monkeypatch.setattr(settings, "VISION_MAX_IMAGE_BYTES", 1024)
    with pytest.raises(DocumentParseError, match="Pillow"):
        vision.describe_image_bytes(b"x" * (70 * 1024), "png")


def test_不支持的图片后缀直接拒(lane):
    with pytest.raises(DocumentParseError, match="不是受支持的图片格式"):
        vision.describe_image_file("/tmp/nothing.tiff")


# ---------------- 解析器与两条车道 ----------------
def test_图片解析产出可切块的文本块(lane, spy, tmp_path):
    path = tmp_path / "截图.png"
    path.write_bytes(png_bytes())
    doc = registry.parse_file(path)

    assert spy and "base64" in spy[0]["url"]
    assert doc.block_count == 1
    block = doc.blocks[0]
    assert block.kind == "image" and block.page == 1
    assert block.extra["model"] == settings.VISION_MODEL      # 换模型后能查出哪批要重跑
    assert "报销单" in doc.plain_text

    from app.services.chunker import chunk_parsed_document

    pieces = chunk_parsed_document(doc)
    assert pieces and "1200" in pieces[0].content              # 描述真的进了切块，检索层不用改


def test_图片白名单只有一个来源():
    """上一轮踩过「上传收下了却没人能解析」：清单必须由 registry 出，file_types 只做闸门"""
    assert sorted(file_types.IMAGE_MIME_BY_EXT) == registry.image_extensions()
    assert file_types.is_image_extension(".PNG") is True
    assert file_types.is_image_extension("pdf") is False


def test_文档车道没被图片放宽(lane):
    """/documents/upload 收到 png 仍然拒：两条车道的 status 语义不同，不能混进一张白名单"""
    assert "png" not in registry.supported_extensions()
    assert "png" in registry.IMAGE_PARSERS                     # 但解析层认得它（图片车道用）


# ---------------- 端到端：图片 → process_document → chunks ----------------
def seed_image_document(run, user_id, *, filename="截图.png", ext="png"):
    """按真路径规则落盘一张图 + 建 documents 记录，返回 (doc_id, kb_id, file_path)"""

    async def _seed(session):
        kb = await _add_kb(session, user_id, name="票据库")
        doc = await _add_doc(session, kb.id, user_id, filename=filename, file_hash=f"h-{uuid.uuid4()}")
        doc.status = DocStatus.PENDING
        # 用真路径规则算，顺带证明图片车道和文档车道共用同一条约定（能复算才删得掉）
        path = document_storage_path(str(user_id), str(user_id), str(kb.id), str(doc.id), ext)
        doc.file_path = path
        await session.commit()
        return str(doc.id), str(kb.id), path

    return run(_seed)


def test_图片描述走的就是文本那条入库链路(client, logged_in, run, lane, spy, monkeypatch):
    """这条是 A 路线的意义本身：图片不用第二套逻辑，切块/token/status 全都一样"""
    doc_id, _kb_id, path = seed_image_document(run, logged_in["user_id"])
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(png_bytes())

    result = run(lambda db: process_document(doc_id, session=db))
    assert result["ok"] is True and result["status"] == "indexed"
    assert result["chunks"] >= 1

    rows = run(lambda db: db.execute(Chunk.__table__.select().where(Chunk.document_id == uuid.UUID(doc_id))))
    text = " ".join(row.content for row in rows)
    assert "报销单" in text and "1200" in text

    doc = run(lambda db: db.get(Document, uuid.UUID(doc_id)))
    assert doc.status == DocStatus.INDEXED


# ---------------- 上传接口：有没有车道，status 不一样 ----------------
def make_kb(run, user_id, name="相册库"):
    async def _seed(session):
        kb = await _add_kb(session, user_id, name=name)
        return str(kb.id)

    return run(_seed)


def upload_image(client, logged_in, kb_id, payload=None):
    return client.post(
        f"/api/v1/documents/image/upload?kb_id={kb_id}",
        headers=logged_in["headers"],
        files={"file": ("相册.png", payload or png_bytes(), "image/png")},
    )


def test_有车道时图片进解析流(client, logged_in, run, lane, spy, monkeypatch):
    # 后台任务里的 process_document 会自己开真库连接，测试里换成桩：
    # 这里只验接口侧的判定（拨到 pending + 派了活），真解析链路交给上面那条端到端用例
    calls = []
    monkeypatch.setattr(documentrouter, "process_document", lambda doc_id, **kw: calls.append(doc_id))
    kb_id = make_kb(run, logged_in["user_id"])

    body = upload_image(client, logged_in, kb_id).json()
    assert body["data"]["status"] == "pending"
    assert body["msg"].startswith("上传成功，正在生成图片描述")
    assert len(calls) == 1


def test_没车道时维持只存档(client, logged_in, run, monkeypatch):
    """回归既有约定：没 key 也不能让图片变成一条 failed（conftest 已把 key 置空）"""
    monkeypatch.setattr(settings, "IMAGE_PARSE_ENABLED", True)
    kb_id = make_kb(run, logged_in["user_id"], name="存档库")
    body = upload_image(client, logged_in, kb_id).json()
    assert body["data"]["status"] == "stored"
    assert "未启用视觉解析" in body["msg"]


def test_文档接口仍然不收图片(client, logged_in, run, lane):
    kb_id = make_kb(run, logged_in["user_id"], name="拒收库")
    resp = client.post(
        f"/api/v1/documents/upload?kb_id={kb_id}",
        headers=logged_in["headers"],
        files={"file": ("伪装.png", png_bytes(), "image/png")},
    )
    assert resp.status_code == 400, resp.text


# ---------------- reindex：后来才配上 key 的图能不能补描述 ----------------
def reindex(client, logged_in, doc_id):
    return client.post(f"/api/v1/documents/reindex/{doc_id}", headers=logged_in["headers"])


def test_stored图片在车道就绪后能被重索引接管(client, logged_in, run, lane, monkeypatch):
    calls = []
    monkeypatch.setattr(documentrouter, "process_document", lambda doc_id, **kw: calls.append(doc_id))
    doc_id, _kb, path = seed_image_document(run, logged_in["user_id"])

    async def _to_stored(session):
        from app.db.models import Document

        doc = await session.get(Document, uuid.UUID(doc_id))
        doc.status = DocStatus.STORED
        await session.commit()

    run(_to_stored)
    body = reindex(client, logged_in, doc_id).json()
    assert body["status"] == 200, body
    assert "已提交重新解析" in body["msg"] and calls == [doc_id]


def test_stored图片没车道时明确拒绝(client, logged_in, run, monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "VISION_API_KEY", "")
    doc_id, _kb, _path = seed_image_document(run, logged_in["user_id"])

    async def _to_stored(session):
        from app.db.models import Document

        doc = await session.get(Document, uuid.UUID(doc_id))
        doc.status = DocStatus.STORED
        await session.commit()

    run(_to_stored)
    body = reindex(client, logged_in, doc_id).json()
    assert "不参与文本解析" in body["msg"]


# ---------------- 扫描件 PDF 兜底（默认关，开了还要 pymupdf） ----------------
def test_扫描件默认关闭时报错文案直接告诉你怎么开(monkeypatch, tmp_path):
    """报错要能自己解释自己：用户看到"可能是扫描件"之后该干什么，不必去翻文档"""
    from app.services.parsers import pdf

    monkeypatch.setattr(settings, "SCANNED_PDF_VISION", False)
    blank = tmp_path / "scan.pdf"
    blank.write_bytes(minimal_pdf(""))
    with pytest.raises(DocumentParseError) as info:
        pdf.parse(blank)
    message = str(info.value)
    assert "没有从 PDF 中提取到文字" in message        # 这条前缀是既有约定（测试与 error_msg 都对齐它）
    assert "SCANNED_PDF_VISION" in message and "pymupdf" in message


def test_开了兜底但没装pymupdf时报装依赖(monkeypatch, tmp_path):
    from app.services.parsers import pdf

    monkeypatch.setattr(settings, "SCANNED_PDF_VISION", True)
    monkeypatch.setitem(sys.modules, "fitz", None)          # import fitz → ImportError
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-fake-dashscope-key")
    blank = tmp_path / "scan.pdf"
    blank.write_bytes(minimal_pdf(""))
    with pytest.raises(DocumentParseError, match="pymupdf"):
        pdf.parse(blank)


def test_有pymupdf时扫描件按页描述(lane, spy, monkeypatch, tmp_path):
    from app.services.parsers import pdf

    class FakePix:
        def tobytes(self, _fmt):
            return png_bytes()

    class FakePage:
        def get_pixmap(self, matrix=None):
            return FakePix()

    class FakeDoc:
        page_count = 3

        def load_page(self, index):
            return FakePage()

        def close(self):
            pass

    fake = types.SimpleNamespace(Matrix=lambda *a: None, open=lambda _p: FakeDoc())
    monkeypatch.setitem(sys.modules, "fitz", fake)
    monkeypatch.setattr(settings, "SCANNED_PDF_VISION", True)
    monkeypatch.setattr(settings, "SCANNED_PDF_MAX_PAGES", 2)

    blank = tmp_path / "scan.pdf"
    blank.write_bytes(minimal_pdf(""))
    doc = pdf.parse(blank)
    assert [b.page for b in doc.blocks] == [1, 2]           # 只处理前 N 页
    assert doc.meta["scanned_via"] == "vision" and doc.meta["pages_described"] == 2
    assert all(b.kind == "image" for b in doc.blocks)
    assert spy[0]["hint"] == "扫描件第 1 页"


# ---------------- 健康检查里看得见 ----------------
def test_health带视觉一节(client):
    data = client.get("/health").json()["data"]
    assert "vision" in data
    assert set(data["vision"]) == {"configured", "ok", "reason", "scanned_pdf"}
