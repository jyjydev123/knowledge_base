"""文档向量化入库 CLI（替掉 app/rag/memory/insert_vector.py 那份草稿）

和草稿的区别就是这条链路真的接进了业务数据：从 `documents`/`chunks` 取，
metadata 带 user_id/kb_id/document_id/chunk_id，按 document_id 先删后插所以可以反复跑，
**不碰长期记忆的 collection，也绝不 drop_tables**。

    .venv/bin/python scripts/index_documents.py --check              # 先看车道能不能跑
    .venv/bin/python scripts/index_documents.py --document <id>      # 单篇
    .venv/bin/python scripts/index_documents.py --kb <id>            # 整库（按 limit 分批）
    .venv/bin/python scripts/index_documents.py --all                # 全库重建（慢，注意配额）
    .venv/bin/python scripts/index_documents.py --kb <id> --query 怎么用 pgvector 检索
    .venv/bin/python scripts/index_documents.py --kb <id> --query 报销 --lane hybrid

--query 走的是**线上同一条代码**（app/rag/retrieval.py），不是另写一遍检索逻辑：
脚本里自测通过不代表线上通过，反之亦然，所以只允许一份实现。
检索必须带 --kb：范围是权限的载体，脚本也没有"以某个用户身份读所有库"的语境，
所以不提供全库检索这个选项（retrieval 层收到空范围就是空结果）。

前提：PG 库已建（scripts/init_db.py）+ 文档已解析入库（chunks 有数据）+ EMBEDDING_API_KEY 已配。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.db import models  # noqa: F401,E402  建 session 前确保模型都注册了
from app.db.models import DocStatus, Document  # noqa: E402
from app.db.psql_config import AsyncSessionLocal  # noqa: E402
from app.rag import retrieval  # noqa: E402
from app.services import vector_index  # noqa: E402


async def _targets(kb_id: str | None, document_id: str | None, all_docs: bool, limit: int) -> list[str]:
    """选出要索引的 document_id：只挑已经切块入库成功的"""
    from app.db.crud import document_crud

    async with AsyncSessionLocal() as db:
        if document_id:
            document = await document_crud.get_document_any(document_id, db)
            return [str(document.id)] if document else []

        conditions = [Document.status == DocStatus.INDEXED, Document.deleted_at.is_(None)]
        if kb_id:
            conditions.append(Document.kb_id == kb_id)
        result = await db.execute(
            select(Document.id).where(*conditions).order_by(Document.created_at).limit(limit)
        )
        if not (kb_id or all_docs or document_id):
            return []
        return [str(row[0]) for row in result.all()]


async def _run(args: argparse.Namespace) -> int:
    ready, why = vector_index.embedding_ready()
    print(f"向量车道: {'可用' if ready else '不可用'} —— {why}")
    print(f"连接: {settings.DATABASE_URL.split('@')[-1]}  collection: {settings.DOCUMENT_VECTOR_COLLECTION}")
    if not ready and not args.check:
        print("请先在 .env 里配 EMBEDDING_API_KEY（端点/模型/维度已有可用默认值："
              "百炼兼容模式 + text-embedding-v3 + 1024 维，单请求 10 条）", file=sys.stderr)
        return 2
    if args.check:
        return 0

    targets = await _targets(args.kb, args.document, args.all, args.limit)
    if not targets:
        print("没有符合条件的文档（要求 status=indexed；先用上传接口或 process_document 把切块做出来）")
        return 1

    total_written = total_removed = failed = 0
    async with AsyncSessionLocal() as db:
        for index, doc_id in enumerate(targets, 1):
            result = await vector_index.index_document(doc_id, session=db, batch_size=args.batch_size)
            total_written += result["indexed"]
            total_removed += result["removed"]
            failed += 0 if result["ok"] else 1
            reason = f" 原因={result['reason']}" if result["reason"] else ""
            mark = "OK " if result["ok"] else "FAIL"
            print(f"[{index}/{len(targets)}] {mark} 写入={result['indexed']} 清理旧向量={result['removed']}{reason}")

    print(f"\n完成：文档 {len(targets)} 篇，写入向量 {total_written} 条，失败 {failed} 篇")

    if args.query:
        print(f"\n=== 检索自检（lane={args.lane} kb_id={args.kb or '未给'}）===")
        if not args.kb:
            print("  --query 必须和 --kb 一起用：检索范围就是权限范围", file=sys.stderr)
            print("  脚本没有「以某个用户身份读所有库」的语境，所以不提供全库检索这个选项", file=sys.stderr)
            return 1
        async with AsyncSessionLocal() as db:
            found = await retrieval.retrieve(args.query, kb_ids=[args.kb], db=db, top_k=args.top_k, lane=args.lane)
        info = found.as_dict()
        print(f"  车道={info['lane']} 生效={info['mode']} 候选={info['candidates']}"
              f" 命中={info['hits']} 字数={info['chars']}")
        if info["fallback"]:
            print(f"  ⚠ 已降级：{info['fallback']}")
        if not found.sources:
            print("  没有参考资料：向量表可能是空的（先跑上面的索引），或问题与库内容无关")
        for source in found.sources:
            print(
                f"  [{source['score']:.4f}] {source['lane'] or '?'} 《{source['title']}》"
                f" chunk={source['chunk_id']} :: {source['snippet'][:70]}"
            )
        if not found.text:
            return 1
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="把 chunks 向量化写入 pgvector")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--document", help="只索引这一篇（document_id）")
    group.add_argument("--kb", help="索引这个知识库里所有已入库文档（kb_id）")
    group.add_argument("--all", action="store_true", help="索引全部已入库文档")
    group.add_argument("--check", action="store_true", help="只检查车道是否可用，不写数据")
    parser.add_argument("--limit", type=int, default=200, help="一次最多处理多少篇")
    parser.add_argument("--batch-size", type=int, default=None, help="每批写多少条向量")
    parser.add_argument("--query", help="写入后跑一次检索自检（必须和 --kb 一起用）")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--lane", choices=("keyword", "vector", "hybrid"), default="vector",
                        help="自检走哪条检索车道（默认 vector：这个脚本本来就是验收向量库的）")
    args = parser.parse_args()
    if not any([args.document, args.kb, args.all, args.check]):
        parser.error("至少要给 --document / --kb / --all / --check 之一")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
