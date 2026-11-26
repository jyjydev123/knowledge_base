"""向量化这一路的测试：配置校验、embedding 客户端参数、回表、向量/混合检索车道

三条纪律：
  1. **不联网**。langchain_openai.OpenAIEmbeddings 全程被 monkeypatch 成"只记录参数"的假类，
     断言的是我们到底把什么参数发出去了（chunk_size=10 这种真金白银的坑）。
  2. **不依赖 PG**。向量库换成假 store（只实现 similarity_search_with_relevance_scores），
     但**回表走真 SQLite**：召回之后能不能正确落到 chunks 行、软删的文档能不能断掉，
     只有真表测得出来。
  3. **权限行为必须测**。kb_ids 为空时不许问向量库；filter 里不许出现 user_id。
"""

import uuid
from types import SimpleNamespace

import pytest
from langchain_core.documents import Document as LCDocument
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.db.crud import chunk_crud
from app.rag import retrieval
from app.rag.memory.base import MemoryNotConfiguredError
from app.services.chunker import ChunkPiece
from tests.test_schema_models import _add_doc, _add_kb, _add_user

TRAVEL = "差旅报销标准是每季度五千元，超出部分需要总监审批。"
PURCHASE = "采购合同的付款方式是月结三十天，验收合格后开具发票。"


def _ready(monkeypatch, *, with_key=True):
    """把配置切到"向量车道看起来可用"：PG 连接串 + 一个像样的 key

    测试车道全局连的是 SQLite（conftest 设的），所以**每个**想测向量车道的用例都必须
    先把 DATABASE_URL 换成 PG 串，否则真实拦路的是"库不对"而不是我们要测的那个条件。
    """
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@127.0.0.1:5432/kb")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-looking-real" if with_key else "")


class FakeStore:
    """只实现检索车道用到的那一个方法"""

    def __init__(self, pairs=(), error=None):
        self.calls: list = []
        self.pairs = list(pairs)
        self.error = error

    def similarity_search_with_relevance_scores(self, query, k=4, filter=None, **kwargs):
        self.calls.append(SimpleNamespace(query=query, k=k, filter=filter))
        if self.error is not None:
            raise self.error
        return self.pairs[:k]


def _pair(chunk, score, *, text=None):
    return (
        LCDocument(
            page_content=text if text is not None else "向量表里的旧快照（正文应以 chunks 表为准）",
            metadata={"chunk_id": str(chunk.id), "kb_id": str(chunk.kb_id), "document_id": str(chunk.document_id)},
        ),
        score,
    )


async def _seed(db, contents=(TRAVEL, PURCHASE), docname="a.md"):
    user = await _add_user(db, f"vr{uuid.uuid4().hex[:6]}", f"vr{uuid.uuid4().hex[:6]}@example.com")
    kb = await _add_kb(db, user.id, name=f"库-{uuid.uuid4().hex[:4]}")
    document = await _add_doc(db, kb.id, user.id, filename=docname)
    pieces = [ChunkPiece(seq=index, content=text, page=index + 1, heading=f"第{index + 1}章")
              for index, text in enumerate(contents)]
    await chunk_crud.replace_document_chunks(document, pieces, db)
    chunks = await chunk_crud.list_document_chunks(document.id, db)
    return SimpleNamespace(user=user, kb=kb, document=document, chunks=chunks)


# --------------------------------------------------------------------------
# 配置：不可逆项必须在启动时就拦住
# --------------------------------------------------------------------------


def test_embedding_默认值是百炼v3():
    """默认值就是文档：填一个 key 就能跑，不需要用户去猜端点"""
    conf = Settings(_env_file=None)
    assert conf.EMBEDDING_MODEL == "text-embedding-v3"
    assert conf.EMBEDDING_DIMS == 1024
    assert "dashscope" in conf.EMBEDDING_BASE_URL and conf.EMBEDDING_BASE_URL.endswith("/v1")
    assert conf.EMBEDDING_BATCH_SIZE == 10          # 百炼单请求上限，langchain 默认 1000
    assert conf.embedding_is_dashscope is True


def test_embedding_非法维度直接不让进程起来():
    """维度写错 = 往库里写不可逆的脏数据，报错要发生在启动时而不是第一次插入"""
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, EMBEDDING_DIMS=1536)
    assert "text-embedding-v3" in str(exc.value) and "1024" in str(exc.value)

    for legal in (64, 512, 768, 1024):
        assert Settings(_env_file=None, EMBEDDING_DIMS=legal).EMBEDDING_DIMS == legal


