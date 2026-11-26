"""用户身份的旁路缓存（cache-aside）：把鉴权那次 users 表 SELECT 变成可选

为什么缓存的是"用户"而不是整个请求结果：
    每条需要登录的请求都要 `get_current_user` → 一次 users 单行查询。
    移动端意味着每次点任何按钮都先打一下数据库；这一行是全站被读得最多的一行。

缓存什么 / 不缓存什么（这条边界是安全属性，不是性能取舍）：
    ✓ id / username / email / role / is_active / 是否已注销 —— 认证与授权只看这些
    ✗ **token 是否已被吊销：永远查库**（`token_crud.is_token_active`）。
      否则"退出登录/改密之后旧 token 立刻失效"会被 TTL 变成"5 分钟内还能用"，
      那不是加速，那是把安全属性换成了性能。
    ✗ hashed_password 不进缓存：改密/注销要验原密码，那些路由一律回源（见 load_for_write）。

两个入口的区别只有一条，但必须写清楚（这是本次实现里最容易埋雷的地方）：
    get_user_cached()   只读车道。命中时返回的是**脱离 Session 的 User 实例**，
                        读字段安全；拿它去改库 = 属性改了、commit 什么都没写、接口还返回 200。
    load_for_write()    写车道。**一定回源**，返回受管对象，可以改可以删。
所以：`update/reset_password/delete/logout` 这几条路由必须走 load_for_write，
并且改完调用 `invalidate()`（铁律 2：先落库，再删键）。
"""

from __future__ import annotations

import logging
import uuid as uuid_lib
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.cache import build_key, delete, get_json, set_json
from app.core.config import settings
from app.db.crud import user_crud
from app.db.models import User

logger = logging.getLogger("app.services.user_cache")

# 结构版本号：字段增减时把它 +1，旧键自然过期，不需要谁去清 Redis
SCHEMA_VERSION = "v1"


def _to_uuid(raw) -> uuid_lib.UUID | None:
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def cache_key(user_id) -> str:
    return build_key("user", SCHEMA_VERSION, user_id)


def _serialize(user: User) -> dict[str, Any]:
    """进缓存的最小字段集。刻意**不含** hashed_password（密码哈希绝不进任何外部存储）"""
    return {
        "id": str(user.id),
        "username": user.username,
        "email": user.email,
        "role": user.role,
        "is_active": bool(user.is_active),
        "deleted": user.deleted_at is not None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
    }


def _reconstruct(snapshot: dict[str, Any]) -> User | None:
    """把快照变回一个 User 实例（**transient，不在 Session 里**）

    任何一处字段解不出来就返回 None（当成缓存未命中，回源数据库）：
    比如改了 SCHEMA_VERSION 之前的旧键、或者中间字段被别的进程写坏了。
    缓存永远不该是"读不出来就报错"的东西。
    """
    try:
        user_id = _to_uuid(snapshot.get("id"))
        if user_id is None:
            return None
        return User(
            id=user_id,
            username=str(snapshot["username"]),
            email=str(snapshot["email"]),
            role=str(snapshot["role"]),
            is_active=bool(snapshot["is_active"]),
            deleted_at=datetime.fromisoformat("1970-01-01T00:00:00+00:00") if snapshot.get("deleted") else None,
            created_at=datetime.fromisoformat(snapshot["created_at"]) if snapshot.get("created_at") else None,
            last_login_at=(
                datetime.fromisoformat(snapshot["last_login_at"]) if snapshot.get("last_login_at") else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.debug("用户缓存快照不可用，回源数据库: %s", exc)
        return None


async def get_user_cached(user_id, db: AsyncSession) -> User | None:
    """只读车道：先查缓存，未命中回源并回填

    返回值可能是 transient 实例（来自缓存），也可能是 Session 里的受管对象（来自数据库）。
    **调用方必须假设它是前者**：只读字段，不要改、不要 delete、不要碰关系属性。
    """
    pk = _to_uuid(user_id)
    if pk is None:
        return None

    key = cache_key(pk)
    snapshot = await get_json(key)
    if snapshot is not None:
        cached = _reconstruct(snapshot)
        if cached is not None:
            return cached

    user = await user_crud.get_user_by_id(pk, db)
    if user is not None:
        # ensure_ascii=False 让中文用户名在 Redis 里是可读的（排错时少一次转义来回）
        await set_json(key, _serialize(user), settings.USER_CACHE_TTL_SECONDS)
    return user


async def load_for_write(user_id, db: AsyncSession) -> User | None:
    """写车道：**一定回源**，返回受管对象。改资料/改密/注销都走这里"""
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    return await user_crud.get_user_by_id(pk, db)


async def invalidate(user_id) -> int:
    """删键。写库成功之后调用（铁律 2）。删不掉也只是旧值活到 TTL 到期，不抛异常"""
    pk = _to_uuid(user_id)
    if pk is None:
        return 0
    return await delete(cache_key(pk))


__all__ = ["cache_key", "get_user_cached", "invalidate", "load_for_write"]
