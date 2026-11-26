"""知识库（M0 本体 + 共享授权）的请求 / 响应模型

命名口径：`Kb*` 是库本体，`KbShare*` 是授权关系。两者都放这一个文件，
因为它们是同一组 `/kb` 路由的请求体，拆开反而找不到。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from app.db.models import KbPermission, KbVisibility


class KbShareCreate(BaseModel):
    """授权请求体

    只按 email 找人（用户名可能重名语义不明确，且 email 是唯一的）；
    重复授权 = 更新权限，所以这个接口是幂等的。
    """

    email: EmailStr = Field(description="被授权用户的邮箱")
    permission: KbPermission = Field(
        default=KbPermission.READ,
        description="read=只读（可看列表/详情/下载）；write=可读写（可上传、删除文档）",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class KbShareOut(BaseModel):
    """一条授权记录（被授权人信息一并返回，省得前端再查一次用户）"""

    share_id: uuid.UUID = Field(description="授权记录ID")
    kb_id: uuid.UUID = Field(description="知识库ID")
    grantee_id: uuid.UUID = Field(description="被授权用户ID")
    grantee_username: str = Field(description="被授权用户名")
    grantee_email: EmailStr = Field(description="被授权用户邮箱")
    permission: KbPermission = Field(description="read / write")
    created_at: datetime | None = Field(default=None, description="授权时间")


# ======================= M0：知识库本体 =======================


class KbCreate(BaseModel):
    """建库请求体

    description 是这个库的「具体说明」（装什么内容、给谁用、口径怎么定），
    问答时会直接拼进 prompt 让模型知道资料范围，所以它对回答质量是有影响的字段，
    不是注释 —— 但依然允许留空，移动端第一步只想先建个空壳是合理用法。
    """

    name: str = Field(min_length=1, max_length=100, description="知识库名称（同一用户下唯一）")
    description: str = Field(default="", max_length=2000, description="知识库具体说明（装什么/给谁用/口径）")
    visibility: KbVisibility = Field(
        default=KbVisibility.PRIVATE, description="private=仅授权者可见；public=全站只读"
    )
    chunk_size: int = Field(default=800, ge=50, le=8000, description="切块目标长度（字符），只对之后的解析生效")
    chunk_overlap: int = Field(default=120, ge=0, le=2000, description="相邻块的重叠长度，必须小于 chunk_size")

    model_config = ConfigDict(str_strip_whitespace=True)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("知识库名称不能为空")
        return value

    @model_validator(mode="after")
    def _overlap_smaller_than_chunk(self):
        """重叠必须小于块长：相等意味着每次滑动窗口都在原地抄一遍，chunker 会收敛成死循环的邻居"""
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class KbUpdate(BaseModel):
    """改库请求体：**没传的字段一律不动**（所以全部 Optional）

    刻意不用「必填的全量 PUT」：移动端表单经常只改一个字段（比如只改描述），
    全量提交会逼前端先把 detail 拉回来再回填，多一次往返、也多一次覆盖别人的并发改动。
    """

    name: str | None = Field(default=None, min_length=1, max_length=100, description="新名称（同一用户下唯一）")
    description: str | None = Field(default=None, max_length=2000, description="新的具体说明；传空串表示清空")
    visibility: KbVisibility | None = Field(default=None, description="private / public")
    chunk_size: int | None = Field(default=None, ge=50, le=8000, description="切块长度（不会自动重切已有文档）")
    chunk_overlap: int | None = Field(default=None, ge=0, le=2000, description="重叠长度，必须小于 chunk_size")

    model_config = ConfigDict(str_strip_whitespace=True)

    @model_validator(mode="after")
    def _at_least_one_field(self):
        """空 body 报 422 而不是"成功但什么都没变"

        前端漏传字段是常见病，静默成功就等于把一个 bug 藏起来。
        """
        if not self.model_fields_set:
            raise ValueError("至少要提交一个要修改的字段")
        size = self.chunk_size
        overlap = self.chunk_overlap
        if size is not None and overlap is not None and overlap >= size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        return self


class KbOut(BaseModel):
    """知识库的一行（列表和详情共用同一个形状）

    permission/is_owner 是给移动端的硬信息：按钮该不该亮（上传/删除/授权/改设置）
    全看这两个字段，前端不该自己去猜 owner_id == 自己。
    """

    kb_id: uuid.UUID = Field(description="知识库ID")
    name: str = Field(description="名称")
    description: str = Field(default="", description="具体说明")
    visibility: str = Field(description="private / public")
    owner_id: uuid.UUID = Field(description="创建者ID")
    is_owner: bool = Field(default=False, description="当前用户是不是创建者（改设置/删库/授权只有 owner 能做）")
    permission: str = Field(description="当前用户对该库的权限：read / write（owner 恒为 write）")
    document_count: int = Field(default=0, description="文档数")
    chunk_count: int = Field(default=0, description="切块数")
    version: int = Field(default=1, description="内容版本号，文档增删时 +1")
    last_indexed_at: datetime | None = Field(default=None, description="最近一次索引完成时间")
    chunk_size: int = Field(default=800, description="切块长度（之后的解析用它）")
    chunk_overlap: int = Field(default=120, description="切块重叠长度")
    created_at: datetime | None = Field(default=None, description="创建时间")
    updated_at: datetime | None = Field(default=None, description="更新时间")


class KbDeleteOut(BaseModel):
    """删库结果：把"清掉了什么"如实报出来，别说一句"删除成功"就把失败的文件清理藏起来"""

    kb_id: uuid.UUID = Field(description="被删除的知识库ID")
    document_count: int = Field(default=0, description="随之删除的文档记录数")
    files_removed: int = Field(default=0, description="实际删掉的磁盘文件数")
    files_missing: int = Field(default=0, description="记录里有、磁盘上本来就没有的文件数（孤儿记录，已一并清掉）")
    vectors_removed: int = Field(default=0, description="清理掉的 pgvector 向量条数（向量车道不可用时为 0）")
