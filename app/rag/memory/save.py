"""长期记忆的 PGVector 车道：向量库与 embedding 客户端的初始化

和 local 后端是同一个接口，只是把「词面 hash」换成「真语义向量」。
用 lru_cache 保成单例：PGVector 在构造时就会连库、建扩展、建表、建 collection，
每轮对话 new 一个等于每轮建一次表，绝对不能那样用。

三个容易踩的点，写死在这里：
  1. langchain_postgres.PGVector 是**同步**实现，驱动必须是 psycopg v3，
     所以要把 settings.DATABASE_URL 里的 asyncpg 前缀换掉（见 _to_psycopg_dsn）。
  2. pgvector 扩展和 embedding 维度是**绑死**的：`dimensions` 一旦建过 collection 就不能改，
     换模型必须连 EMBEDDING_DIMS 和 collection 一起换，否则插入时报维度不一致。
  3. DeepSeek 没有 embedding 接口。对话模型和向量模型是两件事，各自配各家：
     LLM_API_KEY / LLM_BASE_URL 走对话，EMBEDDING_API_KEY / EMBEDDING_BASE_URL 走向量。
     本项目 embedding 默认阿里云百炼的 OpenAI 兼容模式（text-embedding-v3 / 1024 维），
     所以**只需要填一个 EMBEDDING_API_KEY**，端点和模型名都有可用默认值。
  4. 百炼 v3/v4 单请求最多 10 条文本，而 langchain 的 OpenAIEmbeddings 默认
     chunk_size=1000 —— 不显式传就等于每批都 400。见下面 build_embeddings 里的 chunk_size。
"""

from __future__ import annotations

from functools import lru_cache

from app.core.config import DASHSCOPE_EMBEDDING_DIMS, settings
from app.rag.memory.base import MemoryNotConfiguredError


def _supports_dimensions(model: str) -> bool:
    """这个 embedding 模型吃不吃 dimensions 参数（百炼 v3/v4 + OpenAI v3 系列吃）"""
    name = (model or "").strip().lower()
    return name in DASHSCOPE_EMBEDDING_DIMS or name.startswith("text-embedding-3")


def _to_psycopg_dsn(url: str) -> str:
    """把各种写法统一成 SQLAlchemy + psycopg v3 的同步连接串

    postgresql+asyncpg://... → postgresql+psycopg://...
    postgres://...           → postgresql+psycopg://...
    postgresql://...         → postgresql+psycopg://...
    其余部分（主机、端口、库名、参数）原样保留。
    """
    for prefix in ("postgresql+asyncpg://", "postgresql+psycopg://", "postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def build_embeddings():
    """按配置造 embedding 客户端（没有 key 就明确报错，不要静默走 OpenAI 默认值）

    传参的每一条都有理由，改之前先看注释：
      api_key     —— 只从 settings 读，绝不落进代码或日志；
      openai_api_base —— 默认已是百炼兼容端点，换厂商只改 EMBEDDING_BASE_URL；
      dimensions  —— 百炼 v3/v4 与 OpenAI v3 系列支持，其余模型传了会 400；
      chunk_size  —— 批量条数。百炼上限 10，用 settings.EMBEDDING_BATCH_SIZE 覆盖
                     langchain 默认的 1000，否则 add_texts 一批就 400。
    """
    from langchain_openai import OpenAIEmbeddings

    if not settings.embedding_ready:
        raise MemoryNotConfiguredError(
            "pgvector 车道需要 embedding 服务：请在 .env 里配 EMBEDDING_API_KEY"
            f"（模型默认 {settings.EMBEDDING_MODEL}，端点 {settings.EMBEDDING_BASE_URL}；"
            "DeepSeek 没有 embedding 接口，LLM_API_KEY 不能拿来当这个用）"
        )

    kwargs: dict = {
        "model": settings.EMBEDDING_MODEL,
        "api_key": settings.EMBEDDING_API_KEY,
        "timeout": settings.LLM_TIMEOUT_SECONDS,
        "chunk_size": max(1, settings.EMBEDDING_BATCH_SIZE),
    }
    if settings.EMBEDDING_BASE_URL:
        kwargs["openai_api_base"] = settings.EMBEDDING_BASE_URL
    # dimensions 只有 v3 系列（OpenAI text-embedding-3-* 与百炼 text-embedding-v3/v4）支持；
    # 给老模型传这个参数会 400，所以按模型名白名单判断，不盲传。
    if settings.EMBEDDING_DIMS and _supports_dimensions(settings.EMBEDDING_MODEL):
        kwargs["dimensions"] = settings.EMBEDDING_DIMS
    return OpenAIEmbeddings(**kwargs)


def _build_vectorstore(collection_name: str):
    from langchain_postgres import PGVector
    from langchain_postgres.vectorstores import DistanceStrategy

    if settings.DATABASE_URL.startswith("sqlite"):
        raise MemoryNotConfiguredError(
            "pgvector 后端连不了 SQLite：请把 DATABASE_URL 换成 PostgreSQL（开发期可先用 MEMORY_BACKEND=local）"
        )

    return PGVector(
        embeddings=build_embeddings(),
        connection=_to_psycopg_dsn(settings.DATABASE_URL),
        collection_name=collection_name,
        distance_strategy=DistanceStrategy.COSINE,
        embedding_length=settings.EMBEDDING_DIMS,
        use_jsonb=True,        # metadata 存 JSONB，user_id/document_id 过滤才走得动
        create_extension=True,  # 首次用时顺手 CREATE EXTENSION vector（需要账号有权限）
    )


@lru_cache(maxsize=1)
def get_vectorstore():
    """长期记忆用的 collection（全局单例，首次调用才连库建表）"""
    return _build_vectorstore(settings.MEMORY_PG_COLLECTION)


@lru_cache(maxsize=1)
def get_document_vectorstore():
    """知识库文档向量用的 collection —— **必须和记忆分开**

    混在一个 collection 里，检索时"用户记忆"和"文档片段"会互相命中，
    而且清理某个文档的向量会把记忆条目一起带走。分开之后两边都能独立重建。
    """
    return _build_vectorstore(settings.DOCUMENT_VECTOR_COLLECTION)


def reset_vectorstore_cache() -> None:
    """换配置 / 跑测试后用一次，否则旧的单例会带着上一条连接的池子继续用"""
    get_vectorstore.cache_clear()
    get_document_vectorstore.cache_clear()
