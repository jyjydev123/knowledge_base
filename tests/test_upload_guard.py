"""上传闸门的测试：体积 / 后缀 / 内容与后缀是否一致 / 空文件 + 图片车道 + 失败重跑

这一组用例全是"边界"，没有一条是 happy path —— happy path 在 test_document_api.py 里。
边界之所以值得单独钉住，是因为它们每一个都对应一次真实的事故形态：
  信 Content-Length → 5GB 包先把磁盘吃光再报错；
  只看后缀 → .exe 改名 .md 进队列，白烧一次后台任务；
  图片走 pending → 被重索引捞出来解析失败，用户看到一条莫名其妙的 failed。
"""

import pytest

from app.api.v1 import documentrouter
from app.core.config import settings
from tests.test_document_api import assert_envelope, make_kb, upload, uploaded_files
from tests.test_kb_share_api import account, share

PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
JPEG_MAGIC = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PDF_MAGIC = b"%PDF-1.7\n" + b"stream stuff"


def post_image(client, headers, *, kb_id, content, filename="照片.png", name=None):
    params = {"kb_id": kb_id}
    if name is not None:
        params["image_name"] = name
    return client.post(
        "/api/v1/documents/image/upload",
        headers=headers,
        files={"file": (filename, content, "image/png")},
        params=params,
    )


# --------------------------------------------------------------------------
# 体积上限
# --------------------------------------------------------------------------


def test_体积上限从配置来(client):
    """这条只验"配置真的接上了"：以前这两个数是写死在路由里的常量，改 .env 没用"""
    assert documentrouter.MAX_UPLOAD_BYTES == settings.UPLOAD_MAX_SIZE_MB * 1024 * 1024
    assert documentrouter.MAX_IMAGE_BYTES == settings.IMAGE_MAX_SIZE_MB * 1024 * 1024


def test_超限在读取过程中就断掉(client, logged_in, run, monkeypatch, tmp_path):
    """关键不是"会 413"，而是**不能先把整包读完再判**：限制是在流式读的过程中生效的"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    monkeypatch.setattr(documentrouter, "MAX_UPLOAD_BYTES", 16)

    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, content=b"x" * 4096), 413
    )
    assert "大小限制" in body["msg"]
    assert uploaded_files(tmp_path) == [], "超限不该留下任何文件"


def test_空文件被拒而不是变成永远失败的记录(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    body = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b""), 400)
    assert "空的" in body["msg"]

    monkeypatch.setattr(settings, "UPLOAD_REJECT_EMPTY", False)
    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b""), 200)


# --------------------------------------------------------------------------
# 后缀与内容
# --------------------------------------------------------------------------


@pytest.mark.parametrize("filename", ["tool.exe", "notes.doc", "README", "a.md.exe", "x.md/../y"])
def test_不支持或可疑的文件名一律400(client, logged_in, run, monkeypatch, tmp_path, filename):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, filename=filename), 400)


def test_后缀说pdf内容不是pdf_拒收(client, logged_in, run, monkeypatch, tmp_path):
    """伪装成 .pdf 的东西不该进解析队列（进了也只是烧一次后台任务 + 一条 failed）"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    body = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, filename="报告.pdf", content=b"MZ\x90\x00 executable"), 400
    )
    assert "后缀不符" in body["msg"]
    assert uploaded_files(tmp_path) == []


def test_真pdf魔数放行(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    data = assert_envelope(
        upload(client, logged_in["headers"], kb_id=kb_id, filename="手册.pdf", content=PDF_MAGIC), 200
    )["data"]
    assert data["mime_type"] == "application/pdf"
    # MIME 是白名单派生的，不是客户端说是什么就是什么
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == [f"{data['id']}.pdf"]


def test_纯文本不做魔数嗅探(client, logged_in, run, monkeypatch, tmp_path):
    """md/txt 任何字节都"看起来像文本"，嗅探在这里毫无意义，别把正常文件误伤"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b"\x00\x01\x02 weird"), 200)


# --------------------------------------------------------------------------
# 图片车道
# --------------------------------------------------------------------------


def test_图片上传落库是stored而不是pending(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(documentrouter, "process_document", lambda *a, **k: pytest.fail("图片不该被丢去解析"))
    kb_id = make_kb(run, logged_in["user_id"])

    data = assert_envelope(
        post_image(client, logged_in["headers"], kb_id=kb_id, content=PNG_MAGIC), 200
    )["data"]
    assert data["status"] == "stored"
    assert data["mime_type"] == "image/png"
    assert data["file_size"] == len(PNG_MAGIC)
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == [f"{data['id']}.png"]


def test_图片白名单和文档白名单是两套(client, logged_in, run, monkeypatch, tmp_path):
    """tiff 不在图片白名单里（要 libheif/解码依赖），pdf 不在图片接口里（那是文档车道）"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, filename="扫描.tiff", content=PNG_MAGIC), 400)
    assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, filename="伪装.png", content=PDF_MAGIC), 400)
    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, filename="a.png", content=PNG_MAGIC), 400)


