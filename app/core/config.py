"""全局配置

所有配置都可以写进 .env（或环境变量），字段名大小写不敏感。
DATABASE_URL 兼容旧写法 PSQL_URL；两者都不填时使用下面的默认值，
这样在还没配置数据库时项目依然可以被 import / 生成 OpenAPI 文档。
"""

from typing import Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认连接串：postgresql+asyncpg://用户名:密码@主机:端口/库名
DEFAULT_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/knowledge_base"
DEFAULT_SECRET_KEY = "dev-only-secret-key-please-change-me"

# 阿里云百炼（DashScope）的 OpenAI 兼容端点：embedding 模型默认走这里。
# 换区域/换站点只改这一个常量或 .env 里的 EMBEDDING_BASE_URL：
#   国际站 https://dashscope-intl.aliyuncs.com/compatible-mode/v1
#   专属域名 https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
DEFAULT_EMBEDDING_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"

# 通义 text-embedding-v3/v4 允许的向量维度（v4 比 v3 多 1536/2048 两档）。
# 维度是**不可逆**的：collection 一旦写过向量就定死了，改维度必须连 EMBEDDING_DIMS
# 带 collection 名一起换。所以这里在启动时就拦，而不是等第一次插入报维度不一致。
DASHSCOPE_EMBEDDING_DIMS: dict[str, tuple[int, ...]] = {
    "text-embedding-v3": (64, 128, 256, 512, 768, 1024),
    "text-embedding-v4": (64, 128, 256, 512, 768, 1024, 1536, 2048),
}
# 一次 embedding 请求最多带几条文本：v3/v4 都是 10 条，超了直接 400。
# langchain 的 OpenAIEmbeddings 默认 chunk_size=1000，不显式改就必踩这个坑。
DASHSCOPE_EMBEDDING_BATCH_LIMIT = 10

