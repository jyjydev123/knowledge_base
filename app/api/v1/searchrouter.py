"""独立搜索接口（1 条：POST /search）

定位：前端的"纯搜索框"，只要资料不要作答。和问答里的检索**共用同一条实现**
（app/rag/retrieval.retrieve），所以搜索页能搜到的东西，问答一定也能拿到参考资料；
反过来搜不到就说明库里真没有。两条链路各写一份检索逻辑，早晚对不上账。

一条不能省的规矩：
    **搜索范围 = 服务端算出来的可读库 ∩ 客户端点名的库**，
    先算可读、后收窄。顺序反了（先按 kb_ids 查、再滤权限）就等于
    把"能不能读"交给了请求体 —— 越权是这类系统最常见的洞。
    点名的库里有任何一个读不到 → 404（和详情/下载同一口径：不区分"不存在"和"没权限"）。
"""

from typing import Any

from fastapi import APIRouter

from app.api.deps import DB, CurrentUser
from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.rag import retrieval
from app.schemas.common import ApiResponse
from app.schemas.search_schemas import SearchHit, SearchOut, SearchRequest
from app.services import kb_access

router = APIRouter(tags=["search"])

# SearchHit 的字段集，从这里取一次，避免 schema 加了字段这里忘了带
_HIT_FIELDS = set(SearchHit.model_fields)


@router.post("/search", response_model=ApiResponse[SearchOut], summary="全文/语义检索（不作答）")
async def search(
    payload: SearchRequest,
    current_user: CurrentUser,
    db: DB,
):
    """
    显示需求：前端要做搜索框时不必先跑一遍大模型。

    落地：
        1. 先取可读范围（自己的 + 被授权的 + public），再和点名的 kb_ids 取交集；
        2. 点名的库里有一个读不到 → 404，整次请求不执行（不做"能搜的照样搜"这种半成品，
           那会让前端以为搜到了全部，实际上是缺了一部分的空结果）；
        3. 车道/降级/候选数**原样透出去**（mode/lane/fallback/candidates），
           用户看到"零结果"时前端能分辨是没有资料、是没权限、还是 embedding 挂了；
        4. 零结果仍是 200：搜索没搜到不是接口失败。
    """
    accessible = await kb_access.accessible_kb_ids(current_user.id, db)
    allowed = {str(item) for item in accessible}

    if payload.kb_ids:
        wanted = [str(item) for item in payload.kb_ids]
        if any(item not in allowed for item in wanted):
            raise NotFoundError("知识库不存在或没有读取权限")
        scope: list[Any] = [item for item in payload.kb_ids if str(item) in allowed]
    else:
        scope = list(accessible)

    found = await retrieval.retrieve(
        payload.q,
        kb_ids=scope,
        db=db,
        top_k=payload.top_k or settings.CHAT_RETRIEVAL_TOP_K,
    )

    hits = [SearchHit(**{key: source.get(key) for key in _HIT_FIELDS}) for source in found.sources]
    return ApiResponse.ok(
        SearchOut(
            query=payload.q,
            mode=found.mode,
            lane=found.lane,
            fallback=found.fallback,
            candidates=found.candidate_total,
            scoped_kb_ids=scope,
            hits=hits,
        ),
        msg="搜索完成" if hits else "没有找到相关资料",
    )
