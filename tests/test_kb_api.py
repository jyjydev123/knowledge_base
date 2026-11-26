"""M0 知识库接口：建 / 列表 / 详情 / 改 / 删（含权限矩阵与删库收磁盘）

这一组用例存在的意义不是"接口能返回 200"，而是那三条容易被随手改坏的规矩：
  1. owner 永远来自登录态，**请求体里没有 user_id 这个口子**；
  2. 越权的响应是 404（读不到就当不存在），能读不能写才是 403；
  3. 删库必须把磁盘文件一起收走，而且顺序是"先查路径 → 删记录 → 删文件"。
"""

from app.api.v1.kbrouter import router as kb_router
from tests.test_document_api import assert_envelope, upload, uploaded_files
from tests.test_kb_share_api import account, share

# --------------------------------------------------------------------------
# 建库
# --------------------------------------------------------------------------


def create(client, headers, name="产品资料", **kwargs):
    return client.post("/api/v1/kb", headers=headers, json={"name": name, **kwargs})


def test_建库_只需要一个名字(client, logged_in):
    body = assert_envelope(create(client, logged_in["headers"], description="装公司制度"), 200)
    data = body["data"]
    assert data["name"] == "产品资料" and data["description"] == "装公司制度"
    assert data["kb_id"] and data["owner_id"] == logged_in["user_id"]
    # 新库的初始状态：version=1、零文档、owner 隐式 write
    assert (data["version"], data["document_count"], data["chunk_count"]) == (1, 0, 0)
    assert data["permission"] == "write" and data["is_owner"] is True
    assert data["visibility"] == "private"


def test_建库_名字不能为空或纯空格(client, logged_in):
    assert_envelope(create(client, logged_in["headers"], name=""), 422)
    assert_envelope(create(client, logged_in["headers"], name="   "), 422)


def test_建库_重叠必须小于块长(client, logged_in):
    assert_envelope(create(client, logged_in["headers"], chunk_size=100, chunk_overlap=100), 422)
    assert_envelope(create(client, logged_in["headers"], chunk_size=100, chunk_overlap=99), 200)


def test_建库_同一用户重名_409(client, logged_in):
    assert_envelope(create(client, logged_in["headers"], name="团队库"), 200)
    assert_envelope(create(client, logged_in["headers"], name="团队库"), 409)


def test_建库_不同用户可以重名(client, logged_in, other_headers):
    """唯一索引是 uq(owner_id, name)，不是全局唯一 —— 很多人都有「报销制度」这个库很正常"""
    assert_envelope(create(client, logged_in["headers"], name="报销制度"), 200)
    assert_envelope(create(client, other_headers, name="报销制度"), 200)


def test_建库必须登录(client):
    assert_envelope(create(client, {}, name="匿名库"), 401)


# --------------------------------------------------------------------------
# 列表
# --------------------------------------------------------------------------


def list_kbs(client, headers, **params):
    return client.get("/api/v1/kb/list", headers=headers, params=params)


def test_列表_空结果也是200(client, logged_in):
    data = assert_envelope(list_kbs(client, logged_in["headers"]), 200)["data"]
    assert data == {"items": [], "total": 0, "page": 1, "size": 20}


def test_列表_scope三种取值(client):
    owner = account(client, "zhangsan", "zhangsan@example.com")
    reader = account(client, "lisi", "lisi@example.com")

    mine = assert_envelope(create(client, owner["headers"], name="我的库"), 200)["data"]["kb_id"]
    theirs_own = assert_envelope(create(client, reader["headers"], name="别人的库"), 200)["data"]["kb_id"]
    assert_envelope(share(client, owner, mine, reader["email"], "write"), 200)

    # owned：只看自己建的，被授权的绝不混进来
    owned = assert_envelope(list_kbs(client, owner["headers"], scope="owned"), 200)["data"]
    assert [item["kb_id"] for item in owned["items"]] == [mine]

    # shared：只看别人给我的，permission 来自 kb_shares（这里是 write，不是 public 的 read）
    shared = assert_envelope(list_kbs(client, reader["headers"], scope="shared"), 200)["data"]
    assert [item["kb_id"] for item in shared["items"]] == [mine]
    assert shared["items"][0]["permission"] == "write" and shared["items"][0]["is_owner"] is False

    # all：两者合并，自己的那条 is_owner=True，且**不重复计数**
    merged = assert_envelope(list_kbs(client, reader["headers"], scope="all"), 200)["data"]
    assert merged["total"] == 2
    assert {item["kb_id"] for item in merged["items"]} == {mine, theirs_own}
    by_id = {item["kb_id"]: item for item in merged["items"]}
    assert by_id[theirs_own]["is_owner"] is True and by_id[theirs_own]["permission"] == "write"
    assert by_id[mine]["is_owner"] is False


