"""启动期与探库检查

要解决的具体问题：`main.py` 以前既不建表也不查表，**空库上应用照样启动成功**，
`/health` 照样返回 ok —— 然后第一个业务请求才报 `relation "users" does not exist`。
"库没建"和"服务正常"看起来一模一样，排查方向从第一步就歪掉。

三条设计决定：

1. **对外只说类别，不说细节**。连接失败的异常原文里有用户名、密码、内网地址
   （`postgresql+asyncpg://postgres:真密码@127.0.0.1:5432/...`），这些绝不能从 `/health`
   流出去。这里只回 `dialect` / `error` 类别 / `missing_tables`，原文进日志。
2. **内存库直接跳过检查**（`sqlite+aiosqlite:///:memory:`）。它是测试车道：每次连接都是一个
   全新的空库，查了必然报"表缺失"，既没意义又让每个用例多连一次。这一条也保证
   `with TestClient(app)` 触发的 lifespan 不会影响 215 个测试。
3. **默认不改变现有行为**：检查到缺表只 WARNING。生产/Docker 想 fail-fast 就把
   `DB_STRICT_STARTUP=True` 打开；开发想省事可以 `AUTO_CREATE_TABLES=True` 让它启动即建表
   （**只适合单人开发**，正式环境应该用迁移，见 TODO.md P1）。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import settings
from app.db.base import Base

logger = logging.getLogger("app.startup")

ERROR_NONE = None
# 表清单为空：说明 app.db.models 没被导入，元数据里一张表都没有。
# 这种情况如果当"检查通过"，探库就永远是绿的 —— 比报错更危险，所以单独列一种错误。
ERROR_NO_METADATA = "metadata_not_loaded"
ERROR_CONNECTION = "connection_failed"
ERROR_TIMEOUT = "timeout"
ERROR_MISSING_TABLES = "missing_tables"


def get_engine() -> AsyncEngine:
    """单独拿出来做成函数，是为了测试能替换引擎而不用碰全局连接池"""
    from app.db.psql_config import engine as global_engine

    return global_engine


def expected_tables() -> list[str]:
    """期望的表清单

    必须在这里显式导入 models：`Base.metadata.tables` 只包含**已被导入**的模型。
    调用方如果直接传自己的引擎（测试、多库），走不到 `psql_config` 那句
    `from app.db import models`，元数据就是空的 —— 于是"缺 6 张表"会被算成"缺 0 张"。
    """
    from app.db import models  # noqa: F401  注册元数据用，别删

    return sorted(Base.metadata.tables)


def _missing_tables(sync_conn) -> list[str]:
    """run_sync 传进来的是 **Connection**，必须自己拿 inspector

    直接 `sync_conn.get_table_names()` 会 AttributeError —— 而这个异常会被下面的
    except 吞成"连不上"，探库函数就变成永远返回假的正常结果，这种错最难发现。
    """
    from sqlalchemy import inspect as sa_inspect

    present = set(sa_inspect(sync_conn).get_table_names())
    return sorted(set(expected_tables()) - present)


async def inspect_database(engine: AsyncEngine | None = None) -> dict:
    """查库能不能用、表全不全。任何失败都返回结构化结果，不抛异常"""
    eng = engine or get_engine()
    wanted = expected_tables()
    result: dict = {
        "ok": False,
        "dialect": eng.dialect.name,
        "checked_tables": len(wanted),
        "missing_tables": [],
        "error": ERROR_CONNECTION,
    }
    try:
        async with eng.connect() as conn:
            missing = await conn.run_sync(_missing_tables)
    except Exception as exc:  # noqa: BLE001 —— 探库接口不允许把异常抛给调用方
        logger.warning("数据库连接检查失败: %s: %s", type(exc).__name__, str(exc)[:200])
        return result
    if not wanted:  # 表清单为空 = 检查本身没意义，明确失败而不是悄悄通过
        result["error"] = ERROR_NO_METADATA
        logger.error("ORM 元数据里没有任何表：app.db.models 是否被正常导入？")
        return result
    result["missing_tables"] = missing
    result["error"] = None if not missing else ERROR_MISSING_TABLES
    result["ok"] = not missing
    return result


async def create_all(engine: AsyncEngine | None = None) -> None:
    """建表（幂等：`CREATE TABLE IF NOT EXISTS`）。只该在开发车道用"""
    eng = engine or get_engine()
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _is_ephemeral_engine(engine: AsyncEngine) -> bool:
    """看引擎自己的 URL，不看 settings —— 传进来的引擎可能就是测试用的那个

    只判断"是不是内存库"，绝不把 URL 回出去（里面带用户名密码）。
    """
    # 必须看 url.database，不能用 str(url)：SQLAlchemy 会把冒号百分号编码，
    # str() 出来是 sqlite+aiosqlite:///%3Amemory%3A，用 ":memory:" in str(url) 永远判 False。
    database = getattr(engine.url, "database", None)
    return database is None or ":memory:" in str(database)


def is_ephemeral_url(url: str) -> bool:
    """:memory: 或 URL 为空 → 每次连接都是新库，启动检查没有意义"""
    return not url or ":memory:" in url


async def ensure_schema_on_startup(engine: AsyncEngine | None = None) -> dict:
    """lifespan 调这个。返回检查结果，方便调用方（和测试）断言它做了什么"""
    eng = engine or get_engine()
    if is_ephemeral_url(settings.DATABASE_URL):
        logger.debug("内存库/未配置库，跳过启动期表检查")
        return {"skipped": True, "ok": True, "created": False, "missing_tables": []}

    info = await inspect_database(eng)
    if info["ok"]:
        logger.info("数据库检查通过：%d 张表齐备（dialect=%s）", info["checked_tables"], info["dialect"])
        return {"skipped": False, "ok": True, "created": False, "missing_tables": []}

    if info["error"] == ERROR_CONNECTION:
        message = "数据库连不上：请检查 .env 的 DATABASE_URL（用户名/密码/库名）以及 PostgreSQL 是否在运行"
    else:
        message = f"数据库可用，但缺 {len(info['missing_tables'])} 张表：{', '.join(info['missing_tables'])}"

    if settings.AUTO_CREATE_TABLES and info["error"] != ERROR_CONNECTION:
        await create_all(eng)
        after = await inspect_database(eng)
        if after["ok"]:
            logger.warning("AUTO_CREATE_TABLES=True：已自动建表（仅适合开发环境，生产请用迁移）")
            return {"skipped": False, "ok": True, "created": True, "missing_tables": []}
        message = f"自动建表后仍缺表：{', '.join(after['missing_tables'])}"
        info = after

    if settings.DB_STRICT_STARTUP:
        logger.error("启动检查失败（DB_STRICT_STARTUP=True）：%s", message)
        raise RuntimeError(message)

    # 默认只告警不阻断：不改现有行为，但 /health 会如实返回 503
    logger.warning("%s —— 请先运行 `python scripts/init_db.py`（现在起服务可用，但相关接口会报错）", message)
    return {"skipped": False, "ok": False, "created": False, "missing_tables": info["missing_tables"],
            "error": info["error"]}


def _memory_section() -> dict:
    """长期记忆车道的体检：配了什么、真跑不跑得动、跑不动的原因

    这里只报**配置层面**能不能跑（连的是 PG、有没有 embedding key）；
    运行时第一次读写失败仍会把进程内后端降级成"不记"（store._degrade），
    那一步是懒的，不该为它专门连一次库拖慢 /health。
    """
    return {
        "configured": settings.MEMORY_BACKEND,
        "ok": not settings.memory_lane_block_reason,
        "reason": settings.memory_lane_block_reason,
    }


def _vision_section() -> dict:
    """视觉车道（图片 → 描述）的配置层面体检：配了哪个模型、能不能真跑、不能跑的原因

    和 memory/cache 一样**不影响 HTTP 状态码**：视觉车道没配只是"图片不能被搜到"，
    文本知识库照样能用，把它打成 503 会让编排系统摘掉一台正常的实例。
    """
    return {
        "configured": settings.VISION_MODEL if settings.IMAGE_PARSE_ENABLED else "off",
        "ok": settings.vision_ready,
        "reason": settings.vision_lane_block_reason,
        "scanned_pdf": settings.SCANNED_PDF_VISION,
    }


async def cache_section() -> dict:
    """`/health` 里的缓存一节：Redis 这条车道到底有没有在干活

    刻意**不影响 HTTP 状态码**。Redis 只是加速器，它挂了服务照样能用（fail-open 回源数据库），
    把 readiness 打成 503 反而会让编排系统把一台"只是没缓存"的实例摘掉 —— 放大故障。
    但结果必须**看得到**：静默降级会让人一直以为加速在做，直到容量出问题。
    """
    from app.core import cache

    ok, detail = await cache.ping()
    data = cache.status()
    data["ok"] = ok
    data["detail"] = "" if ok else detail
    return data


async def health_payload(engine: AsyncEngine | None = None, timeout_seconds: float | None = None) -> tuple[int, dict]:
    """`/health` 的后端：返回 (HTTP 状态码, ApiResponse.data)

    状态码用 503 而不是 200：编排系统（Docker healthcheck、k8s readiness、CI）
    只会看状态码，不会解析 body。探库必须带超时 —— 远端主机不可达时 TCP 连接会挂在
    内核的重试里，没有超时的话 `/health` 自己就变成故障源。
    """
    import asyncio

    eng = engine or get_engine()
    limit = settings.HEALTH_CHECK_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds

    # 内存库没有持久 schema 是它的定义使然，不是故障。以前这里直接返 503，
    # 和 ensure_schema_on_startup() 对内存库"跳过检查"的口径不一致（一个跳一个红），
    # 结果测试车道里 /health 永远是 503，真故障反而被淹在噪声里。
    if _is_ephemeral_engine(eng):
        return 200, {
            "status": "ok",
            "version": settings.APP_VERSION,
            "database": {
                "ok": True,
                "ephemeral": True,
                "dialect": eng.dialect.name,
                "error": None,
                "missing_tables": [],
                "checked_tables": len(expected_tables()),
            },
            "memory_backend": _memory_section(),
            "vision": _vision_section(),
            "cache": await cache_section(),
        }

    try:
        info = await asyncio.wait_for(inspect_database(eng), timeout=limit)
    except TimeoutError:
        return 503, {
            "status": "degraded",
            "database": {"ok": False, "ephemeral": False, "error": ERROR_TIMEOUT},
            "cache": await cache_section(),
        }
    status_code = 200 if info["ok"] else 503
    return status_code, {
        "status": "ok" if info["ok"] else "degraded",
        "version": settings.APP_VERSION,
        "database": {
            "ok": info["ok"],
            "ephemeral": False,
            "dialect": info["dialect"],
            "error": info["error"],
            "missing_tables": info["missing_tables"],
            "checked_tables": info["checked_tables"],
        },
        "memory_backend": _memory_section(),
        "vision": _vision_section(),
        "cache": await cache_section(),
    }
