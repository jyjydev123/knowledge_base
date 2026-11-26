"""知识库路由（M0 本体 5 条 + 共享授权 3 条 = 8 条）

约定：
    - **读写用的 kb_id 一律用 query 参数**，配合 require_kb_access 依赖做权限判定
      （只有 `DELETE /kb/{kb_id}` 用路径参数，它是个动作而不是查询）；
    - 授权三条 + 改设置 + 删库都是 owner 专属（mode="manage"）：write 授权不能改库设置，
      更不能往下授权，否则权限会人传人失控；
    - 越权口径沿用 kb_access：**连 read 都没有 → 404**（不暴露别人的库是否存在），
      **能读不能写 → 403**（msg 里说清需要什么权限）；
    - `/kb/list` 同时返回 owned + shared 并带 permission，移动端靠它决定按钮可用性。

删库的一条硬顺序（错了就永久丢文件）：
    列文件路径 → 删库记录（级联带走 documents/chunks/shares）→ 删磁盘文件 → 收空目录 → 清向量。
    路径只能从 documents.file_path 复算，记录一删就再也算不回来，所以**必须先查后删**。
"""

import uuid as uuid_lib
from typing import Annotated

from fastapi import APIRouter, Path, Query
from starlette.concurrency import run_in_threadpool

from app.api.deps import DB, CurrentUser, KBManager, KBReader
from app.core.exceptions import ConflictError, NotFoundError, ParamError
from app.db.crud import document_crud, kb_crud, user_crud
from app.schemas.common import ApiResponse, PageData, PageParams
from app.schemas.kb_schemas import KbCreate, KbDeleteOut, KbOut, KbShareCreate, KbShareOut, KbUpdate
from app.services import vector_index
from app.services.process_document import (
    SAFE_EXT,
    delete_document_file,
    remove_kb_directories_if_empty,
)

router = APIRouter(prefix="/kb", tags=["knowledge_bases"])

# scope 的三种取值直接对应 crud 的三条来源，不猜：写错就 400，别默默按 all 跑
LIST_SCOPES = ("owned", "shared", "all")


def _kb_to_out(kb, permission: str, is_owner: bool) -> KbOut:
    """手工组装 KbOut（不在异步路由里碰 kb.owner / kb.documents 关系）

    ⚠️ 名字刻意不叫 `_to_out`：本文件里已经有一个 `_to_out(share, ...)` 组装授权记录，
    两个同名函数后者会**静默覆盖**前者，症状是"建库接口报 AttributeError:
    'KnowledgeBase' object has no attribute 'kb_id'"（本项目实测踩过）。
    同一模块里的组装函数一律带上类型名：`_kb_to_out` / `_share_to_out`。

    为什么不用 response_model + from_attributes 直接扔 ORM 对象：
    碰一次关系属性就是 MissingGreenlet，而 500 的堆栈看起来像"数据库坏了"，
    排查成本远高于写十二行赋值。
    """
    return KbOut(
        kb_id=kb.id,
        name=kb.name,
        description=kb.description or "",
        visibility=kb.visibility,
        owner_id=kb.owner_id,
        is_owner=is_owner,
        permission=permission,
        document_count=kb.document_count or 0,
        chunk_count=kb.chunk_count or 0,
        version=kb.version or 1,
        last_indexed_at=kb.last_indexed_at,
        chunk_size=kb.chunk_size,
        chunk_overlap=kb.chunk_overlap,
        created_at=kb.created_at,
        updated_at=kb.updated_at,
    )


# ======================= M0：知识库本体 =======================


