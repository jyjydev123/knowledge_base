"""password_reset_codes 表的 CRUD（业务编排在 app/services/password_reset.py，路由不直接碰表）

**为什么这里用 bcrypt 而不是 sha256**（和 token_crud 不同口径，别照着抄）：
    验证码只有 6 位数字 = 10^6 种可能。sha256 是无盐快哈希，拿到库的人
    把一百万个候选哈希一遍只要几十毫秒 —— 等于没存。所以短码必须用
    带盐的慢哈希（复用 app/core/security.py 的 bcrypt），并配合 attempts 上限。
    代价：**不能按哈希查行**（同一个码每次哈希结果都不同），只能先按 user_id
    把那条待用的码取出来，再 verify。这也是本文件没有 get_by_raw_code() 的原因。

所有时间判断都收在 `_as_utc()` 里做一次 —— PG 存 timestamptz、SQLite 存的时候会丢掉时区
尾缀，各处直接比 datetime 会让"过期"在两种库上行为不一致，而那恰好是安全属性。
"""

from __future__ import annotations

import secrets
import uuid as uuid_lib
from datetime import datetime, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash, verify_password
from app.db.models import PasswordResetCode

CODE_LENGTH = 6               # 6 位数字，手机上好输（RESET_CODE_LENGTH 可改，但别低于 6）
CODE_HASH_MAX_LENGTH = 72     # 与 models 里 String(72) 对齐（bcrypt 串 60 字符）


def new_raw_code(length: int = CODE_LENGTH) -> str:
    """生成给用户看的验证码。**secrets 不是 random**：random 是可预测的（同一进程内种子可推）"""
    digits = max(int(length), 4)      # 下限 4 位，防配置写 0 退化成"永远猜得中"
    upper = 10**digits
    return str(secrets.randbelow(upper)).zfill(digits)


def hash_code(raw_code: str) -> str:
    """短码 → bcrypt。返回串每次不同（内置随机盐），所以本表**只能按 user 查再比对**"""
    return get_password_hash(raw_code)


def check_code(raw_code: str, hashed: str) -> bool:
    """定长比较交给 passlib/bcrypt，它内部就是恒定时间的比较，别自己写 =="""
    if not raw_code or not hashed:
        return False
    return verify_password(raw_code, hashed)


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


def _as_utc(value: datetime | None) -> datetime | None:
    """把库里读出来的时间统一成"带时区的 UTC"，无时区的按 UTC 解释（SQLite 就是这种）"""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def create_code(
    user_id,
    raw_code: str,
    expires_at: datetime,
    db: AsyncSession,
    *,
    channel: str | None = None,
    client_ip: str | None = None,
) -> PasswordResetCode | None:
    """落一条待用的验证码。user_id 非法/码为空返回 None，由调用方兜（不抛，免得匿名接口 500）"""
    pk = _to_uuid(user_id)
    if pk is None or not raw_code:
        return None

    record = PasswordResetCode(
        code_hash=hash_code(raw_code),
        user_id=pk,
        expires_at=expires_at,
        attempts=0,
        channel=str(channel)[:16] if channel else None,
        client_ip=client_ip[:45] if client_ip else None,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return record


async def latest_active_for_user(user_id, db: AsyncSession) -> PasswordResetCode | None:
    """该用户**最近一条还没用掉且没过期**的码（校验车道就认这一条）

    为什么不取"最近一条"再判可用性：那样重新申请之后，旧码在新码过期的时间窗里
    会被当成"当前码"——用户手里同时攥着两条码时，先申请的那条应该立刻失效。
    """
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    result = await db.execute(
        select(PasswordResetCode)
        .where(
            PasswordResetCode.user_id == pk,
            PasswordResetCode.used_at.is_(None),
            PasswordResetCode.expires_at > utc_now(),
        )
        .order_by(PasswordResetCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def latest_for_user(user_id, db: AsyncSession) -> PasswordResetCode | None:
    """该用户最近一次申请（**冷却判定要的是"上一条多久之前发的"**，含已用/已过期的）"""
    pk = _to_uuid(user_id)
    if pk is None:
        return None
    result = await db.execute(
        select(PasswordResetCode)
        .where(PasswordResetCode.user_id == pk)
        .order_by(PasswordResetCode.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def is_usable(record: PasswordResetCode | None) -> bool:
    """没用过、没过期、且还没被尝试次数烧掉，才算能用"""
    if record is None or record.used_at is not None:
        return False
    expires_at = _as_utc(record.expires_at)
    return bool(expires_at and expires_at > utc_now())


async def mark_used(record: PasswordResetCode, db: AsyncSession) -> bool:
    """作废（正常用完 / 尝试次数超上限都走这里，语义统一成"这条没了"）"""
    record.used_at = utc_now()
    await db.commit()
    return True


async def register_attempt(record: PasswordResetCode, db: AsyncSession, max_attempts: int) -> bool:
    """错误一次就 +1；到达上限顺手作废。返回"这条现在还能不能用"

    没有 attempts 的话，6 位数字 = 一百万种，匿名接口可以无限试 —— 那才是真实风险，
     bcrypt 挡的是"库被人拖走"，attempts 挡的是"在线爆破"，两条管的是不同的事。
    """
    record.attempts = int(record.attempts or 0) + 1
    if max_attempts > 0 and record.attempts >= int(max_attempts):
        record.used_at = utc_now()
    await db.commit()
    return record.used_at is None


async def invalidate_user_codes(user_id, db: AsyncSession) -> int:
    """作废该用户所有未使用的码（重新申请时先清旧的、以及改密成功之后）

    为什么必须做：用户手里可能同时有旧码和新码；不清旧码就等于把有效期
    拉长成"最早那次申请的过期时间"，也等于把攻击面翻倍。
    """
    pk = _to_uuid(user_id)
    if pk is None:
        return 0
    result = await db.execute(
        update(PasswordResetCode)
        .where(PasswordResetCode.user_id == pk, PasswordResetCode.used_at.is_(None))
        .values(used_at=utc_now())
    )
    await db.commit()
    return int(result.rowcount or 0)


async def purge_expired(db: AsyncSession) -> int:
    """清掉过期记录（申请量大的时候挂定时任务；现在给 preflight/手工清理用）"""
    result = await db.execute(delete(PasswordResetCode).where(PasswordResetCode.expires_at < utc_now()))
    await db.commit()
    return int(result.rowcount or 0)


async def list_for_user(user_id, db: AsyncSession, limit: int = 10) -> list[PasswordResetCode]:
    """排障用：这个账号最近申请过几次、分别什么时候（**不返回哈希**，别把它当凭证贴出去）"""
    pk = _to_uuid(user_id)
    if pk is None:
        return []
    result = await db.execute(
        select(PasswordResetCode)
        .where(PasswordResetCode.user_id == pk)
        .order_by(PasswordResetCode.created_at.desc())
        .limit(max(int(limit), 1))
    )
    return list(result.scalars().all())


__all__ = [
    "CODE_HASH_MAX_LENGTH",
    "CODE_LENGTH",
    "check_code",
    "create_code",
    "hash_code",
    "invalidate_user_codes",
    "is_usable",
    "latest_active_for_user",
    "latest_for_user",
    "list_for_user",
    "mark_used",
    "new_raw_code",
    "purge_expired",
    "register_attempt",
    "utc_now",
]