def test_列表_public库对所有人只读(client, run):
    owner = account(client, "zhangsan", "zhangsan@example.com")
    stranger = account(client, "wangwu", "wangwu@example.com")

    kb_id = assert_envelope(create(client, owner["headers"], name="公开手册"), 200)["data"]["kb_id"]
    # 没设 public 之前，陌生人一条都看不见
    assert assert_envelope(list_kbs(client, stranger["headers"]), 200)["data"]["total"] == 0

    assert_envelope(
        client.post("/api/v1/kb/update", headers=owner["headers"], params={"kb_id": kb_id},
                    json={"visibility": "public"}), 200)

    items = assert_envelope(list_kbs(client, stranger["headers"], scope="shared"), 200)["data"]["items"]
    assert [item["kb_id"] for item in items] == [kb_id]
    assert items[0]["permission"] == "read" and items[0]["is_owner"] is False


def test_列表_非法scope_400(client, logged_in):
    assert_envelope(list_kbs(client, logged_in["headers"], scope="everything"), 400)


def test_列表_分页与条数上限(client, logged_in):
    for index in range(3):
        assert_envelope(create(client, logged_in["headers"], name=f"库{index}"), 200)
    page = assert_envelope(list_kbs(client, logged_in["headers"], page=1, size=2), 200)["data"]
    assert page["total"] == 3 and len(page["items"]) == 2
    assert_envelope(list_kbs(client, logged_in["headers"], size=200), 422)  # size 上限 100


# --------------------------------------------------------------------------
# 详情 / 修改
# --------------------------------------------------------------------------


def detail(client, headers, kb_id):
    return client.get("/api/v1/kb/detail", headers=headers, params={"kb_id": kb_id})


def update(client, headers, kb_id, payload):
    return client.post("/api/v1/kb/update", headers=headers, params={"kb_id": kb_id}, json=payload)


def test_详情_越权一律404(client, logged_in, other_headers):
    kb_id = assert_envelope(create(client, logged_in["headers"]), 200)["data"]["kb_id"]
    assert assert_envelope(detail(client, logged_in["headers"], kb_id), 200)["data"]["name"] == "产品资料"
    assert_envelope(detail(client, other_headers, kb_id), 404)
    assert_envelope(detail(client, logged_in["headers"], "00000000-0000-0000-0000-000000000000"), 404)
    assert_envelope(detail(client, logged_in["headers"], "not-a-uuid"), 404)


def test_修改_只改提交的字段(client, logged_in):
    kb_id = assert_envelope(
        create(client, logged_in["headers"], name="旧名", description="旧说明", chunk_size=600), 200)["data"]["kb_id"]

    data = assert_envelope(update(client, logged_in["headers"], kb_id, {"name": "新名"}), 200)["data"]
    assert data["name"] == "新名"
    assert data["description"] == "旧说明" and data["chunk_size"] == 600


def test_修改_空body是422而不是静默成功(client, logged_in):
    """前端漏传字段是常见病：返回 200 + "修改成功" 会把 bug 藏起来"""
    kb_id = assert_envelope(create(client, logged_in["headers"]), 200)["data"]["kb_id"]
    assert_envelope(update(client, logged_in["headers"], kb_id, {}), 422)


def test_修改_改名撞自己的另一个库_409(client, logged_in):
    first = assert_envelope(create(client, logged_in["headers"], name="A库"), 200)["data"]["kb_id"]
    assert_envelope(create(client, logged_in["headers"], name="B库"), 200)
    assert_envelope(update(client, logged_in["headers"], first, {"name": "B库"}), 409)


def test_修改_切块参数不会重切已有文档(client, logged_in):
    """这条是**行为契约**：改参数只是改配置，绝不能顺手触发全库重算（那是一笔 embedding 费用）"""

    kb_id = assert_envelope(create(client, logged_in["headers"], chunk_size=500), 200)["data"]["kb_id"]
    data = assert_envelope(update(client, logged_in["headers"], kb_id, {"chunk_size": 900}), 200)["data"]
    assert data["chunk_size"] == 900 and data["document_count"] == 0