def test_embedding_批量超限直接报错():
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, EMBEDDING_BATCH_SIZE=64)
    assert "EMBEDDING_BATCH_SIZE" in str(exc.value)
    assert Settings(_env_file=None, EMBEDDING_BATCH_SIZE=1).EMBEDDING_BATCH_SIZE == 1


def test_embedding_非百炼模型不套百炼的维度表():
    """换成 OpenAI 本体时不该被 v3 的维度白名单拦住（也别拦住 batch）"""
    conf = Settings(_env_file=None, EMBEDDING_MODEL="text-embedding-3-small", EMBEDDING_DIMS=1536,
                    EMBEDDING_BASE_URL="https://api.openai.com/v1", EMBEDDING_BATCH_SIZE=512)
    assert conf.embedding_is_dashscope is False


def test_embedding_空字符串回退默认值():
    """'.env 里写了 EMBEDDING_MODEL ='这种半拉子配置很常见，不能因此把端点打到 OpenAI 本体"""
    conf = Settings(_env_file=None, EMBEDDING_MODEL="", EMBEDDING_BASE_URL="")
    assert conf.EMBEDDING_MODEL == "text-embedding-v3"
    assert conf.EMBEDDING_BASE_URL.endswith("/compatible-mode/v1")


def test_embedding_ready_认占位key(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "your_api_key_here")
    assert settings.embedding_ready is False
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-abcd1234")
    assert settings.embedding_ready is True


def test_vector_lane_ready_口径(monkeypatch):
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setattr(settings, "DATABASE_URL", "postgresql+asyncpg://u:p@h/db")
    assert settings.vector_lane_ready is False
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk-looking-real")
    assert settings.vector_lane_ready is True
    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///x.db")
    assert settings.vector_lane_ready is False


# --------------------------------------------------------------------------
# embedding 客户端：参数就是钱和 400
# --------------------------------------------------------------------------


@pytest.fixture()
def captured_kwargs(monkeypatch):
    """把 OpenAIEmbeddings 换成"只记录参数"的假类"""
    seen: dict = {}

    class Recorder:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    fake_module = SimpleNamespace(OpenAIEmbeddings=Recorder)
    monkeypatch.setitem(__import__("sys").modules, "langchain_openai", fake_module)
    return seen


def test_build_embeddings_参数按百炼的规矩来(captured_kwargs, monkeypatch):
    from app.rag.memory.save import build_embeddings

    _ready(monkeypatch)
    captured_kwargs.clear()
    build_embeddings()
    assert captured_kwargs["model"] == "text-embedding-v3"
    assert captured_kwargs["api_key"] == "sk-looking-real"
    assert captured_kwargs["openai_api_base"] == settings.EMBEDDING_BASE_URL
    assert captured_kwargs["dimensions"] == 1024
    # 这两条是本项目真金白银的坑：批量必须 <=10，否则 PGVector 每次 add_texts 都 400
    assert captured_kwargs["chunk_size"] == settings.EMBEDDING_BATCH_SIZE == 10
    assert captured_kwargs["timeout"] == settings.LLM_TIMEOUT_SECONDS


def test_build_embeddings_不支持维度的模型不盲传(captured_kwargs, monkeypatch):
    from app.rag.memory.save import build_embeddings

    _ready(monkeypatch)
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "text-embedding-v2")
    captured_kwargs.clear()
    build_embeddings()
    assert "dimensions" not in captured_kwargs
    assert captured_kwargs["chunk_size"] == 10      # 批量上限和模型无关，照传


def test_build_embeddings_没有key时报错而不是静默打向远端(monkeypatch):
    from app.rag.memory.save import build_embeddings

    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    with pytest.raises(MemoryNotConfiguredError) as exc:
        build_embeddings()
    assert "EMBEDDING_API_KEY" in str(exc.value)


# --------------------------------------------------------------------------
# 回表：chunk_crud.list_chunks_by_ids
# --------------------------------------------------------------------------


