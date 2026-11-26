"""独立搜索接口（POST /search）的请求 / 响应模型

这个接口是"纯搜索框"用的：只要资料，不要模型作答。
它和问答里的检索走的是**同一条实现**（app/rag/retrieval.py），
所以不会出现"搜索页搜得到、问答却说资料里没有"这种两张皮。
"""

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


class SearchRequest(BaseModel):
    """搜索请求

    kb_ids 是**可选的收窄条件**，不是授权来源：不传就在"你读得到的全部库"里搜，
    传了就在"你读得到的 ∩ 你点名的"里搜。服务端永远先算可读范围，
    客户端说什么都不能扩大它 —— 这一条是越权的唯一防线。
    """

    q: str = Field(min_length=1, max_length=500, description="检索词/问题原文")
    kb_ids: list[uuid.UUID] | None = Field(
        default=None, max_length=50, description="限定在这些库里搜（必须是你能读的）"
    )
    top_k: int | None = Field(default=None, ge=1, le=20, description="返回几条，默认用 CHAT_RETRIEVAL_TOP_K")

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("q")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("检索词不能为空")
        return value


class SearchHit(BaseModel):
    """一条命中。字段和问答里 references 帧的 sources 完全一致，前端一份渲染代码两用"""

    index: int = Field(description="序号，从 1 开始（对应正文里的角标）")
    document_id: uuid.UUID = Field(description="文档ID")
    kb_id: uuid.UUID = Field(description="所属知识库ID")
    chunk_id: uuid.UUID = Field(description="切块ID（点回原文定位用它）")
    chunk_seq: int = Field(default=0, description="块在文档内的序号")
    page: int | None = Field(default=None, description="页码（解析不出来的格式为 null）")
    heading: str | None = Field(default=None, description="所属小标题")
    title: str = Field(default="", description="文档标题")
    score: float = Field(default=0.0, description="相关度。**量纲看 lane**：keyword 是词频分，vector 是 0~1 相似度")
    lane: str = Field(default="keyword", description="这条是哪种检索给的：keyword / vector / both")
    snippet: str = Field(default="", description="命中片段（已按 CHAT_SNIPPET_CHARS 截断）")


class SearchOut(BaseModel):
    """搜索结果 + 这次检索到底发生了什么（车道、候选数、有没有降级）"""

    query: str = Field(description="原样回显检索词，前端好做高亮")
    mode: str = Field(description="生效车道或拦截原因：keyword/vector/hybrid/none/no_scope/no_terms/disabled")
    lane: str = Field(description="配置要求走的车道：keyword/vector/hybrid")
    fallback: str = Field(default="", description="非空=向量车道不可用已退回关键词，这里带原因")
    candidates: int = Field(default=0, description="截断前的候选块数")
    scoped_kb_ids: list[uuid.UUID] = Field(default_factory=list, description="本次实际搜索的库范围")
    hits: list[SearchHit] = Field(default_factory=list, description="命中列表")