def test_图片重复上传409(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, filename="a.jpg", content=JPEG_MAGIC), 200)
    # 换文件名换不了内容指纹：同一张图重复传照样 409（判重按 file_hash，不按标题）
    body = assert_envelope(
        post_image(client, logged_in["headers"], kb_id=kb_id, filename="b.jpg", content=JPEG_MAGIC), 409
    )
    assert "未重复入库" in body["msg"]


def test_图片超限413且不留文件(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    monkeypatch.setattr(documentrouter, "MAX_IMAGE_BYTES", 32)
    assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, content=PNG_MAGIC * 20), 413)
    assert uploaded_files(tmp_path) == []


def test_图片需要write权限_read授权只能看不能传(client, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    owner = account(client, "zhangsan", "zhangsan@example.com")
    reader = account(client, "lisi", "lisi@example.com")
    kb_id = assert_envelope(
        client.post("/api/v1/kb", headers=owner["headers"], json={"name": "图集"}), 200
    )["data"]["kb_id"]
    assert_envelope(share(client, owner, kb_id, reader["email"], "read"), 200)
    assert_envelope(post_image(client, reader["headers"], kb_id=kb_id, content=PNG_MAGIC), 403)


def test_图片能走既有的下载与删除接口(client, logged_in, run, monkeypatch, tmp_path):
    """图片复用 documents 表的意义就在这：前端不需要为它学第二套接口"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    data = assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, content=PNG_MAGIC), 200)["data"]

    detail = assert_envelope(
        client.get("/api/v1/documents/detail", headers=logged_in["headers"], params={"document_id": data["id"]}), 200
    )["data"]
    assert detail["status"] == "stored"

    resp = client.get(f"/api/v1/documents/download/{data['id']}", headers=logged_in["headers"])
    assert resp.status_code == 200 and resp.content == PNG_MAGIC

    assert_envelope(client.delete(f"/api/v1/documents/delete/{data['id']}", headers=logged_in["headers"]), 200)
    assert uploaded_files(tmp_path, logged_in["user_id"], kb_id) == []


# --------------------------------------------------------------------------
# 失败重跑入口
# --------------------------------------------------------------------------


def reindex(client, headers, document_id):
    return client.post(f"/api/v1/documents/reindex/{document_id}", headers=headers)


def test_failed文档可以重跑(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    from app.db.crud import document_crud
    from app.db.models import DocStatus

    kb_id = make_kb(run, logged_in["user_id"])
    doc_id = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id), 200)["data"]["id"]

    async def _fail(session):
        document = await document_crud.get_document_any(doc_id, session)
        await document_crud.update_status(document, DocStatus.FAILED, session, error_msg="解析炸了")

    run(_fail)

    scheduled: list[str] = []
    monkeypatch.setattr(documentrouter, "process_document", lambda doc, **kw: scheduled.append(str(doc)))

    body = assert_envelope(reindex(client, logged_in["headers"], doc_id), 200)
    assert "轮询" in body["msg"]
    assert body["data"]["status_counts"]["pending"] == 1
    assert scheduled == [doc_id], "重跑必须带 force，否则已索引的会被跳过"

    async def _status(session):
        document = await document_crud.get_document_any(doc_id, session)
        return document.status, document.error_msg

    assert run(_status) == ("pending", None), "重跑前必须把状态拨回 pending 并清掉旧的 error_msg"


def test_重跑越权与不存在的口径(client, logged_in, other_headers, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    doc_id = assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id), 200)["data"]["id"]

    assert_envelope(reindex(client, other_headers, doc_id), 404)
    assert_envelope(reindex(client, logged_in["headers"], "00000000-0000-0000-0000-000000000000"), 404)


def test_重跑对read授权是403(client, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    owner = account(client, "zhangsan", "zhangsan@example.com")
    reader = account(client, "lisi", "lisi@example.com")
    kb_id = assert_envelope(
        client.post("/api/v1/kb", headers=owner["headers"], json={"name": "共享库"}), 200
    )["data"]["kb_id"]
    doc_id = assert_envelope(upload(client, owner["headers"], kb_id=kb_id), 200)["data"]["id"]

    assert_envelope(share(client, owner, kb_id, reader["email"], "read"), 200)
    assert_envelope(reindex(client, reader["headers"], doc_id), 403)


def test_stored资源重跑被明确拒绝而不是静默解析(client, logged_in, run, monkeypatch, tmp_path):
    """图片重跑解析只会变 failed，所以这里如实回一句"不参与解析"，绝不假装提交成功了"""
    monkeypatch.chdir(tmp_path)
    kb_id = make_kb(run, logged_in["user_id"])
    uploaded = assert_envelope(post_image(client, logged_in["headers"], kb_id=kb_id, content=PNG_MAGIC), 200)
    image_id = uploaded["data"]["id"]

    body = assert_envelope(reindex(client, logged_in["headers"], image_id), 200)
    assert "不参与文本解析" in body["msg"]