def test_回表拿到和关键词车道同形状的字典(run):
    seed = run(_seed)
    rows = run(lambda db: chunk_crud.list_chunks_by_ids([c.id for c in seed.chunks], [seed.kb.id], db))
    assert len(rows) == 2
    assert set(rows[0]) == {
        "chunk_id", "kb_id", "document_id", "owner_id", "seq", "content",
        "page", "heading", "token_count", "document_title", "document_filename",
    }
    # 回表**不保证顺序**（按主键过滤，SQLite 给的是主键序），排序是 search_vectors 的活
    by_id = {str(row["chunk_id"]): row for row in rows}
    assert by_id[str(seed.chunks[0].id)]["content"] == TRAVEL, "正文来自 chunks 表，不是向量表"
    assert all(row["document_title"] == "a.md" for row in rows)
    assert all(str(row["kb_id"]) == str(seed.kb.id) for row in rows)


def test_回表按kb范围过滤且不越权(run):
    mine = run(_seed)

    async def _other(db):
        return await _seed(db)

    stranger = run(_other)
    ids = [c.id for c in mine.chunks] + [c.id for c in stranger.chunks]
    # 只给我自己的范围：别人的 chunk_id 传进来也拿不到
    rows = run(lambda db: chunk_crud.list_chunks_by_ids(ids, [mine.kb.id], db))
    assert len(rows) == 2 and {row["kb_id"] for row in rows} == {mine.kb.id}
    # 范围为空 = 什么都拿不到，绝不能变成"不带条件"
    assert run(lambda db: chunk_crud.list_chunks_by_ids(ids, [], db)) == []
    assert run(lambda db: chunk_crud.list_chunks_by_ids([], [mine.kb.id], db)) == []


def test_回表把软删文档断掉(run):
    """路由目前是硬删（chunks 外键级联），但 documents.deleted_at 这个标记一直在筛选条件里：
    哪天改成软删、或者手工在库里打了标记，检索必须挡住，不能把已删文档的内容答给用户。
    """
    from datetime import UTC, datetime

    from app.db.models import Document

    seed = run(_seed)

    async def _mark(db):
        document = await db.get(Document, seed.document.id)
        document.deleted_at = datetime.now(UTC)
        await db.commit()

    run(_mark)
    rows = run(lambda db: chunk_crud.list_chunks_by_ids([c.id for c in seed.chunks], [seed.kb.id], db))
    assert rows == [], "向量表里的脏向量必须被软删标记挡住，不能进 prompt"


def test_回表容忍脏id(run):
    seed = run(_seed)
    rows = run(lambda db: chunk_crud.list_chunks_by_ids(
        [seed.chunks[0].id, "not-a-uuid", None], [seed.kb.id, "bad"], db))
    assert len(rows) == 1


# --------------------------------------------------------------------------
# search_vectors：召回 + 降级
# --------------------------------------------------------------------------


def test_过滤条件只按kb_id不按user_id(monkeypatch):
    _ready(monkeypatch)
    flt = retrieval.vector_filter(["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"])
    assert set(flt) == {"kb_id"}
    assert flt["kb_id"]["$in"] == ["11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"]


def test_车道不可用时给出人话原因(run, monkeypatch):
    _ready(monkeypatch, with_key=False)
    hits, reason = run(lambda db: retrieval.search_vectors("报销", [], db))
    assert hits == [] and "EMBEDDING_API_KEY" in reason

    monkeypatch.setattr(settings, "DATABASE_URL", "sqlite+aiosqlite:///x.db")
    hits, reason = run(lambda db: retrieval.search_vectors("报销", [], db))
    assert hits == [] and "SQLite" in reason


def test_召回后回表取正文(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.91), _pair(seed.chunks[1], 0.42)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    hits, reason = run(lambda db: retrieval.search_vectors("差旅报销标准是多少", [seed.kb.id], db, limit=4))
    assert reason == "" and len(hits) == 2
    assert hits[0]["content"] == TRAVEL
    assert hits[0]["vector_score"] == 0.91
    assert store.calls[0].filter == {"kb_id": {"$in": [str(seed.kb.id)]}}
    assert store.calls[0].k == 4          # 直接调时 limit 就是要召回的条数
    assert hits[0]["vector_score"] > hits[1]["vector_score"], "必须按相似度降序返回，别让最相关的被预算丢掉"


def test_相似度下限能挡住噪音(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.55), _pair(seed.chunks[1], 0.20)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)
    monkeypatch.setattr(settings, "RETRIEVAL_MIN_SCORE", 0.5)

    hits, reason = run(lambda db: retrieval.search_vectors("随便问问", [seed.kb.id], db, limit=4))
    assert reason == "" and len(hits) == 1 and hits[0]["content"] == TRAVEL


