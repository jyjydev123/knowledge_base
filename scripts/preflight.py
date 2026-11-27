#!/usr/bin/env python3
"""启动前体检：把"现在这套配置到底能跑到哪一步"打印成一屏

    ./start.sh check
    .venv/bin/python scripts/preflight.py

刻意**只读配置、不连任何服务**：不连 PG、不 ping Redis、不调 embedding。
所以它能在断网环境和 CI 里跑，也不会因为"连不上"就报一堆假故障。
真实连通性看 `GET /health`（它才真的探库、列缺表）。

为什么要这个脚本：`./start.sh` 起来只告诉你 "Uvicorn running on ..."，
而向量入库、语义检索、长期记忆这三件事**没配 key 时是自动降级的**（应用照样跑），
现象是"功能好像有又好像没有"。体检一遍就知道到底缺哪一个。
"""

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import settings  # noqa: E402
from app.db import models  # noqa: E402,F401  建表前必须导入，否则 metadata 里表不齐
from app.db.base import Base  # noqa: E402
from app.rag.retrieval import vector_lane_block_reason  # noqa: E402


def redact(url: str) -> str:
    """连接串只留 host:port/path —— 密码和用户名绝不进日志/终端（/health 同一条规矩）"""
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    netloc = parts.hostname if not parts.port else f'{parts.hostname}:{parts.port}'
    return urlunsplit((parts.scheme, netloc, parts.path, '', ''))


def transport(url: str) -> str:
    text = str(url or '')
    for scheme in ('rediss', 'redis', 'unix', 'http', 'https'):
        if text.startswith(f'{scheme}://'):
            return scheme
    return 'unknown'


def display_width(text: str) -> int:
    """终端里中日韩字符占两列。用 len() 对齐会让中文行比英文行短一截（看着像排版坏了）"""
    return sum(2 if ord(char) > 0x2E80 else 1 for char in text)


def pad(text: str, width: int) -> str:
    return text + ' ' * max(0, width - display_width(text))


def build_checks() -> list[tuple[str, bool, str]]:
    vector_block = vector_lane_block_reason()
    memory_block = settings.memory_lane_block_reason

    # 端点与模型对不上是这类"多家 OpenAI 兼容协议"配置最容易犯的错：
    # 模型写 deepseek-chat、base 却指向 api.openai.com，请求发出去只会拿到 4xx，
    # 而对方的报错信息里不会提"你 base 配错了"半个字（本项目 .env 就中过一次）。
    model = (settings.LLM_MODEL or '').lower()
    base = (settings.LLM_BASE_URL or '').lower()
    suspect = ('deepseek' in model and 'deepseek' not in base) or ('gpt-' in model and 'deepseek' in base)

    return [
        ('数据库', True, f'{redact(settings.DATABASE_URL)}  pool={settings.DB_POOL_SIZE}+{settings.DB_MAX_OVERFLOW}'),
        ('表结构', True, f'{len(Base.metadata.tables)} 张表（真源 app/db/models.py；建表 scripts/init_db.py）'),
        (
            'LLM 问答',
            settings.llm_ready,
            f'{settings.LLM_MODEL} @ {redact(settings.LLM_BASE_URL)}'
            + ('' if settings.llm_ready and not suspect else '  → ' + '；'.join(
                part
                for part in (
                    '' if settings.llm_ready else '未配 key，问答会明确报 503',
                    f'⚠ 模型与端点看着不匹配（{settings.LLM_MODEL} 发往 {base}）' if suspect else '',
                )
                if part
            )),
        ),
        (
            'Embedding',
            settings.embedding_ready,
            f'{settings.EMBEDDING_MODEL} / {settings.EMBEDDING_DIMS} 维 / 单请求 {settings.EMBEDDING_BATCH_SIZE} 条'
            f' @ {redact(settings.EMBEDDING_BASE_URL)}'
            + ('' if settings.embedding_ready else '  → 未配 EMBEDDING_API_KEY'),
        ),
        (
            '视觉车道',
            settings.vision_ready,
            f'{settings.VISION_MODEL} @ {redact(settings.VISION_BASE_URL)}'
            + ('  ← key 复用 EMBEDDING_API_KEY' if not settings.VISION_API_KEY and settings.EMBEDDING_API_KEY else '')
            + ('' if settings.vision_ready else f'  → {settings.vision_lane_block_reason}'),
        ),
        (
            '向量车道',
            settings.vector_lane_ready,
            '可用（scripts/index_documents.py 能真跑）'
            if settings.vector_lane_ready
            else f'{vector_block}  → 入库会跳过、检索自动退回关键词',
        ),
        (
            '长期记忆',
            not memory_block,
            f'MEMORY_BACKEND={settings.MEMORY_BACKEND}' + (f'  → {memory_block}' if memory_block else '  → 可跑'),
        ),
        (
            '检索车道',
            True,
            f'RETRIEVAL_LANE={settings.RETRIEVAL_LANE}（默认 keyword，向量验收后再切）'
            f' min_score={settings.RETRIEVAL_MIN_SCORE}',
        ),
        (
            'Redis 缓存',
            settings.REDIS_ENABLED,
            (
                f'{transport(settings.REDIS_URL)}://… TTL={settings.USER_CACHE_TTL_SECONDS}s'
                if settings.REDIS_ENABLED
                else '已关闭（REDIS_ENABLED=False），鉴权每次都查库'
            ) + '  ← 连不上会自动熔断回源，不阻塞请求',
        ),
        (
            '上传限制',
            True,
            f'文档 {settings.UPLOAD_MAX_SIZE_MB}MB / 图片 {settings.IMAGE_MAX_SIZE_MB}MB'
            + ('，空文件拒收' if settings.UPLOAD_REJECT_EMPTY else '，空文件放行'),
        ),
        (
            '启动策略',
            True,
            f'DEBUG={settings.DEBUG}（环境变量名是 APP_DEBUG）'
            f' STRICT={settings.DB_STRICT_STARTUP} AUTO_CREATE={settings.AUTO_CREATE_TABLES}',
        ),
    ]


def main() -> int:
    checks = build_checks()
    warn = sum(0 if ok else 1 for _, ok, _ in checks)

    for name, ok, detail in checks:
        print(f'{"OK" if ok else "!!"}  {pad(name, 16)} {detail}')

    print()
    if warn == 0:
        print('结论：配置齐全，可以直接起。能不能真连上 PG / pgvector 扩展装没装，看 GET /health。')
    else:
        print(f'结论：{warn} 项没配好。应用照样能起，但对应功能会**自动降级**（不报错、也不静默，原因写在 /health）。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
