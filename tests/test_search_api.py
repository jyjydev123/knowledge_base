"""POST /search 的测试：范围来自权限，零结果不是失败

三条最要紧的：
  1. 点名列人的库 → 404，并且**整次请求不执行**（不做"能搜的照样搜"）；
  2. 不传 kb_ids 时在"读得到的全部库"里搜，共享/public 库要能命中；
  3. 车道、候选数、降级原因必须原样透出去，前端才分得清"没有资料"和"服务坏了"。

种子数据一律用 `seed_chunks`（直接写 chunks 表）而不是 /upload：
搜索命中靠的是切块，而测试车道 DOCUMENT_INDEX_ON_UPLOAD=False 不会去解析，
用上传做检索用例等于把一个真跑过的解析链路塞进每条搜索测试里。
"""

import uuid

from tests.test_chat_api import seed_chunks
from tests.test_document_api import assert_envelope
from tests.test_kb_share_api import account, share


def search(client, headers, **payload):
    return client.post("/api/v1/search", headers=headers, json=payload)


def test_搜到内容并带出处(client, logged_in, run):
    kb_id = seed_chunks(
        run,
        logged_in["user_id"],
        contents=("员工报销流程：先填单子，再走财务审批，超过五千元需要总监签字。",),
    )

    body = assert_envelope(search(client, logged_in["headers"], q="报销流程是什么"), 200)
    data = body["data"]
    assert data["mode"] == "keyword" and data["lane"] == "keyword" and data["fallback"] == ""
    assert data["hits"], "资料里有这几个字就该命中"

    hit = data["hits"][0]
    assert hit["index"] == 1 and hit["lane"] == "keyword"
    assert uuid.UUID(hit["document_id"]) and uuid.UUID(hit["chunk_id"])
    assert hit["title"] == "手册.md" and hit["page"] == 1 and hit["heading"] == "报销"
    assert set(data["scoped_kb_ids"]) == {kb_id}
    assert "五千元" in hit["snippet"]
    assert data["candidates"] >= 1


def test_没搜到也是200(client, logged_in, run):
    seed_chunks(run, logged_in["user_id"], contents=("普通内容，没有问题相关的字。",))
    body = assert_envelope(search(client, logged_in["headers"], q="zzz不存在的词"), 200)
    assert body["data"]["hits"] == []
    assert body["data"]["mode"] in {"none", "no_terms"}
    assert "没有" in body["msg"]


def test_没有任何可读库时是no_scope(client, logged_in):
    data = assert_envelope(search(client, logged_in["headers"], q="随便"), 200)["data"]
    assert data["mode"] == "no_scope" and data["hits"] == [] and data["scoped_kb_ids"] == []


def test_点名列人的库整次请求都不执行(client, run):
    """越权口径：先算可读范围，客户端点名的东西**不能扩大它**"""
    owner = account(client, "zhangsan", "zhangsan@example.com")
    stranger = account(client, "wangwu", "wangwu@example.com")
    kb_id = seed_chunks(run, owner["user_id"], contents=("机密项目的预算表。",), name="机密库")

    body = assert_envelope(search(client, stranger["headers"], q="机密", kb_ids=[kb_id]), 404)
    assert "权限" in body["msg"] or "不存在" in body["msg"]

    # 不点名时也一样搜不到
    hits = assert_envelope(search(client, stranger["headers"], q="机密"), 200)["data"]["hits"]
    assert hits == []


def test_共享库在不点名时也能搜到(client, run):
    owner = account(client, "zhangsan", "zhangsan@example.com")
    reader = account(client, "lisi", "lisi@example.com")
    kb_id = seed_chunks(run, owner["user_id"], contents=("团建预算说明：每人两百元，含交通。",), name="团队库")
    assert_envelope(share(client, owner, kb_id, reader["email"], "read"), 200)

    data = assert_envelope(search(client, reader["headers"], q="团建预算"), 200)["data"]
    assert data["hits"] and {hit["kb_id"] for hit in data["hits"]} == {kb_id}


def test_点名自己的库能把范围收窄(client, logged_in, run):
    kb_a = seed_chunks(run, logged_in["user_id"], contents=("只有A库有的关键词alpha。",), name="A库")
    kb_b = seed_chunks(run, logged_in["user_id"], contents=("只有B库有的关键词beta。",), name="B库")

    only_b = assert_envelope(search(client, logged_in["headers"], q="关键词beta", kb_ids=[kb_b]), 200)["data"]
    assert {hit["kb_id"] for hit in only_b["hits"]} == {kb_b}
    assert set(only_b["scoped_kb_ids"]) == {kb_b}, "范围要如实回显，前端才知道自己搜的是哪几个库"

    both = assert_envelope(search(client, logged_in["headers"], q="有的关键词"), 200)["data"]
    assert {hit["kb_id"] for hit in both["hits"]} == {kb_a, kb_b}


def test_top_k能少返回(client, logged_in, run):
    kb_id = seed_chunks(
        run,
        logged_in["user_id"],
        contents=(f"共同关键词 第{index}段说明文字" for index in range(6)),
    )
    data = assert_envelope(search(client, logged_in["headers"], q="共同关键词", top_k=1, kb_ids=[kb_id]), 200)["data"]
    assert len(data["hits"]) == 1 and data["candidates"] >= 2


def test_请求体校验(client, logged_in):
    assert_envelope(search(client, logged_in["headers"], q=""), 422)
    assert_envelope(search(client, logged_in["headers"], q="   "), 422)
    assert_envelope(search(client, logged_in["headers"], q="x", kb_ids=["not-a-uuid"]), 422)
    assert_envelope(search(client, logged_in["headers"], q="x", top_k=999), 422)
    assert_envelope(search(client, {}, q="x"), 401)


def test_检索总开关对搜索同样生效(client, logged_in, run, monkeypatch):
    from app.core.config import settings

    seed_chunks(run, logged_in["user_id"], contents=("报销流程说明。",))
    monkeypatch.setattr(settings, "CHAT_RETRIEVAL_ENABLED", False)

    data = assert_envelope(search(client, logged_in["headers"], q="报销"), 200)["data"]
    assert data["mode"] == "disabled" and data["hits"] == []