def test_向量库初始化失败时降级不抛(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)

    def boom():
        raise RuntimeError("connection refused")

    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", boom)
    hits, reason = run(lambda db: retrieval.search_vectors("报销", [seed.kb.id], db))
    assert hits == [] and "向量库初始化失败" in reason


def test_远端调用失败时降级不抛(run, monkeypatch):
    """欠费 401 / 批量 400 / 超时都从这里退，问答不能因此 500"""
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore(error=RuntimeError("Error code: 401 - invalid API key"))
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("报销标准", kb_ids=[seed.kb.id], db=db, lane="vector"))
    assert found.mode == "keyword" and found.fallback.startswith("向量检索失败")
    assert "401" in found.fallback
    assert found.text, "向量挂了关键词还得把资料带上"


def test_召回的chunk在业务表里不存在时静默丢掉(run, monkeypatch):
    """脏向量（重新切块/删文档后残留）不该进 prompt"""
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.9), (_fake_chunk(), 0.8)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)
    hits, reason = run(lambda db: retrieval.search_vectors("报销", [seed.kb.id], db))
    assert reason == "" and len(hits) == 1


def _fake_chunk():
    """一条在 chunks 表里**不存在**的向量：模拟重新切块/删文档后残留的脏向量"""
    return LCDocument(
        page_content="旧快照",
        metadata={"chunk_id": str(uuid.uuid4()), "kb_id": str(uuid.uuid4()), "document_id": str(uuid.uuid4())},
    )


# --------------------------------------------------------------------------
# 纯函数：排序与合并
# --------------------------------------------------------------------------


def _hit(chunk, content, score, **extra):
    row = {
        "chunk_id": chunk.id, "kb_id": chunk.kb_id, "document_id": chunk.document_id,
        "owner_id": chunk.owner_id, "seq": chunk.seq, "content": content,
        "page": 1, "heading": "", "token_count": 10,
        "document_title": extra.get("title", "a.md"), "document_filename": "a.md",
    }
    if "vector_score" in extra:
        row["vector_score"] = extra["vector_score"]
    row["score"] = score
    return row


def test_向量排序稳定且每篇限流(run):
    seed = run(_seed)
    rows = [
        _hit(seed.chunks[0], TRAVEL, 0.5, vector_score=0.5),
        _hit(seed.chunks[1], PURCHASE, 0.9, vector_score=0.9),
    ]
    picked = retrieval.rank_vector_hits(rows, top_k=4)
    assert [p["score"] for p in picked] == [0.9, 0.5]

    async def _second(db):
        return await _seed(db, contents=("甲" * 40, "乙" * 40, "丙" * 40))

    many = run(_second)
    crowded = [
        _hit(many.chunks[0], "甲" * 40, 0.99, vector_score=0.99),
        _hit(many.chunks[1], "乙" * 40, 0.98, vector_score=0.98),
        _hit(many.chunks[2], "丙" * 40, 0.97, vector_score=0.97),
    ]
    limited = retrieval.rank_vector_hits(crowded, top_k=4)
    assert len(limited) == retrieval.HITS_PER_DOCUMENT, "同一篇文档不能霸占整个参考资料"


def test_合并去重并标注both():
    a, b = uuid.uuid4(), uuid.uuid4()
    vector = [{"chunk_id": a, "document_id": "d1"}, {"chunk_id": b, "document_id": "d2"}]
    keyword = [{"chunk_id": b, "document_id": "d2"}, {"chunk_id": "c", "document_id": "d3"}]
    merged = retrieval.merge_lane_hits(vector, keyword, top_k=4)
    assert [str(m["chunk_id"]) for m in merged] == [str(a), str(b), "c"]
    assert [m["lane"] for m in merged] == ["vector", "both", "keyword"]
    assert retrieval.effective_mode(merged) == "hybrid"
    assert retrieval.effective_mode([]) == "none"
    assert retrieval.effective_mode([{"chunk_id": a, "document_id": "d1", "lane": "vector"}]) == "vector"


def test_合并也受每篇限流():
    rows_vector = [
        {"chunk_id": uuid.uuid4(), "document_id": "d1", "content": "x"}
        for _ in range(3)
    ]
    rows_keyword = [{"chunk_id": uuid.uuid4(), "document_id": "d1", "content": "y"}]
    merged = retrieval.merge_lane_hits(rows_vector, rows_keyword, top_k=4)
    assert len(merged) == retrieval.HITS_PER_DOCUMENT, "两路加起来也不能让一篇文档霸屏"


