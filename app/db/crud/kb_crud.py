"""知识库与共享授权的 CRUD

只读写数据库、不抛 HTTP 异常；权限判定规则在 app/services/kb_access.py，
这样「谁能做什么」只有一处定义，路由和测试都复用同一套。
"""

import uuid as uuid_lib
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.models import KbPermission, KbShare, KbVisibility, KnowledgeBase


def _to_uuid(raw):
    if raw is None or isinstance(raw, uuid_lib.UUID):
        return raw
    try:
        return uuid_lib.UUID(str(raw))
    except (ValueError, TypeError):
        return None


async def get_kb(kb_id, db: AsyncSession) -> KnowledgeBase | None:
    """按 id 取知识库本体，**不带 owner 过滤**

    共享之后「不是我的库」也可能是我能读的库，所以这里只负责取，
    能不能访问交给 kb_access.resolve() 判。非法 id 返回 None。
    """
    pk = _to_uuid(kb_id)
    if pk is None:
        return None
    result = await db.execute(
        select(KnowledgeBase).where(KnowledgeBase.id == pk, KnowledgeBase.deleted_at.is_(None))
    )
    return result.scalar_one_or_none()


async def list_owned_kbs(owner_id, db: AsyncSession, offset: int = 0, limit: int = 20) -> tuple[list[KnowledgeBase], int]:
    """某用户自己创建的库（M0 的 /kb/list 再用，先备好）"""
    from sqlalchemy import func

    uid = _to_uuid(owner_id)
    if uid is None:
        return [], 0
    conditions = [KnowledgeBase.owner_id == uid, KnowledgeBase.deleted_at.is_(None)]
    total = (await db.execute(select(func.count()).select_from(KnowledgeBase).where(*conditions))).scalar_one()
    result = await db.execute(
        select(KnowledgeBase).where(*conditions).order_by(KnowledgeBase.updated_at.desc()).offset(offset).limit(limit)
    )
    return list(result.scalars().all()), int(total)


async def get_share(kb_id, grantee_id, db: AsyncSession) -> KbShare | None:
    pk, uid = _to_uuid(kb_id), _to_uuid(grantee_id)
    if pk is None or uid is None:
        return None
    result = await db.execute(
        select(KbShare).where(KbShare.kb_id == pk, KbShare.grantee_id == uid)
    )
    return result.scalar_one_or_none()


async def grant_share(
    kb_id, grantee_id, granted_by, db: AsyncSession, permission: str = KbPermission.READ
) -> KbShare | None:
    """授权（幂等）：已有记录就更新权限，没有就插入

    返回 None 表示失败（id 非法 / 被授权用户或库不存在触发外键 / 唯一索引冲突且更新失败）。
    """
    pk, uid, giver = _to_uuid(kb_id), _to_uuid(grantee_id), _to_uuid(granted_by)
    if pk is None or uid is None:
        return None

    existing = await get_share(pk, uid, db)
    if existing is not None:
        existing.permission = permission
        await db.commit()
        await db.refresh(existing)
        return existing

    record = KbShare(kb_id=pk, grantee_id=uid, granted_by=giver, permission=permission)
    db.add(record)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return await get_share(pk, uid, db)
    await db.refresh(record)
    return record


async def revoke_share(kb_id, grantee_id, db: AsyncSession) -> bool:
    pk, uid = _to_uuid(kb_id), _to_uuid(grantee_id)
    if pk is None or uid is None:
        return False
    result = await db.execute(select(KbShare).where(KbShare.kb_id == pk, KbShare.grantee_id == uid))
    record = result.scalar_one_or_none()
    if record is None:
        return False
    await db.delete(record)
    await db.commit()
    return True


async def list_shares(kb_id, db: AsyncSession) -> list[KbShare]:
    """某库的授权名单（带被授权人信息，用 selectinload 避免异步下懒加载炸）"""
    pk = _to_uuid(kb_id)
    if pk is None:
        return []
    result = await db.execute(
        select(KbShare)
        .where(KbShare.kb_id == pk)
        .options(selectinload(KbShare.grantee))
        .order_by(KbShare.created_at.desc())
    )
    return list(result.scalars().all())