def test_修改_被授权read_write都不行(client, run):
    """矩阵里最容易被写坏的一格：改库设置/删库/改授权 = owner 专属，write 授权也不行"""
    owner = account(client, "zhangsan", "zhangsan@example.com")
    writer = account(client, "lisi", "lisi@example.com")
    kb_id = assert_envelope(create(client, owner["headers"], name="团队库"), 200)["data"]["kb_id"]

    # write 授权能上传（下面 upload 用例验证），但不能改库设置
    assert_envelope(share(client, owner, kb_id, writer["email"], "write"), 200)
    body = assert_envelope(update(client, writer["headers"], kb_id, {"name": "我说了算"}), 403)
    assert "创建者" in body["msg"]
    assert assert_envelope(detail(client, writer["headers"], kb_id), 200)["data"]["name"] == "团队库"


def test_删除路由必须排在共享授权之后(client):
    """防回归：`DELETE /kb/{kb_id}` 写在 `DELETE /kb/share` 之前的话，
    取消授权会先撞上动态段，"share" 被当 uuid 解析 → 422，接口整个坏掉。"""
    paths = [route.path for route in kb_router.routes]
    assert paths.index("/kb/share") < paths.index("/kb/{kb_id}")


# --------------------------------------------------------------------------
# 删库（含磁盘回收）
# --------------------------------------------------------------------------


def test_删库_连文档带磁盘一起收(client, logged_in, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    kb_id = assert_envelope(create(client, logged_in["headers"], name="要删的库"), 200)["data"]["kb_id"]

    assert_envelope(upload(client, logged_in["headers"], kb_id=kb_id, content=b"# body"), 200)
    owner_id = logged_in["user_id"]
    assert uploaded_files(tmp_path, owner_id, kb_id), "上传之后目录里应该有文件"

    body = assert_envelope(client.delete(f"/api/v1/kb/{kb_id}", headers=logged_in["headers"]), 200)
    data = body["data"]
    assert data["kb_id"] == kb_id and data["document_count"] == 1
    assert data["files_removed"] == 1 and data["files_missing"] == 0

    # 磁盘和记录一起消失，不留孤儿
    assert uploaded_files(tmp_path, owner_id, kb_id) == []
    assert assert_envelope(detail(client, logged_in["headers"], kb_id), 404)
    assert (tmp_path / "data" / str(owner_id)).exists()  # 只收库目录，用户目录本身不动


def test_删库_被授权人传到share目录的也收干净(client, run, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    owner = account(client, "zhangsan", "zhangsan@example.com")
    writer = account(client, "lisi", "lisi@example.com")
    kb_id = assert_envelope(create(client, owner["headers"], name="协作库"), 200)["data"]["kb_id"]
    assert_envelope(share(client, owner, kb_id, writer["email"], "write"), 200)

    assert_envelope(upload(client, writer["headers"], kb_id=kb_id, content=b"# collab"), 200)
    assert uploaded_files(tmp_path, kb_id=kb_id), "被授权人的文件落在 data/share/{kb_id}/"

    data = assert_envelope(client.delete(f"/api/v1/kb/{kb_id}", headers=owner["headers"]), 200)["data"]
    assert data["files_removed"] == 1
    assert uploaded_files(tmp_path, kb_id=kb_id) == []


def test_删库_只有owner能删(client, logged_in, other_headers, run):
    kb_id = assert_envelope(create(client, logged_in["headers"]), 200)["data"]["kb_id"]
    assert_envelope(client.delete(f"/api/v1/kb/{kb_id}", headers=other_headers), 404)
    assert_envelope(detail(client, logged_in["headers"], kb_id), 200)  # 没删掉


def test_删空库也成功(client, logged_in):
    kb_id = assert_envelope(create(client, logged_in["headers"], name="空库"), 200)["data"]["kb_id"]
    data = assert_envelope(client.delete(f"/api/v1/kb/{kb_id}", headers=logged_in["headers"]), 200)["data"]
    assert data["document_count"] == 0 and data["files_removed"] == 0


def test_删库之后同名可以重建(client, logged_in):
    """为什么删库走硬删：uq(owner_id, name) 不含 deleted_at，软删会让这个名字永远被占着"""
    assert_envelope(create(client, logged_in["headers"], name="反复横跳"), 200)
    listed = assert_envelope(client.get("/api/v1/kb/list", headers=logged_in["headers"]), 200)
    kb_id = listed["data"]["items"][0]["kb_id"]
    assert_envelope(client.delete(f"/api/v1/kb/{kb_id}", headers=logged_in["headers"]), 200)
    again = assert_envelope(create(client, logged_in["headers"], name="反复横跳"), 200)
    assert again["data"]["kb_id"] != kb_id