@router.post("", response_model=ApiResponse[KbOut], summary="创建知识库")
async def create_kb(
    current_user: CurrentUser,
    payload: KbCreate,
    db: DB,
):
    """
    显示需求：用户自己建知识库（之前只能靠脚本建库联调，前端所有页面都是空壳）。

    落地：
        1. owner 恒为**当前登录用户**，请求体里没有 user_id 这个口子（防越权建别人的库）；
        2. 同一用户下重名 → 409（uq(owner_id, name)，不同用户之间重名允许）；
        3. 新库 version=1、document_count=0，后续文档增删由 document_crud 自动 +1。
    """
    kb = await kb_crud.create_kb(
        current_user.id,
        payload.name,
        db,
        description=payload.description,
        visibility=str(payload.visibility),
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    if kb is None:
        raise ConflictError("已存在同名知识库，请换一个名字")
    return ApiResponse.ok(_kb_to_out(kb, "write", True), msg="创建成功")


@router.get("/list", response_model=ApiResponse[PageData[KbOut]], summary="知识库列表（自己建的 + 被授权的）")
async def list_kbs(
    current_user: CurrentUser,
    db: DB,
    scope: Annotated[str, Query(description="owned=我建的 / shared=别人给我的(含 public) / all=合并")] = "all",
    page: Annotated[int, Query(ge=1, description="页码，从1开始")] = 1,
    size: Annotated[int, Query(ge=1, le=100, description="每页条数，最大100")] = 20,
):
    """
    列举当前用户看得见的知识库，**每条带 permission**。

    为什么要带 permission：移动端要靠它决定「上传/删除/授权」按钮亮不亮。
    让前端自己拿 owner_id 和 user_id 比是错的 —— 那等于在客户端复刻一遍权限矩阵，
    矩阵一改前端就悄悄不一致。
    """
    if scope not in LIST_SCOPES:
        raise ParamError(f"scope 只能是 {'/'.join(LIST_SCOPES)}")

    params = PageParams(page=page, size=size)
    entries, total = await kb_crud.list_visible_kbs(
        current_user.id, db, scope=scope, offset=params.offset, limit=params.limit
    )
    data = PageData[KbOut](
        items=[_kb_to_out(item.kb, item.permission, item.is_owner) for item in entries],
        total=total,
        page=params.page,
        size=params.limit,
    )
    return ApiResponse.ok(data, msg="查询成功")


@router.get("/detail", response_model=ApiResponse[KbOut], summary="知识库详情")
async def kb_detail(access: KBReader):
    """
    看某个库的具体说明与统计。

    权限：read 即可（被授权 read 的人、public 库的任何人也能看）；
    完全没有 read 权限时依赖注入已经返回 404，不区分"不存在"和"没权限"。
    """
    return ApiResponse.ok(_kb_to_out(access.kb, str(access.permission), access.is_owner), msg="获取成功")


@router.post("/update", response_model=ApiResponse[KbOut], summary="修改知识库设置")
async def update_kb(
    access: KBManager,
    payload: KbUpdate,
    db: DB,
):
    """
    改名 / 改说明 / 改可见范围 / 改切块参数。**只有 owner 能改**（write 授权也不行）。

    两点要说清：
        1. 只改提交了的字段（未提交的保持原样），所以空 body 直接 422，不会"成功但没变"；
        2. 改 chunk_size/chunk_overlap **不会自动重切已有文档**，只对之后的解析生效。
           真要全库重切走 `POST /documents/reindex/{document_id}` 逐篇来，
           这里绝不后台偷偷重算 —— 一个改名的请求不该触发全库 embedding 费用。
    """
    kb = access.kb
    updated = await kb_crud.update_kb(
        kb,
        db,
        name=payload.name,
        description=payload.description,
        visibility=str(payload.visibility) if payload.visibility is not None else None,
        chunk_size=payload.chunk_size,
        chunk_overlap=payload.chunk_overlap,
    )
    if updated is None:
        raise ConflictError("改名失败：已存在同名知识库")
    return ApiResponse.ok(_kb_to_out(updated, "write", True), msg="修改成功")


# ======================= 共享授权（3 条，owner 专属） =======================




def _share_to_out(share, grantee_username: str, grantee_email: str) -> KbShareOut:
    """手工组装，避免在异步路由里碰 share.grantee 触发懒加载异常"""
    return KbShareOut(
        share_id=share.id,
        kb_id=share.kb_id,
        grantee_id=share.grantee_id,
        grantee_username=grantee_username,
        grantee_email=grantee_email,
        permission=share.permission,
        created_at=share.created_at,
    )


@router.post("/share", response_model=ApiResponse[KbShareOut], summary="授权他人访问知识库")
async def grant_kb_share(
    access: KBManager,
    payload: KbShareCreate,
    db: DB,
):
    """
    显示需求：让用户可以把自己的知识库共享给别人（只读 / 读写）。

    落地：
        1. 权限：只有 owner（require_kb_access("manage")）能授权，write 授权的人不行；
        2. 被授权人按 email 精确查找，找不到 404；给 owner 自己授权 400（无意义）；
        3. 同一 (kb, 用户) 再授权 = 更新权限，接口幂等，重复调用不会多出记录。
    """
    grantee = await user_crud.get_user_by_email(payload.email, db)
    if grantee is None:
        raise NotFoundError("用户不存在")
    if str(grantee.id) == str(access.kb.owner_id):
        raise ParamError("不能给自己授权，你已经是该知识库的创建者")

    share = await kb_crud.grant_share(
        access.kb.id, grantee.id, access.kb.owner_id, db, permission=str(payload.permission)
    )
    if share is None:
        raise ConflictError("授权失败，请稍后重试")

    return ApiResponse.ok(_share_to_out(share, grantee.username, grantee.email), msg="授权成功")


@router.delete("/share", response_model=ApiResponse[None], summary="取消授权")
async def revoke_kb_share(
    access: KBManager,
    db: DB,
    email: Annotated[str, Query(description="要取消授权的用户邮箱")],
):
    """
    取消某个用户对该知识库的授权。

    落地：
        1. 同样 owner only；
        2. 「没有这条授权」不当错误处理（幂等），msg 里说明即可，
           前端重复调用/并发取消都不会拿到 4xx；
        3. 只删授权记录，不动文档 —— 对方自己的库里上传的东西仍然归他。
    """
    grantee = await user_crud.get_user_by_email(email, db)
    if grantee is None:
        raise NotFoundError("用户不存在")

    removed = await kb_crud.revoke_share(access.kb.id, grantee.id, db)
    if not removed:
        return ApiResponse.ok(msg="该用户本来就没有这个知识库的授权")

    # 授权撤销后不需要动 version（版本只反映文档内容变化）
    return ApiResponse.ok(msg="已取消授权")


@router.get("/share/list", response_model=ApiResponse[list[KbShareOut]], summary="知识库授权名单")
async def list_kb_shares(access: KBManager, db: DB):
    """
    查看某知识库当前的授权名单（owner only）。

    实现提示：kb_crud.list_shares 用 selectinload 一次性把被授权人带出来，
    这样组装响应时不会触发异步懒加载（MissingGreenlet）。
    """
    shares = await kb_crud.list_shares(access.kb.id, db)
    items = [
        _share_to_out(share, share.grantee.username, share.grantee.email)
        for share in shares
        if share.grantee is not None  # 用户被硬删时 selectinload 拿不到，跳过而不是 500
    ]
    return ApiResponse.ok(items, msg="获取成功")


# ======================= 删库（放在共享授权三条**之后**，顺序有意义） =======================
#
# 为什么这条不跟在 update 后面：FastAPI 按**注册顺序**匹配路由，
# `DELETE /kb/{kb_id}` 如果写在 `DELETE /kb/share` 之前，取消授权的请求会先撞上动态段，
# 然后 "share" 被当 uuid 解析 → 422，接口整个坏掉（本项目实测踩过一次）。
# 新增任何 `DELETE /kb/xxx` 静态路由时，都必须排在它前面，或者干脆改成路径参数风格。
#
@router.delete("/{kb_id}", response_model=ApiResponse[KbDeleteOut], summary="删除知识库")
async def delete_kb(
    kb_id: Annotated[uuid_lib.UUID, Path(description="知识库ID")],
    access: KBManager,
    db: DB,
):
    """
    删除知识库：库没了，里面的文档、切块、授权、磁盘文件、向量一起没。

    落地顺序（**每一步都不能换**）：
        1. 先把该库所有文档的 (id, owner_id, file_path) 查出来 —— 记录删掉之后路径就复算不回来了；
        2. 硬删库记录：documents/chunks/kb_shares 由外键级联一起走；
        3. 逐个删磁盘文件（用 document_storage_path 的同一套规则复算，uploader 用文档的 owner_id，
           所以被授权人传到 data/share/ 下的那些也收得干净）；
        4. 目录空了才 rmdir（remove_kb_directories_if_empty 内部只删空目录，绝不 rmtree）；
        5. 清向量。向量车道不可用时这里只是没清成，**不影响删除结果**：
           向量表里的残留靠 chunk_id 回表找不到而自然失效（见 retrieval 的脏向量处理）。
    """
    kb = access.kb
    rows = await document_crud.list_kb_document_rows(kb.id, db)
    document_ids = [str(row[0]) for row in rows]

    if not await kb_crud.delete_kb(kb, db):
        raise ConflictError("删除失败，请稍后重试")

    # 3./4. 磁盘回收（同步 IO 一律进线程池；单个文件删不掉就记一笔，绝不为它回滚数据库）
    removed = missing = 0
    for document_id, uploader_id, file_path in rows:
        ext = (file_path or "").rsplit(".", 1)[-1].lower() if file_path else ""
        if not ext or not SAFE_EXT.match(ext):
            missing += 1
            continue
        try:
            gone = await run_in_threadpool(
                delete_document_file, str(kb.owner_id), str(uploader_id), str(kb.id), str(document_id), ext
            )
            removed += 1 if gone else 0
            missing += 0 if gone else 1
        except OSError:
            missing += 1
    await run_in_threadpool(remove_kb_directories_if_empty, str(kb.owner_id), str(kb.id))

    # 5. 向量清理（best-effort）
    vectors_removed = await vector_index.remove_document_vectors(document_ids) if document_ids else 0

    return ApiResponse.ok(
        KbDeleteOut(
            kb_id=kb.id,
            document_count=len(rows),
            files_removed=removed,
            files_missing=missing,
            vectors_removed=vectors_removed,
        ),
        msg="删除成功",
    )