async def list_shared_kb_ids(grantee_id, db: AsyncSession) -> list[uuid_lib.UUID]:
    """被授权给这个用户的知识库 id（不含 public 库，也不含自己的库 —— 由调用方合并）"""
    uid = _to_uuid(grantee_id)
    if uid is None:
        return []
    result = await db.execute(
        select(KbShare.kb_id)
        .join(KnowledgeBase, KnowledgeBase.id == KbShare.kb_id)
        .where(KbShare.grantee_id == uid, KnowledgeBase.deleted_at.is_(None))
        .order_by(KbShare.created_at.desc())
    )
    return list(result.scalars().all())


async def list_public_kb_ids(db: AsyncSession) -> list[uuid_lib.UUID]:
    """全站可见（public）的库 id"""
    result = await db.execute(
        select(KnowledgeBase.id).where(
            KnowledgeBase.visibility == "public", KnowledgeBase.deleted_at.is_(None)
        )
    )
    return list(result.scalars().all())


# ======================= M0：知识库本身的增删改查 =======================

# 一次最多扫多少个库再在内存里分页。为什么不三条 SQL 各自分页后拼接？
#   因为「可见范围」= 我建的 + 被授权的 + public 的，三条来源**必须先去重再排序**，
#   在 SQL 层分页会得到三页拼起来的错页。一人几百个库是这类系统的上限，
#   真超了这个量再改成一条 UNION ALL（代价是多一处难读的 SQL，先不换）。
LIST_SCAN_LIMIT = 500

# 列表的"最近活跃"口径 = 有更新时间用更新时间，没有就用创建时间（新建的库 updated_at 为 NULL）
_KB_RECENCY = func.coalesce(KnowledgeBase.updated_at, KnowledgeBase.created_at)


def _recency(kb: KnowledgeBase):
    """给 Python 端排序用的同一个口径。留空 datetime 兜底，绝不让 sorted 撞上 None 比较"""
    from datetime import UTC, datetime

    return kb.updated_at or kb.created_at or datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True)
class KbEntry:
    """列表里的一行：库 + 当前用户对它的权限 + 是不是自己建的

    owner 的 permission 直接给 WRITE（owner 隐式拥有全部权限），这样前端只看一个字段
    就能决定按钮可用性，不用再去猜 is_owner；is_owner 仍然留着，删库/授权这类
    owner 专属操作要靠它（矩阵在 app/services/kb_access.py）。
    """

    kb: KnowledgeBase
    permission: str
    is_owner: bool