# --------------------------------------------------------------------------
# retrieve：车道切换的对外行为
# --------------------------------------------------------------------------


def test_默认车道还是keyword(run, monkeypatch):
    """填了 key 也不许悄悄改变线上行为 —— 切车道是显式决定"""
    _ready(monkeypatch)
    seed = run(_seed)
    monkeypatch.setattr(settings, "RETRIEVAL_LANE", "keyword")
    store = FakeStore([_pair(seed.chunks[0], 0.99)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("差旅报销标准是多少", kb_ids=[seed.kb.id], db=db))
    assert found.mode == "keyword" and found.lane == "keyword" and found.fallback == ""
    assert store.calls == [], "关键词车道一次都不该问向量库（省一次出网）"


def test_vector车道命中(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.93)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[seed.kb.id], db=db, lane="vector"))
    assert found.mode == "vector" and found.fallback == ""
    assert found.sources[0]["lane"] == "vector" and found.sources[0]["score"] == 0.93
    assert "差旅报销标准" in found.text
    assert found.candidate_total == 1
    # 线上车道会多召回一倍候选：每篇限流 + 回表掉行之后还够凑满 top_k
    assert store.calls[0].k == settings.CHAT_RETRIEVAL_TOP_K * retrieval.VECTOR_CANDIDATE_MULTIPLIER


def test_vector车道正常但零命中不偷偷换车道(run, monkeypatch):
    """把"库里确实没有"和"embedding 挂了"分成两种可分辨的结果"""
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[seed.kb.id], db=db, lane="vector"))
    assert found.mode == "none" and found.fallback == "" and found.sources == []
    assert store.calls, "确实问过了，没命中是数据问题不是没跑"


def test_hybrid车道两路合并(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.9)])       # 这块关键词也能命中（"差旅报销"）
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("采购合同付款方式", kb_ids=[seed.kb.id], db=db, lane="hybrid"))
    assert found.mode == "hybrid", "向量给一块、关键词给另一块，合起来才是 hybrid"
    lanes = {source["lane"] for source in found.sources}
    assert lanes & {"keyword", "both"}
    assert found.candidate_total >= 2


def test_没配key时vector车道自动降级到keyword(run, monkeypatch):
    _ready(monkeypatch, with_key=False)
    seed = run(_seed)
    found = run(lambda db: retrieval.retrieve("差旅报销标准", kb_ids=[seed.kb.id], db=db, lane="vector"))
    assert found.lane == "vector" and found.mode == "keyword"
    assert "EMBEDDING_API_KEY" in found.fallback


def test_没有范围时根本不碰向量库(run, monkeypatch):
    """越权测试：kb_ids 为空必须直接 no_scope，一次 embedding 调用都不能发出去"""
    _ready(monkeypatch)
    store = FakeStore([(_fake_chunk(), 0.99)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=None, db=db, lane="hybrid"))
    assert found.mode == "no_scope" and found.sources == []
    assert store.calls == []

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[None, ""], db=db, lane="vector"))
    assert found.mode == "no_scope" and store.calls == []


def test_检索总开关优先级最高(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.9)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)
    monkeypatch.setattr(settings, "CHAT_RETRIEVAL_ENABLED", False)

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[seed.kb.id], db=db, lane="hybrid"))
    assert found.mode == "disabled" and found.lane == "hybrid" and store.calls == []


def test_未知车道名退回keyword(run, monkeypatch):
    """车道参数可能来自会话 extra / 前端脏数据，非法值必须退回最便宜且必定可用的一条"""
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.9)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)

    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[seed.kb.id], db=db, lane="semantic"))
    assert found.lane == "keyword" and found.mode == "keyword" and store.calls == []


def test_as_dict_把车道信息都带出去(run, monkeypatch):
    _ready(monkeypatch)
    seed = run(_seed)
    store = FakeStore([_pair(seed.chunks[0], 0.9)])
    monkeypatch.setattr("app.rag.memory.save.get_document_vectorstore", lambda: store)
    found = run(lambda db: retrieval.retrieve("差旅报销", kb_ids=[seed.kb.id], db=db, lane="hybrid"))
    info = found.as_dict()
    assert set(info) == {"mode", "lane", "terms", "candidates", "hits", "chars", "fallback"}
