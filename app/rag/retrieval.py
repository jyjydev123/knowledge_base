"""问答的检索层：两条车道 + 一条混合车道，返回形状完全一致

  keyword 车道 = DB 里 ILIKE 取候选 + 词频算分（不联网、不依赖 pgvector，永远可用）
  vector  车道 = pgvector 语义召回 chunk_id → **回 chunks 表取正文**
  hybrid  车道 = 两路都跑，按 chunk_id 去重合并（向量在前，关键词补漏）

车道选择见 settings.RETRIEVAL_LANE（默认 keyword）。为什么默认还是关键词：
  填上 embedding key 就自动改变线上问答行为是不负责任的默认值；切车道必须是显式决定。
  但向量化入库（app/services/vector_index.py）已经按**阿里云百炼 text-embedding-v3 / 1024 维**
  接通，维度在 config 里启动时校验（一旦建过 collection 就不能改）。

降级是**显式**的，这是本文件最容易写坏的地方：
  向量车道不可用（没配 key / 连的是 SQLite / 远端 401 或超时 / collection 还没建）时，
  自动退回关键词车道，并把原因原样放进 RetrievedContext.fallback、mode 如实标成 keyword。
  不 500、不静默：前端 meta 事件里看得到为什么这次是关键词。
  反过来，向量车道**正常跑了但没命中**不算故障，mode 直接是 none —— 不偷偷换车道，
  否则你永远分不清"资料库里确实没有"和"embedding 挂了"。

三条硬规矩：
  1. **必须有范围**。kb_ids 由调用方给出，且必须是「这个用户读得到的库」
     （app/services/kb_access.accessible_kb_ids 或会话自己限定的那个库）。
     这里不接受"kb_ids 为空=全库检索"这种默认：越权是这类系统最常见的洞，
     默认值必须是"查不到"而不是"全给你"。
  2. **进 prompt 的资料长度有上限**（CHAT_RETRIEVAL_MAX_CHARS）。一个 8k 字的块
     会把短期窗口整个吃掉，模型后面真正的问题反而看不见。
  3. **每块都带出处**（document_id / chunk_id / page / heading），
     前端的引用角标和"点回原文"全靠它；出处对不上的检索结果等于没有出处。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.crud import chunk_crud

logger = logging.getLogger("app.rag.retrieval")

# 英文/数字词：>=2 个字符才有意义（"a"、"I" 这种单字母命中率高得没边，纯噪声）
_ASCII_TERM = re.compile(r"[A-Za-z0-9_]{2,}")
# 中日韩连续段：没有分词器，所以按「整段 + 2-gram」造候选词
_CJK_RUN = re.compile(r"[一-鿿㐀-䶿]+")

MAX_QUERY_TERMS = 12          # 送给 SQL 的词数（与 chunk_crud.search_chunks 的上限一致）
HITS_PER_DOCUMENT = 2         # 同一篇文档最多贡献几块，避免一篇长文霸占整个参考资料
SCORE_REPEAT_CAP = 5          # 同一个词出现 5 次以上不再加分，防止长块靠体量刷分
VECTOR_CANDIDATE_MULTIPLIER = 2   # 向量多召回几倍候选：每篇限流 + 回表掉行之后还够凑满 top_k
LANCES = ("keyword", "vector", "hybrid")


@dataclass(slots=True)
class RetrievedContext:
    """一次检索的结果。text 进 prompt，sources 给前端"""

    text: str = ""
    sources: list[dict] = field(default_factory=list)
    mode: str = "none"          # 实际生效的车道：keyword/vector/hybrid/none
                                # 或者前置拦截：disabled / no_scope / no_terms
    lane: str = "keyword"       # 本次**请求**的车道（配置里那个），mode 是"真正起作用的那个"
    terms: list[str] = field(default_factory=list)
    candidate_total: int = 0    # DB/向量库里命中的候选块数（截断前）
    fallback: str = ""          # 非空 = 向量车道不可用已退回关键词，这里带原因；空 = 没降级

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "lane": self.lane,
            "terms": list(self.terms),
            "candidates": self.candidate_total,
            "hits": len(self.sources),
            "chars": len(self.text),
            "fallback": self.fallback,
        }


def extract_terms(question: str, *, max_terms: int = MAX_QUERY_TERMS) -> list[str]:
    """从问题里造检索词（纯函数，可单测）

    中文按 2-gram 展开是有意的：没有分词器时，"报销流程怎么走" 整串 ILIKE 几乎必然 0 命中，
    而 "报销"/"销流"/"流程" 能命中。噪声词（2-gram 里的"销流"）不致命——
    它只影响候选集大小，真正的取舍在 score 里按命中数与词长决定。
    """
    text = (question or "").strip()
    if not text:
        return []

    terms: list[str] = []
    seen: set[str] = set()

    def _push(word: str) -> None:
        token = word.strip().lower()
        if len(token) >= 2 and token not in seen and len(terms) < max_terms:
            seen.add(token)
            terms.append(token)

    for match in _ASCII_TERM.findall(text):
        _push(match)

    for run in _CJK_RUN.findall(text):
        if len(run) <= 4:
            _push(run)
            continue
        _push(run[:6])  # 长串的开头往往就是主题词
        for index in range(len(run) - 1):
            _push(run[index:index + 2])
            if len(terms) >= max_terms:
                break

    # 词长的排在前面：SQL 里只取前 MAX_QUERY_TERMS 个，长的更有信息量
    terms.sort(key=lambda item: (-len(item), item))
    return terms


def score_content(content: str, terms: list[str], *, heading: str = "", title: str = "") -> float:
    """词频算分：命中次数 × 词长权重，标题/小标题命中额外加权

    为什么不用真余弦：那是 embedding 的活。这里要的是"资料里有没有这几个字"，
    一个便宜、确定、能单测的启发式比半吊子的 TF-IDF 更好维护。
    """
    body = (content or "").lower()
    meta = f"{heading or ''} {title or ''}".lower()
    score = 0.0
    for term in terms:
        hits = body.count(term)
        if hits:
            score += (1.0 + len(term)) * min(hits, SCORE_REPEAT_CAP)
        if term in meta:
            score += 2.0 * (1.0 + len(term))
    return score


def rank_hits(hits: list[dict], terms: list[str], *, top_k: int) -> list[dict]:
    """排序 + 每篇文档限流 + 截断（纯函数）"""
    scored = []
    for index, hit in enumerate(hits):
        value = score_content(
            hit.get("content") or "",
            terms,
            heading=hit.get("heading") or "",
            title=hit.get("document_title") or hit.get("document_filename") or "",
        )
        scored.append((value, index, hit))  # index 兜底：同分时保持 DB 返回顺序，结果可复现
    scored.sort(key=lambda item: (-item[0], item[1]))

    picked: list[dict] = []
    per_document: dict[str, int] = {}
    for value, _index, hit in scored:
        if value <= 0:
            continue  # 一个词都没命中（只有标题命中过之类）就别进 prompt
        doc_key = str(hit.get("document_id"))
        if per_document.get(doc_key, 0) >= HITS_PER_DOCUMENT:
            continue
        per_document[doc_key] = per_document.get(doc_key, 0) + 1
        enriched = dict(hit)
        enriched["score"] = round(value, 3)
        picked.append(enriched)
        if len(picked) >= top_k:
            break
    return picked


def format_references(picked: list[dict], *, max_chars: int, snippet_chars: int) -> tuple[str, list[dict]]:
    """拼【参考资料】正文 + 引用列表。超预算从后面往前丢，但至少留一块

    至少要留一块是有意为之：全丢了 mode 还是 keyword 却什么资料都没有，
    模型会直接回答"资料里找不到"，用户以为检索坏了。
    """
    blocks: list[str] = []
    sources: list[dict] = []
    used = 0

    for index, hit in enumerate(picked, 1):
        content = str(hit.get("content") or "").strip()
        snippet = content[:snippet_chars]
        if len(content) > snippet_chars:
            snippet += "…"
        located = [f"[{index}]《{hit.get('document_title') or hit.get('document_filename') or '文档'}》"]
        if hit.get("page"):
            located.append(f"第{hit['page']}页")
        if hit.get("heading"):
            located.append(str(hit["heading"]))
        block = " · ".join(located) + "\n" + snippet

        if blocks and used + len(block) > max_chars:
            break  # 预算用完：后面更好的块没有了，但至少前面的还在
        blocks.append(block)
        used += len(block)
        sources.append(
            {
                "index": index,
                "document_id": str(hit.get("document_id")),
                "chunk_id": str(hit.get("chunk_id")),
                "kb_id": str(hit.get("kb_id")),
                "chunk_seq": int(hit.get("seq") or 0),
                "page": hit.get("page"),
                "heading": hit.get("heading"),
                "title": hit.get("document_title") or hit.get("document_filename") or "",
                "score": hit.get("score", 0),
                "lane": hit.get("lane") or "",   # keyword / vector / both：出处之外再带一个"怎么找到的"
                "snippet": snippet,
            }
        )
    return "\n\n".join(blocks), sources


def vector_lane_block_reason() -> str:
    """向量车道现在**不能**跑的原因；空串表示可以跑

    单独抽出来是为了两处复用（线上检索 + CLI --check）口径一致，
    也方便测试里断言"没配 key 时的降级原因写得人话"。
    """
    if settings.DATABASE_URL.startswith("sqlite"):
        # 先报这个：换 key 也救不了连错库，测试车道里那些"没配 key"的用例就是被这条挡住的
        return "当前 DATABASE_URL 是 SQLite，pgvector 车道需要 PostgreSQL"
    if not settings.embedding_ready:
        return "未配置 EMBEDDING_API_KEY（embedding 走阿里云百炼，填 key 即可，端点已有默认值）"
    return ""


def vector_filter(kb_ids: list[Any]) -> dict[str, Any]:
    """构造 pgvector 的 metadata 过滤条件

    **只按 kb_id 过滤，绝不按 user_id 过滤** —— 这条最容易写错而且错了不报异常：
    metadata 里的 user_id 是「文档的 owner，也就是库主人」（见 vector_index.chunk_metadata），
    而提问者可能是被分享的人。按当前用户 id 过滤的后果是"共享库/public 库里的文档
    列得出来、搜得到标题，但永远检索不到内容"，比越权更难查。
    越权的正确拦法有两层：调用方给的 kb_ids 必须是 accessible_kb_ids 的结果，
    以及命中后回表时再按 kb_ids 校验一次（chunk_crud.list_chunks_by_ids 里做了）。
    """
    return {"kb_id": {"$in": [str(item) for item in kb_ids]}}


async def search_vectors(
    question: str,
    kb_ids: list[Any],
    db: AsyncSession,
    *,
    limit: int | None = None,
) -> tuple[list[dict], str]:
    """语义召回：pgvector 出 chunk_id + 相似度 → 回 chunks 表取正文

    返回 (hits, 降级原因)。**原因非空时 hits 必为空**，调用方据此退回关键词车道。
    不抛异常：向量车道挂在问答主链路上，embedding 欠费/超时不该让整个对话 500。

    为什么要回表而不是直接用向量库里的 page_content：正文只有一个真源，
    且软删的文档、重新切块的旧向量必须以业务表为准（细节写在 chunk_crud 的注释里）。
    返回的 hits 按相似度降序，带 vector_score / 与 search_chunks 同形状的字段。
    """
    reason = vector_lane_block_reason()
    if reason:
        return [], reason

    want = max(int(limit if limit is not None else settings.CHAT_RETRIEVAL_TOP_K * VECTOR_CANDIDATE_MULTIPLIER), 1)
    try:
        from app.rag.memory.save import get_document_vectorstore

        store = await asyncio.to_thread(get_document_vectorstore)
    except Exception as exc:  # noqa: BLE001 —— 三方库建表/连库的意外失败不能带崩问答
        logger.warning("向量库初始化失败，退回关键词车道: %s", str(exc)[:200])
        return [], f"向量库初始化失败：{type(exc).__name__}: {str(exc)[:160]}"

    try:
        pairs = await asyncio.to_thread(
            store.similarity_search_with_relevance_scores,
            question,
            k=want,
            filter=vector_filter(kb_ids),
        )
    except Exception as exc:  # noqa: BLE001 —— 欠费 401、批量超限 400、超时都从这里退
        logger.warning("向量检索失败，退回关键词车道: %s", str(exc)[:200])
        return [], f"向量检索失败：{type(exc).__name__}: {str(exc)[:160]}"

    scored: dict[str, float] = {}
    minimum = float(settings.RETRIEVAL_MIN_SCORE or 0.0)
    for document, score in pairs or []:
        meta = document.metadata or {}
        chunk_id = str(meta.get("chunk_id") or document.id or "").strip()
        if not chunk_id:
            continue
        value = float(score) if score is not None else 0.0
        if minimum > 0.0 and value < minimum:
            continue
        scored[chunk_id] = max(scored.get(chunk_id, 0.0), value)

    if not scored:
        return [], ""

    rows = await chunk_crud.list_chunks_by_ids(list(scored.keys()), kb_ids, db)
    hits: list[dict] = [dict(row) for row in rows]
    for hit in hits:
        hit["vector_score"] = round(scored.get(str(hit.get("chunk_id")), 0.0), 4)
    # 回表回来的顺序是数据库给的（SQLite 这里甚至按主键字符串排），所以**必须自己按分数排**。
    # 忘了这一步的症状是"最相关的参考资料排在最后被预算丢掉"，很难查。
    hits.sort(key=lambda item: (-float(item.get("vector_score") or 0.0), str(item.get("chunk_id"))))
    # 回表少掉的行是**好事**：块被删了、文档软删了、或者那条向量不属于这批 kb。
    # 脏向量不该出现在 prompt 里，这里静默丢掉就行（清理由 vector_index 按 document_id 负责）。
    return hits, ""


def rank_vector_hits(hits: list[dict], *, top_k: int) -> list[dict]:
    """按相似度排序 + 每篇文档限流（与关键词车道的规矩一致）

    同分时按 document_id + seq 排：让结果可复现，不然同一个问题两次跑给出不同引用，
    前端角标会跳。score 字段统一成 0~1 的相似度（COSINE  relevance 已由 langchain 归一）。
    """
    ordered = sorted(
        hits,
        key=lambda item: (
            -float(item.get("vector_score") or 0.0),
            str(item.get("document_id")),
            int(item.get("seq") or 0),
        ),
    )
    picked: list[dict] = []
    per_document: dict[str, int] = {}
    for hit in ordered:
        doc_key = str(hit.get("document_id"))
        if per_document.get(doc_key, 0) >= HITS_PER_DOCUMENT:
            continue
        per_document[doc_key] = per_document.get(doc_key, 0) + 1
        enriched = dict(hit)
        enriched["score"] = round(float(hit.get("vector_score") or 0.0), 4)
        enriched["lane"] = "vector"
        picked.append(enriched)
        if len(picked) >= top_k:
            break
    return picked


def merge_lane_hits(vector_picked: list[dict], keyword_picked: list[dict], *, top_k: int) -> list[dict]:
    """合并两路结果：按 chunk_id 去重（向量在前）、两路都命中的标 both、重新限流

    为什么不加权求和成一个分数：关键词分是词频（无界）、向量分是余弦（0~1），
    两个不同量纲的东西相加出来的分数没法解释，调权重成了玄学。
    "向量优先 + 关键词补漏"是能被说清楚的策略，也更便宜（少一次 embedding 调用就少一次风险）。
    """
    merged: dict[str, dict] = {}
    order: list[str] = []
    for lane, rows in (("vector", vector_picked), ("keyword", keyword_picked)):
        for hit in rows:
            key = str(hit.get("chunk_id"))
            if not key or key == "None":
                continue
            if key not in merged:
                enriched = dict(hit)
                enriched["lane"] = lane
                merged[key] = enriched
                order.append(key)
            elif merged[key].get("lane") != lane:
                merged[key]["lane"] = "both"

    picked: list[dict] = []
    per_document: dict[str, int] = {}
    for key in order:
        hit = merged[key]
        doc_key = str(hit.get("document_id"))
        if per_document.get(doc_key, 0) >= HITS_PER_DOCUMENT:
            continue
        per_document[doc_key] = per_document.get(doc_key, 0) + 1
        picked.append(hit)
        if len(picked) >= top_k:
            break
    return picked


def effective_mode(picked: list[dict]) -> str:
    """从真正进 prompt 的块反推车道 —— mode 说的是"资料是哪来的"，不是"配置写了什么" """
    if not picked:
        return "none"
    lanes = {str(hit.get("lane") or "") for hit in picked}
    if lanes == {"vector"}:
        return "vector"
    if lanes == {"keyword"}:
        return "keyword"
    return "hybrid"


async def retrieve(
    question: str,
    *,
    kb_ids: list[Any] | None,
    db: AsyncSession,
    top_k: int | None = None,
    lane: str | None = None,
) -> RetrievedContext:
    """按可读库范围检索参考资料，走哪条车道由 settings.RETRIEVAL_LANE（或参数 lane）决定。

    返回永远是 RetrievedContext（可能是空的 text + mode 说明为什么空），**不抛异常**：
    检索没命中是常态，不该让一次问答失败；embedding 挂了是降级（fallback 字段），
    真出问题是 DB 挂了，那由上层的事务处理。
    """
    limit = settings.CHAT_RETRIEVAL_TOP_K if top_k is None else int(top_k)
    # None 和空白都要丢：kb_ids 来自请求/会话配置，混进空串是常态，
    # 留着空串会让"范围其实为空"变成"带着空条件去查向量库"。
    scope = [item for item in (kb_ids or []) if item is not None and str(item).strip()]
    wanted = (lane or settings.RETRIEVAL_LANE)
    wanted = wanted if wanted in LANCES else "keyword"

    if not settings.CHAT_RETRIEVAL_ENABLED:
        return RetrievedContext(mode="disabled", lane=wanted)
    if not scope:
        # 没有范围就是没有资料。"全库检索"这个默认值永远不该存在
        return RetrievedContext(mode="no_scope", lane=wanted)

    terms = extract_terms(question)
    fallback = ""

    vector_rows: list[dict] = []
    if wanted in ("vector", "hybrid"):
        vector_rows, fallback = await search_vectors(question, scope, db, limit=limit * VECTOR_CANDIDATE_MULTIPLIER)

    # 关键词车道什么时候跑：配置要它 / 混合车道（本来就两路都跑）/ 向量车道坏了要救场
    need_keyword = wanted in ("keyword", "hybrid") or bool(fallback)
    keyword_rows: list[dict] = []
    if need_keyword:
        if not terms:
            if wanted == "keyword":
                return RetrievedContext(mode="no_terms", lane=wanted, terms=terms, fallback=fallback)
        else:
            keyword_rows = await chunk_crud.search_chunks(scope, terms, db, candidate_limit=max(limit * 10, 40))

    if wanted == "keyword":
        picked = [dict(hit, lane="keyword") for hit in rank_hits(keyword_rows, terms, top_k=limit)]
    else:
        vector_picked = rank_vector_hits(vector_rows, top_k=limit)
        keyword_picked = rank_hits(keyword_rows, terms, top_k=limit) if keyword_rows else []
        picked = (
            vector_picked
            if wanted == "vector" and not fallback
            else merge_lane_hits(vector_picked, keyword_picked, top_k=limit)
        )

    text, sources = format_references(
        picked,
        max_chars=settings.CHAT_RETRIEVAL_MAX_CHARS,
        snippet_chars=settings.CHAT_SNIPPET_CHARS,
    )
    return RetrievedContext(
        text=text,
        sources=sources,
        mode=effective_mode(picked) if text else "none",
        lane=wanted,
        terms=terms,
        candidate_total=len(vector_rows) + len(keyword_rows),
        fallback=fallback,
    )


__all__ = [
    "RetrievedContext",
    "effective_mode",
    "extract_terms",
    "format_references",
    "merge_lane_hits",
    "rank_hits",
    "rank_vector_hits",
    "retrieve",
    "score_content",
    "search_vectors",
    "vector_filter",
    "vector_lane_block_reason",
]