# 占位 key 的样子：your_api_key_here / sk-xxxx / changeme 这类。
# 认成「未配置」很重要 —— 否则开发期每个请求都真去连一次远端、拿一个 401，
# 慢且 noisy，还容易被误判成"网络或模型有问题"。
_PLACEHOLDER_HINTS = ("your_", "_here", "placeholder", "changeme", "xxxx")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------------- 应用信息 ----------------
    APP_NAME: str = "AI知识库系统"
    APP_DESCRIPTION: str = "这是一个ai知识库系统"
    APP_VERSION: str = "0.1.0"
    # 环境变量只认 APP_DEBUG：裸 DEBUG 是个被用烂的名字（node 的 debug 包、各种 shell 都设过，
    # 本机就有 DEBUG=release），字段名直接叫 DEBUG 会被外面的值污染成非法布尔 → 应用起不来。
    DEBUG: bool = Field(default=True, validation_alias="APP_DEBUG")
    API_V1_PREFIX: str = "/api/v1"

    # ---------------- 数据库 ----------------
    DATABASE_URL: str = Field(
        default=DEFAULT_DATABASE_URL,
        validation_alias=AliasChoices("DATABASE_URL", "PSQL_URL"),
    )
    # 开发时想看打印的 SQL 就把 SQL_ECHO 设成 True
    SQL_ECHO: bool = False
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20

    # ---------------- JWT ----------------
    SECRET_KEY: str = DEFAULT_SECRET_KEY
    ALGORITHM: str = "HS256"
    # 单位：分钟，默认 1 天
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24
    # 引导用：注册时命中这里列出的用户名，users.role 直接给 admin
    # （库里已有 role 字段，权限判断一律看 role，这个配置只负责「第一个管理员从哪来」）
    ADMIN_USERNAMES: str = "admin"

    # ---------------- LLM（对话模型）----------------
    # 对话模型和向量模型**是两件事、通常也是两家**：DeepSeek 只提供 chat，没有 embedding 接口，
    # 所以本项目 embedding 固定走阿里云百炼的 OpenAI 兼容模式（text-embedding-v3）。
    # 两组字段各自独立：LLM_* 管问答，EMBEDDING_* 管向量化与向量检索。
    # 两个字段都留了旧名字的别名（OPENAI_API_KEY / OPENAI_BASE_URL），app/rag 里沿用老写法也能读到。
    LLM_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("LLM_API_KEY", "OPENAI_API_KEY"),
    )
    # 端点的旧名字有三种写法，全都认：.env 里现在用的是 OPENAI_API_BASE_URL
    LLM_BASE_URL: str = Field(
        default="https://api.deepseek.com/v1",
        validation_alias=AliasChoices("LLM_BASE_URL", "OPENAI_BASE_URL", "OPENAI_API_BASE_URL"),
    )
    LLM_MODEL: str = "deepseek-chat"
    LLM_TEMPERATURE: float = 0.2
    LLM_TIMEOUT_SECONDS: int = 60
    EMBEDDING_API_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("EMBEDDING_API_KEY", "OPENAI_EMBEDDING_API_KEY"),
    )
    # 默认端点已经是百炼兼容模式：只要填一个 EMBEDDING_API_KEY 就能跑，
    # 想换 OpenAI 本体/别的兼容网关就把这里改成对方的 /v1。
    EMBEDDING_BASE_URL: str = Field(
        default=DEFAULT_EMBEDDING_BASE_URL,
        validation_alias=AliasChoices("EMBEDDING_BASE_URL", "OPENAI_EMBEDDING_BASE_URL"),
    )
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIMS: int = 1024             # 一旦建过库就不能改，改维度要重建 collection
    # 每次 embedding 请求带的文本条数。百炼 v3/v4 的上限是 10（见上面常量），
    # 这里必须 <= 上限；VECTOR_INDEX_BATCH_SIZE 是"一批多少条切块"，两者是不同的层。
    EMBEDDING_BATCH_SIZE: int = DASHSCOPE_EMBEDDING_BATCH_LIMIT

    # ---------------- 视觉描述（A 路线：图片 → 文字 → 现有切块/向量链路）----------------
    # 只做一件事：把一张图变成一段**可检索的文字**。产出直接交给 chunker，
    # 所以图片描述和 md/pdf 走同一套切块、同一张 chunks 表、同一个 pgvector ——
    # 刻意不新开"多模态检索"这条链路（检索层不用动，才有办法用 --kb 做权限范围）。
    # key 默认复用百炼那把：text-embedding-v3 和 qwen-vl-max 在同一个兼容模式网关下，
    # 已经配过 EMBEDDING_API_KEY 的项目不必再配第三把凭证。
    VISION_MODEL: str = "qwen-vl-max"
    VISION_API_KEY: str = Field(default="", validation_alias=AliasChoices("VISION_API_KEY", "DASHSCOPE_API_KEY"))
    VISION_BASE_URL: str = Field(
        default=DEFAULT_EMBEDDING_BASE_URL,
        validation_alias=AliasChoices("VISION_BASE_URL", "DASHSCOPE_BASE_URL"),
    )
    VISION_TIMEOUT_SECONDS: int = 60
    VISION_MAX_TOKENS: int = 700          # 描述不是写论文，超了就截
    VISION_PROMPT: str = ""               # 空 = 用 app/services/vision.py 里的默认提示词（想改措辞不必改代码）
    # 送模型前的两道闸：先等比缩到 VISION_MAX_PIXELS，再卡字节数。
    # 手机相册原图动辄 8MB+，直接 base64 上去就是 11MB 请求体，慢、贵、还容易 413。
    VISION_MAX_PIXELS: int = 1568         # qwen-vl 内部也是按 tile 切的，缩到这一档基本不丢信息
    VISION_MAX_IMAGE_BYTES: int = 4 * 1024 * 1024
    # 图片车道总开关：True 且视觉可用 → 上传后 status=pending 进解析流；否则维持 stored（只存不解析）
    IMAGE_PARSE_ENABLED: bool = True
    # 扫描件 PDF 兜底：整篇提不到字时把页面栅格化后交给视觉模型。
    # **默认关**：一页一次付费请求，50 页的扫描件就是 50 次，必须让人明确决定；
    # 另外栅格化要 pymupdf（可选依赖），没装时开了这个开关也只会给一条说清楚原因的 failed。
    SCANNED_PDF_VISION: bool = False
    SCANNED_PDF_MAX_PAGES: int = 5

    # ---------------- 磁盘布局（唯一真源，别再在别处写死 "data"）----------------
    # data/ 只装**用户上传的原件**（外加 data/share/ 里被授权人传的那份）；
    # 可重建的派生物（local 记忆车道的 jsonl、以后可能有的解析临时文件）一律进 var/。
    # 为什么可配置：测试要把这两棵树指到临时目录，否则每跑一次 pytest 工作区就多几十个孤儿目录。
    DATA_DIR: str = "data"
    VAR_DIR: str = "var"

    # ---------------- 长期记忆（跨会话，按 user_id 隔离）----------------
    # 默认 **pgvector**：长期记忆只认 PostgreSQL 一条路（业务要求：不留第二份记忆数据）。
    # pgvector = langchain_postgres.PGVector：真语义向量，需要 PG 库 + pgvector 扩展 + EMBEDDING_API_KEY
    # off      = 全链路 no-op（灰度或不想让记忆进 prompt 时用）
    # local    = 本地 JSONL + 词面 hash：**仅供离线自测**，不是生产选项，也**不再是任何降级的目标**。
    #            以前 pgvector 连不上会自动退 local，那等于"生产上悄悄换了套记忆存储"，
    #            两份数据两套行为，是最难发现的一类问题；现在连不上就明确不记（见 memory_lane_block_reason）。
    MEMORY_BACKEND: Literal["local", "pgvector", "off"] = "pgvector"
    MEMORY_DIR: str = "var/memory"     # 注意在 var/：这是派生物，不是知识库文件
    MEMORY_TOP_K: int = 3
    MEMORY_SCORE_THRESHOLD: float = 0.5    # 低于这个相似度不进 prompt；调高会漏，调低会混噪音
    MEMORY_DEDUP_THRESHOLD: float = 0.95   # 高于这个相似度不再重复写
    MEMORY_MAX_ITEMS_PER_USER: int = 500   # 每用户保留条数上限，超出按最旧淘汰
    MEMORY_AUTO_WRITE: bool = True         # 每轮问答后是否自动落长期记忆
    MEMORY_EXTRACT_FACTS: bool = True      # True=用 LLM 抽「用户事实」再写；False=写一轮对话的摘要
    MEMORY_PG_COLLECTION: str = "long_term_memory"
    # 知识库文档向量的 collection（与记忆分开，见 app/rag/memory/save.py 的说明）
    DOCUMENT_VECTOR_COLLECTION: str = "kb_documents"
    VECTOR_INDEX_BATCH_SIZE: int = 64

    # ---------------- 上传限制（手机端必须卡死）----------------
    # 为什么不是"前端限制了就行"：客户端的限制只是体验优化，服务端才是唯一有效的闸。
    # 移动网络上传大文件失败的代价（用户等 3 分钟然后 413）远高于拒收一个 21MB 的文件。
    UPLOAD_MAX_SIZE_MB: int = 20        # 文档（pdf/docx/pptx/md/txt）
    IMAGE_MAX_SIZE_MB: int = 8          # 图片（jpg/png/webp/gif/bmp）：多模态问答预留的车道
    # 内容嗅探读多少个字节就够（只对齐 magic number，绝不为嗅探而读完整文件）
    UPLOAD_SNIFF_BYTES: int = 16
    # 空文件（0 字节）默认拒收：它进库只会变成一条永远解析失败的记录
    UPLOAD_REJECT_EMPTY: bool = True

    # ---------------- Redis（旁路缓存 cache-aside）----------------
    # 铁律：缓存只当**加速器**，永远不是数据源。连不上/超时/键没有，一律回源数据库，
    # 且**绝不因为缓存失败而让接口报错**（fail-open）。反过来，写操作必须先落库再删键，
    # 顺序反了就会留下"库里改了、缓存还是旧的"这种最难查的 bug。
    REDIS_ENABLED: bool = True          # False = 整套缓存旁路直接关掉，行为退回纯数据库
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    REDIS_SOCKET_TIMEOUT_SECONDS: float = 0.5   # 比一次数据库查询还慢的缓存不如不要
    REDIS_MAX_CONNECTIONS: int = 20
    CACHE_KEY_PREFIX: str = "kb"        # 所有键都带这个前缀，清缓存时能按前缀扫，不会误伤同机其它应用
    USER_CACHE_TTL_SECONDS: int = 300   # 用户身份缓存的存活时间；改密/禁用/注销都会立刻删键

    # ---------------- 文档索引（M2 后半）----------------
    # 上传成功后是否立刻丢给 BackgroundTasks 解析切块。
    # 测试车道设 False：那里的全局引擎是没建表的内存库，任务跑了只会制造噪声；
    # 切块入库本身用 process_document(session=...) 直接测。
    DOCUMENT_INDEX_ON_UPLOAD: bool = True

    # ---------------- 短期记忆（单个会话内的上下文窗口）----------------
    SHORT_TERM_ENABLED: bool = True
    SHORT_TERM_MAX_TURNS: int = 6          # 一轮 = 一问一答；超出就把最老的轮次压进摘要
    SHORT_TERM_TOKEN_BUDGET: int = 2000    # 窗口 token 预算，和轮数哪个先到就压哪个
    SHORT_TERM_SUMMARY_MAX_CHARS: int = 400
    SHORT_TERM_TTL_SECONDS: int = 4 * 60 * 60   # 会话闲置多久算过期（过期即丢窗口）
    SHORT_TERM_MAX_SESSIONS: int = 512          # 进程内最多缓存多少个会话，超出按 LRU 淘汰
    SHORT_TERM_PERSIST: bool = False            # True 时把窗口落 var/memory/sessions/，重启不丢
    SHORT_TERM_USE_TIKTOKEN: bool = False       # 默认用字符启发式：tiktoken 首次要联网下载词表

    # ---------------- 对话（M5：/aichat 会话与问答）----------------
    # 检索总开关。关掉之后问答仍然能跑（只是没有【参考资料】），
    # 前端联调时可以拿它对比「模型凭记忆答」和「凭资料答」的差别。
    CHAT_RETRIEVAL_ENABLED: bool = True
    CHAT_RETRIEVAL_TOP_K: int = 4          # 进 prompt 的资料块数（比 MEMORY_TOP_K 大一点：文档片段更碎）
    CHAT_RETRIEVAL_MAX_CHARS: int = 4000   # 资料总长上限，超了从后往前丢，防止一个长块吃掉整个窗口
    CHAT_SNIPPET_CHARS: int = 320          # 单块进 prompt 的截断长度（引用列表里也是这个）
    # ---------------- 检索车道 ----------------
    # keyword = 只在 DB 里做词面 ILIKE + 词频排序：不联网、不依赖 pgvector，永远可用
    # vector  = 只走 pgvector 语义召回（需要 EMBEDDING_API_KEY + PostgreSQL）
    # hybrid  = 两路都跑，按 chunk_id 去重合并（召回最全，代价是两倍延迟）
    # 默认仍是 keyword：填了 embedding key 也不自动改变线上行为，切车道是**显式决定**；
    # 车道不可用时（没 key / SQLite / 远端失败）会退回 keyword 并在 mode 里如实标记。
    RETRIEVAL_LANE: Literal["keyword", "vector", "hybrid"] = "keyword"
    # 向量召回的相似度下限（COSINE 归一到 0~1，0=不过滤）。调高会漏，调低会混噪音进来。
    RETRIEVAL_MIN_SCORE: float = 0.0

    CHAT_STREAM_HEARTBEAT_SECONDS: float = 15.0  # 0=不发心跳；反代/手机的空闲超时一般 30~60s
    CHAT_AUTO_TITLE_CHARS: int = 30        # 首轮用问题自动命名时截取多少个字符
    # 僵尸生成：上一路流要是被硬杀且没走到 finally，这一路会一直占着"该会话正在生成"的键。
    # 超过这个秒数就允许被新请求顶掉（同时把旧的取消掉），而不是永远 409。
    CHAT_STALE_GENERATION_SECONDS: int = 600

    # ---------------- 启动检查 / 健康检查 ----------------
    # 表不齐就不让进程起来（生产/Docker 建议 True）。默认 False：不改现有行为，
    # 但 /health 会如实返回 503，编排系统和 CI 看得到。
    DB_STRICT_STARTUP: bool = False
    # 仅开发用：启动时 metadata.create_all。正式环境应该走迁移（TODO.md P1 的 alembic）
    AUTO_CREATE_TABLES: bool = False
    # 探库超时：远端不可达时 TCP 连接会挂在内核重试里，没超时 /health 自己就成故障源
    HEALTH_CHECK_TIMEOUT_SECONDS: float = 2.0

    # ---------------- 跨域 ----------------
    # 逗号分隔，例如 http://localhost:5173,http://127.0.0.1:5173
    CORS_ORIGINS: str = "*"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _origins_to_str(cls, value):
        """允许 .env 里写成 JSON 数组或者普通字符串"""
        if isinstance(value, list):
            return ",".join(str(v) for v in value)
        return value if value is not None else "*"

    @model_validator(mode="after")
    def _fill_blank_values(self):
        """.env 里写了空的 key（例如 `PSQL_URL =`）时，回退到默认值而不是报错"""
        if not self.DATABASE_URL or not self.DATABASE_URL.strip():
            self.DATABASE_URL = DEFAULT_DATABASE_URL
        if not self.SECRET_KEY or not self.SECRET_KEY.strip():
            self.SECRET_KEY = DEFAULT_SECRET_KEY
        for name, default in (
            ("LLM_BASE_URL", "https://api.deepseek.com/v1"),
            ("LLM_MODEL", "deepseek-chat"),
            ("EMBEDDING_MODEL", "text-embedding-v3"),
            ("EMBEDDING_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
            ("VISION_MODEL", "qwen-vl-max"),
            ("VISION_BASE_URL", DEFAULT_EMBEDDING_BASE_URL),
            ("MEMORY_DIR", "var/memory"),
            ("MEMORY_PG_COLLECTION", "long_term_memory"),
        ):
            value = getattr(self, name)
            if value is None or not str(value).strip():
                setattr(self, name, default)
        return self

    @model_validator(mode="after")
    def _check_embedding_setup(self):
        """embedding 模型的两个硬约束放在启动时查，别等第一次写向量才炸

        维度选错 = 往库里写不可逆的脏数据；批量超限 = 每个请求都 400。
        两者都是配置写错，当场报错比运行时慢慢漏出来好查得多。
        """
        allowed = DASHSCOPE_EMBEDDING_DIMS.get(self.EMBEDDING_MODEL)
        if allowed is not None and self.EMBEDDING_DIMS not in allowed:
            raise ValueError(
                f"EMBEDDING_MODEL={self.EMBEDDING_MODEL} 只支持这些维度 {allowed}，"
                f"当前 EMBEDDING_DIMS={self.EMBEDDING_DIMS}；"
                "注意维度一旦建过 collection 就不能改，换维度要换一个 collection 名重建"
            )
        limited = self.EMBEDDING_MODEL in DASHSCOPE_EMBEDDING_DIMS
        if limited and self.EMBEDDING_BATCH_SIZE > DASHSCOPE_EMBEDDING_BATCH_LIMIT:
            raise ValueError(
                f"EMBEDDING_BATCH_SIZE={self.EMBEDDING_BATCH_SIZE} 超过百炼的单请求上限 "
                f"{DASHSCOPE_EMBEDDING_BATCH_LIMIT}（超过会被服务端直接 400）"
            )
        if self.EMBEDDING_BATCH_SIZE < 1:
            raise ValueError("EMBEDDING_BATCH_SIZE 至少是 1")
        return self

    @property
    def admin_usernames(self) -> set[str]:
        return {name.strip() for name in self.ADMIN_USERNAMES.split(",") if name.strip()}


    # ---- 兼容旧写法：app/rag/agent.py 里读的是 settings.OPENAI_API_KEY ----
    @property
    def OPENAI_API_KEY(self) -> str:
        return self.LLM_API_KEY

    @property
    def OPENAI_BASE_URL(self) -> str:
        return self.LLM_BASE_URL

    @staticmethod
    def _usable_key(value: str) -> bool:
        """非空、且不像占位符，才认为真的能拿去调远端"""
        text = (value or "").strip().lower()
        if not text:
            return False
        if any(hint in text for hint in _PLACEHOLDER_HINTS):
            return False
        return not set(text.removeprefix("sk-")) <= {"x", "0", "-", "_"}

    @property
    def llm_ready(self) -> bool:
        """能真正调 LLM 吗（没配 key 时记忆层自动降级成规则式摘要，不报错也不白跑一次网络）"""
        return self._usable_key(self.LLM_API_KEY)

    @property
    def embedding_ready(self) -> bool:
        return self._usable_key(self.EMBEDDING_API_KEY)

    @property
    def vision_api_key(self) -> str:
        """真正拿去调视觉模型的那把 key：VISION_API_KEY 优先，没填就回落到百炼的 embedding key"""
        return self.VISION_API_KEY or self.EMBEDDING_API_KEY

    @property
    def vision_ready(self) -> bool:
        return self.IMAGE_PARSE_ENABLED and self._usable_key(self.vision_api_key)

    @property
    def vision_lane_block_reason(self) -> str:
        """视觉车道不能真跑的原因，空串 = 可以跑

        和 vector_lane_block_reason / memory_lane_block_reason 同一口径：
        降级必须**看得见**。图片没描述出来会变成"库里有一张搜不到的图"，
        用户只会以为检索坏了，所以这里必须能被 /health 和 ./start.sh check 读到。
        """
        if not self.IMAGE_PARSE_ENABLED:
            return "IMAGE_PARSE_ENABLED=False，图片只落盘不解析（status=stored）"
        if not self._usable_key(self.vision_api_key):
            return "未配置 VISION_API_KEY（留空则复用 EMBEDDING_API_KEY，百炼两处同一把）"
        return ""

    @property
    def embedding_is_dashscope(self) -> bool:
        """当前 embedding 模型是不是百炼的 v3/v4（影响 dimensions 参数与批量上限）"""
        return self.EMBEDDING_MODEL in DASHSCOPE_EMBEDDING_DIMS

    @property
    def memory_lane_block_reason(self) -> str:
        """长期记忆这条车道**不能真跑**的原因；空串 = 可以跑

        存在的意义是"降级可见"：连不上 PG 时行为是不记（不是偷偷换成本地文件），
        但运维必须能从 /health 一眼看出为什么不记，否则线上会一直以为记忆在生效。
        """
        if self.MEMORY_BACKEND == "off":
            return "MEMORY_BACKEND=off，长期记忆整条链路不记"
        if self.MEMORY_BACKEND == "local":
            return "MEMORY_BACKEND=local 是离线自测车道：记忆只写本机 JSONL，多 worker/线上都不通用"
        if self.DATABASE_URL.startswith("sqlite"):
            return "MEMORY_BACKEND=pgvector 但 DATABASE_URL 是 SQLite：长期记忆不记（请换 PostgreSQL）"
        if not self.embedding_ready:
            return "MEMORY_BACKEND=pgvector 但没配 EMBEDDING_API_KEY：长期记忆不记"
        return ""

    @property
    def vector_lane_ready(self) -> bool:
        """向量检索/向量入库这条路现在能不能真跑（缺 key 或连的是 SQLite 就不行）"""
        return self.embedding_ready and not self.DATABASE_URL.startswith("sqlite")

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


settings = Settings()