async def create_kb(
    owner_id, name: str, db: AsyncSession, *, description: str = "",
    visibility: str = KbVisibility.PRIVATE, chunk_size: int = 800, chunk_overlap: int = 120,
) -> KnowledgeBase | None:
    """建库。返回 None 表示撞了 uq(owner_id, name)（同名库已存在），由路由转 409

    version 从 1 开始（模型默认值），语义是「库的内容变过几轮」；
    document_crud.bump_kb_version() 在文档增删时 +1，前端拿它判断缓存要不要失效。
    """
    uid = _to_uuid(owner_id)
    if uid is None:
        return None

    kb = KnowledgeBase(
        owner_id=uid,
        name=name,
        description=description or "",
        visibility=visibility,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
    db.add(kb)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(kb)
    return kb


async def update_kb(
    kb: KnowledgeBase, db: AsyncSession, *, name: str | None = None, description: str | None = None,
    visibility: str | None = None, chunk_size: int | None = None, chunk_overlap: int | None = None,
) -> KnowledgeBase | None:
    """改库设置（只有显式传值的字段才改，None 表示"没提交这个字段"）。返回 None = 改名撞了同主人的另一个库

    ⚠️ **改 chunk_size / chunk_overlap 不会自动重切已有文档**：切块参数只对之后的解析生效。
    真要全库重切得逐篇重跑 process_document（成本高，路由层绝不偷偷做，只在 docstring 里写明）。
    """
    if name is not None:
        kb.name = name
    if description is not None:
        kb.description = description
    if visibility is not None:
        kb.visibility = visibility
    if chunk_size is not None:
        kb.chunk_size = chunk_size
    if chunk_overlap is not None:
        kb.chunk_overlap = chunk_overlap

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    await db.refresh(kb)
    return kb


async def delete_kb(kb: KnowledgeBase, db: AsyncSession) -> bool:
    """删库（**硬删**）：documents / chunks / kb_shares 由外键级联一起走

    为什么不是软删（别的表都有 deleted_at）：
      1. uq(owner_id, name) 不含 deleted_at，软删之后同名库永远建不起来，
         用户视角就是「删了但名字还被占着」；
      2. 与文档删除口径一致（document_crud.delete_document 也是硬删 + 磁盘回收）；
      3. 磁盘文件必须由调用方**在删记录之前**先把路径查出来 —— 记录一没，路径就再也算不回来了。
         所以路由的顺序固定为：列文件 → delete_kb → 删文件 → 收空目录，绝不能反过来。
    """
    try:
        await db.delete(kb)
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def list_visible_kbs(
    user_id, db: AsyncSession, *, scope: str = "all", offset: int = 0, limit: int = 20
) -> tuple[list[KbEntry], int]:
    """当前用户看得见的库：owned / shared(+public) / all，每条带 permission

    scope 语义（前端两个 tab 直接对应）：
      owned  = 我建的
      shared = 别人给我的（含被设成 public 的库，按只读算）
      all    = 两者合并，按 updated_at 倒序
    """
    uid = _to_uuid(user_id)
    if uid is None:
        return [], 0

    entries: dict[str, KbEntry] = {}

    if scope in ("owned", "all"):
        rows = (
            await db.execute(
                select(KnowledgeBase)
                .where(KnowledgeBase.owner_id == uid, KnowledgeBase.deleted_at.is_(None))
                .order_by(_KB_RECENCY.desc())
                .limit(LIST_SCAN_LIMIT)
            )
        ).scalars().all()
        for kb in rows:
            entries[str(kb.id)] = KbEntry(kb=kb, permission=KbPermission.WRITE, is_owner=True)

    if scope in ("shared", "all"):
        # 被逐条授权的库：permission 以 kb_shares 为准（read / write）
        shared = (
            await db.execute(
                select(KnowledgeBase, KbShare.permission)
                .join(KbShare, KbShare.kb_id == KnowledgeBase.id)
                .where(KbShare.grantee_id == uid, KnowledgeBase.deleted_at.is_(None))
                .order_by(_KB_RECENCY.desc(), KbShare.created_at.desc())
                .limit(LIST_SCAN_LIMIT)
            )
        ).all()
        for kb, permission in shared:
            entries.setdefault(str(kb.id), KbEntry(kb=kb, permission=str(permission), is_owner=False))

        # public 库 = 全站只读。已经被逐条授权过的不覆盖（write 高于 public 的 read）
        public = (
            await db.execute(
                select(KnowledgeBase)
                .where(
                    KnowledgeBase.visibility == KbVisibility.PUBLIC,
                    KnowledgeBase.owner_id != uid,
                    KnowledgeBase.deleted_at.is_(None),
                )
                .order_by(_KB_RECENCY.desc())
                .limit(LIST_SCAN_LIMIT)
            )
        ).scalars().all()
        for kb in public:
            entries.setdefault(str(kb.id), KbEntry(kb=kb, permission=KbPermission.READ, is_owner=False))

    # 排序键用 recency = updated_at or created_at：新建还没动过的库 updated_at 是 NULL，
    # 直接拿它排序会 TypeError（None 和 datetime 比不了），带 id 兜底保证同刻稳定。
    ordered = sorted(
        entries.values(),
        key=lambda item: (_recency(item.kb), str(item.kb.id)),
        reverse=True,
    )
    return ordered[offset : offset + limit], len(ordered)


async def count_owner_documents(kb_id, db: AsyncSession) -> int:
    """某个库还剩多少条文档记录（删库前收磁盘用，别把 SQL 写进路由）"""
    from app.db.models import Document

    pk = _to_uuid(kb_id)
    if pk is None:
        return 0
    result = await db.execute(select(func.count()).select_from(Document).where(Document.kb_id == pk))
    return int(result.scalar_one())
