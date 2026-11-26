# knowledge_base

一个 AI 本地知识库。

- **[TODO.md](TODO.md)** —— 还差什么没实现（**真 PG / 向量库的真机验收**、后端 6 条尾巴、前端、手机化、上线前收尾），做功能先看这里
- **[DEVELOPMENT.md](DEVELOPMENT.md)** —— 开发规范（分层、统一返回格式、命名、鉴权、测试、Git、依赖）

## 目录结构

```
.                               # 项目根 = Python 导入根（所以一律 from app.xxx import ...）
├── pyproject.toml              # uv 项目定义 + pytest / ruff 配置
├── uv.lock                     # 完整锁定文件
├── .venv/                      # 虚拟环境（根目录，已 gitignore）
├── requirements*.txt           # 给 pip 用户的等价清单
├── init_db.sql                 # 由 models.py 生成的 PG 建表快照（勿手改）
├── start.sh                    # 启动脚本（uvicorn app.main:app --reload）
├── scripts/
│   ├── init_db.py              # 建表 / --drop 重置 / --check / --snapshot 生成 SQL
│   ├── parse_document.py       # 解析预览 CLI：看一个文件被切成哪些块（M2 排错用）
│   ├── index_documents.py      # 文档向量化入库 CLI（按 document_id 幂等；--query 走线上同一条检索）
│   └── try_memory.py           # 记忆层自检 CLI（不建库不联网也能跑）
├── tests/                      # 接口测试 + 表结构测试
├── frontend/                   # Vue 3 + Vite + TS + Naive UI（第一版已实现，见下面「前端」一节）
│   ├── src/api/                # 唯一出网口：client(信封解包/401) · endpoints(33 条) · sse(流式) · types
│   ├── src/views/              # 6 个页面：登录 · 知识库 · 文档 · 授权 · 对话 · 搜索 ·（我的）
│   ├── src/stores|router|utils # Pinia 登录态 · 路由守卫与 hash 回退 · token 存取(壳里换 Preferences)
│   └── src/components/         # MarkdownBlock(过 DOMPurify) · SourceList · StatusTag · KbFormModal
├── data/                      # 知识库数据（文件、图片、视频、音频、文档、模型等）
└── app/                        # 后端代码包
    ├── main.py                 # FastAPI 入口 + 统一返回格式的异常处理器
    ├── api/
    │   ├── deps.py             # 公共依赖：DB Session、当前登录用户、管理员、KBReader/KBWriter/KBManager
    │   └── v1/
    │       ├── userrouter.py       # 用户路由
    │       ├── kbrouter.py         # 知识库路由（M0 本体 5 条 + 共享授权 3 条）
    │       ├── documentrouter.py   # 文档路由（上传/图片上传/列表/详情/下载/删除/状态/重索引）
    │       ├── searchrouter.py     # POST /search：纯检索，不叫模型作答（与问答共用同一条检索）
    │       └── aichatrouter.py     # 对话路由（会话 CRUD + SSE 问答 + 停止 + agent 目录）
    ├── core/
    │   ├── config.py           # .env 配置
    │   ├── security.py         # 密码哈希 + JWT 签发/解析
    │   ├── document_hash.py    # 上传文件的流式 sha256 + 体积上限
    │   ├── cache.py            # Redis 旁路缓存：客户端、fail-open 降级、键规矩（**只当加速器**）
    │   └── exceptions.py       # 业务异常（统一包成 status/msg/data）
    ├── db/
    │   ├── base.py             # DeclarativeBase
    │   ├── models.py           # 表结构唯一真源：User / Token / KnowledgeBase / Document / Chunk / KbShare
    │   ├── psql_config.py      # 异步引擎、Session、get_db 依赖
    │   └── crud/               # 数据访问层（只读写库，不抛 HTTP 异常）
    ├── services/               # 业务算法层
    │   ├── kb_access.py        # 知识库权限矩阵与越权语义的唯一实现处
    │   ├── process_document.py # 存储路径规则、落盘、删除、解析→切块→入库编排
    │   ├── chunker.py          # 切块纯函数：按 heading 分组 + 滑窗（长度取 kb.chunk_size）
    │   ├── vector_index.py     # 把 chunks 向量化写入 pgvector（按 document_id 幂等）
    │   ├── file_types.py       # 上传类型策略唯一来源：文档/图片两套白名单 + 魔数嗅探
    │   ├── user_cache.py       # 用户身份的旁路缓存（只读车道 / 写车道分开）
    │   ├── text_metrics.py     # token 估算（切块与短期窗口共用一份口径）
    │   └── parsers/            # 格式解析：base / registry / pdf / docx / pptx / markdown
    └── rag/                    # 大模型侧
        ├── llm.py              # LLM 调用唯一入口（没配 key 自动降级，不让 import 炸）
        ├── chat.py             # 会话**存取**层：chat_sessions/chat_messages 是唯一存储
        ├── answer.py           # 一轮问答的**编排**：范围→检索→上下文→模型→落库（流式/一次性两条车道）
        ├── retrieval.py        # 检索层（M4 第一版：关键词召回 + 词频算分，形状与向量版一致）
        ├── generation.py       # 在跑的生成注册表（/stop 靠它；进程内，多 worker 有局限）
        ├── output/             # SSE 分帧 + 结构化输出（逐字流式与结构化输出互斥）
        └── memory/             # 两层记忆：session=短期窗口，store=长期检索（local/pgvector/off 三后端）
    └── schemas/                # ApiResponse[T] / PageData[T] / 各模块请求响应体
```

> 包里没有 `__init__.py`，靠 Python 3.3+ 的命名空间包（PEP 420）导入，少一层维护成本。

## 快速开始

```bash
# 1. 装依赖（在项目根执行，虚拟环境 = 根目录 .venv）
~/.local/bin/uv sync --group dev
#    不想用 uv 的话: python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt

# 2. 配置 .env（可直接复制 .env.example，至少要填 DATABASE_URL 和 SECRET_KEY）
cp .env.example .env

# 3. 建表（表结构真源是 app/db/models.py，按 .env 里的 DATABASE_URL 执行）
.venv/bin/python scripts/init_db.py            # 只建缺失的表
.venv/bin/python scripts/init_db.py --check    # 检查库里的表和模型是否一致
.venv/bin/python scripts/init_db.py --drop     # 开发期重置：删表重建（会清空数据）
#    init_db.sql 是同一份结构生成的 PostgreSQL 快照，可用 --snapshot > init_db.sql 刷新

# 4. 启动
./start.sh            # 等价于 uvicorn app.main:app --reload
```

接口文档：<http://127.0.0.1:8000/docs>

前端（可选，第 5 步）：

```bash
cd frontend && npm install && npm run dev     # http://<局域网IP>:5173，dev 走 vite 代理到 127.0.0.1:8000
```

## 前端（M7：第一版已实现）

技术栈按 `DEVELOPMENT.md` §13 的拍板结果落地：**Vue 3 + Vite + TypeScript + Naive UI + vue-router + Pinia + @tanstack/vue-query**，
渲染模型输出用 `marked` + **`dompurify`**（只有 `components/MarkdownBlock.vue` 一处用 `v-html`）。

页面顺序就是移动端的动线：登录/注册 → 知识库列表（owned/shared + permission 徽标 + 建库）→
文档管理（上传文档/图片、状态轮询、下载、重新解析、删除）→ 共享授权 → 对话（流式 + 引用 + 停止 + 会话侧栏）→ 搜索。

四条接口层的硬规矩（写在 `src/api/*.ts` 的注释里）：

| 规矩 | 落点 | 为什么 |
| --- | --- | --- |
| 信封只在 `client.ts` 解一次，`status !== 200` 统一抛 `ApiError` | `src/api/client.ts` | 业务代码不各写各的错误判断；401 就地清 token + 跳登录 |
| 流式用 `fetch` + `ReadableStream`，不用 `EventSource` | `src/api/sse.ts` | `EventSource` 不能带 Authorization 头，也只能 GET；`done` 帧的整段文本才落定气泡 |
| 按钮可用性只看后端给的 `permission` / `is_owner` | `views/KbListView.vue` | 权限矩阵只有一个真源（`services/kb_access.py`），复制到浏览器里迟早不一致 |
| `VITE_API_BASE` 只给主机名也会自动补 `/api/v1` | `client.ts:resolveBase` | 壳里没有 dev proxy，指错一次就是整屏 404，而它在 WebView 里长得跟"后端坏了"一样 |

已实测的部分：`npm run build` 通过、`vue-tsc` 类型检查干净、`vite` dev server 起得来；
**还没和真后端联调过**（后端 421 条测试全在临时 SQLite 上），真机/真库联调是 `TODO.md` §1+§4 的下一步。

### 启动检查与 `/health`

应用**自己不会建表**（表结构真源仍是 `app/db/models.py` + `scripts/init_db.py`），所以启动时会检查一次：

| 情形 | 行为 |
| --- | --- |
| 表齐 | 日志 INFO，正常启动 |
| 缺表 / 连不上 | 默认**只告警不阻断**，但 `/health` 返回 **503** 并列出缺哪几张表 |
| `DB_STRICT_STARTUP=True` | 缺表直接拒绝启动（生产/Docker 建议打开：让编排系统第一时间发现） |
| `AUTO_CREATE_TABLES=True` | 启动即建表 —— **只给单人开发用**，正式环境该走迁移 |
| 内存库（测试车道 `:memory:`） | 跳过检查，`/health` 返回 200 并带 `database.ephemeral=true` |

```bash
curl -s localhost:8000/health | head -c 200      # 探库带超时（HEALTH_CHECK_TIMEOUT_SECONDS，默认 2s）
```
`/health` 是免鉴权接口，所以响应里**只有类别信息**（方言、缺表清单、错误类型），
连接串、用户名密码、异常原文一律只进日志。Redis 连接串同样按这条规矩处理：
`cache` 一节只报 `transport`（redis / rediss(tls) / unix），不报 host、端口、密码。

响应里另外两节值得单独看一眼，因为它们**不影响 HTTP 状态码**：

| 节 | 字段 | 为什么要单独看 |
| --- | --- | --- |
| `cache` | `configured` / `client_ready` / `last_ok` / `active` / `circuit_open` / `hits` / `misses` | Redis 只是加速器，挂了服务照样能用（fail-open 回源数据库），所以不能把 readiness 打成 503；但**必须看得见**。`client_ready=true` 不等于能用（懒连接），要看 `last_ok` |
| `memory_backend` | `configured` / `ok` / `reason` | 长期记忆连不上 PG 时行为是"不记"，原因写在这里而不是悄悄换个后端 |

## 统一返回格式

所有接口（**包括报错**）都返回 `{"status": ..., "msg": ..., "data": ...}`，
`status` 与 HTTP 状态码保持一致，200 表示成功。类型由
`app/schemas/common.py` 里的泛型模型 `ApiResponse[T]` 描述，
路由上写 `response_model=ApiResponse[UserResponse]` 就能在 Swagger 里看到 `data` 的具体结构。

```jsonc
// 成功
{ "status": 200, "msg": "登录成功", "data": { "access_token": "eyJ...", "token_type": "bearer", "expires_in": 86400, "user": { "id": "uuid", "username": "zhangsan", "email": "a@b.com", "created_at": "..." } } }

// 失败（路由里 raise ConflictError("...") 即可，不用手写返回体）
{ "status": 409, "msg": "用户名 zhangsan 已被注册", "data": null }
```

## 接口一览

前缀 `/api/v1`；除注册/登录外都需要请求头 `Authorization: Bearer <token>`，
**用户身份一律来自 token，接口不再接收 user_id 参数**。

### 用户 `/user`

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/register` | 注册（用户名、邮箱唯一，密码 bcrypt 存储） | 用户信息 |
| POST | `/login` | 登录，`account` 支持用户名或邮箱 | token + 用户信息 |
| GET | `/info` | 当前登录用户信息（身份只来自 token，传 user_id 也无效） | 用户信息 |
| POST | `/update` | 改用户名/邮箱，只改传了的字段 | 用户信息 |
| POST | `/reset_password` | 改密码，需原密码；成功后所有 token 失效 | null |
| POST | `/logout` | 退出登录，删除当前 token | null |
| POST | `/delete` | 注销账号，需密码确认；关联数据级联删除 | null |
| GET | `/list` | 分页查看用户，`users.role=admin` 才可用 | 分页数据 |

登录签发的 JWT 只把 **sha256** 写进 `tokens` 表（不存原文），所以拖库拿不到可用凭证；
退出登录、改密码、注销都能让旧 token 立即失效，同时记录登录来源 ip / User-Agent。

第一个管理员：注册时用户名命中 `.env` 的 `ADMIN_USERNAMES` 就直接写 `role=admin`，
之后权限判断只看 `role`，不再依赖配置。

### 知识库 `/kb`（M0 本体）

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/kb` | 建库：`name` 必填（同一用户下唯一），`description`=这个库装什么/给谁用/口径，`visibility`、`chunk_size/overlap` 可选 | `KbOut`（version=1） |
| GET | `/kb/list?scope=&page=&size=` | `owned` 我建的 / `shared` 别人给我的(含 public) / `all` 合并去重；**每条带 permission** | 分页 `KbOut` |
| GET | `/kb/detail?kb_id=` | 库详情（read 即可，public 库人人可读） | `KbOut` |
| POST | `/kb/update?kb_id=` | 改名称/说明/可见范围/切块参数。**owner 专属**；只改提交了的字段，空 body 422 | `KbOut` |
| DELETE | `/kb/{kb_id}` | 删库：先列出文件路径 → 硬删记录（级联带走文档/块/授权）→ 删磁盘 → 收空目录 → 清向量 | `KbDeleteOut` |

三个容易踩的点：

- **`description` 是问答质量的输入**，不是注释：它是"这个库的具体说明"，会被拼进 prompt 让模型知道资料范围。
- **改 `chunk_size` / `chunk_overlap` 不会重切已有文档**，只对之后的解析生效。全库重切要逐篇走
  `POST /documents/reindex/{document_id}`（一个改配置的请求不该触发全库 embedding 费用）。
- **删库走硬删**，因为 `uq(owner_id, name)` 不含 `deleted_at`：软删会让那个名字永远被占着，
  用户视角就是"删了但建不回来"。删之前必须先查 `documents.file_path`，记录一删路径就再也算不回来。

### 知识库授权 `/kb`

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/kb/share?kb_id=` | owner 把库共享给某个邮箱的用户（`permission=read`/`write`），重复调用=更新权限 | 授权记录 |
| DELETE | `/kb/share?kb_id=&email=` | owner 取消授权（幂等，没授权过也不报错） | null |
| GET | `/kb/share/list?kb_id=` | owner 查看该库的授权名单 | 授权列表 |

权限矩阵（判定实现只在 `app/services/kb_access.py` 一处）：

| 操作 | owner | `write` 授权 | `read` 授权 | public 库的其他人 |
| --- | :-: | :-: | :-: | :-: |
| 看库信息 / 文档列表 / 详情 / 下载 / 状态 | ✓ | ✓ | ✓ | ✓ |
| 上传文档 / 删除文档（**含别人传的**） | ✓ | ✓ | ✗ | ✗ |
| 改库设置、删库、增删授权 | ✓ | ✗ | ✗ | ✗ |

两条对外语义：连 `read` 都没有 → 一律 **404**（不暴露别人的库是否存在）；能读不能写 → **403**。
共享库里别人上传的文件落在**库主人**目录下：`data/{库owner}/{kb_id}/{document_id}.{ext}`，一个库一个目录。

### 文档 `/documents`

| 方法 | 路径 | 说明 | 状态 |
| --- | --- | --- | --- |
| GET | `/list` | 分页列出自己的文档（可按 `kb_id` 过滤），带 `download_url` | ✅ 已实现 |
| POST | `/upload` | 上传文档：后缀白名单 → 体积上限（读的过程中卡，不信 Content-Length）→ 魔数核对后缀 → sha256 判重 → 落盘 → 写库（失败回滚） | ✅ 已实现 |
| POST | `/image/upload` | **上传图片**（jpg/png/webp/gif/bmp，`IMAGE_MAX_SIZE_MB` 上限）：落盘 + 写库，`status=stored` 不参与文本解析 | ✅ 已实现 |
| POST | `/reindex/{document_id}` | 重新解析入库单篇（`failed` 的重跑入口）：立刻返回，结果轮询 `/status` | ✅ 已实现 |
| GET | `/detail` | 文档详情：正文预览（按 chunk seq 升序）+ `kb_name`/`kb_version`/`chunk_total` | ✅ 已实现 |
| GET | `/status` | 知识库状态：六种解析进度 + **磁盘↔数据库一致性核对**（`missing_files`/`orphan_files`/`healthy`） | ✅ 已实现 |
| DELETE | `/delete/{document_id}` | 先判边界 → 删记录（级联删块、知识库 version+1）→ 再删磁盘文件 | ✅ 已实现 |
| GET | `/download/{document_id}` | 下载原始文件（`FileResponse`，用 `filename` 原始名，路径越界一律拒绝） | ✅ 已实现 |

文档 8 条路由已全部实现（`NotImplementedFeatureError` → 501 保留给以后新增接口占位用）。

上传的三道闸（唯一实现处 `app/services/file_types.py`，两条车道共用）：

| 闸 | 拦什么 | 响应 |
| --- | --- | --- |
| 后缀白名单 | `.exe` / `.doc`（老格式解析器不支持）/ 没有后缀 / 字符集非法 | 400 |
| 体积上限 | 超过 `UPLOAD_MAX_SIZE_MB`（文档）或 `IMAGE_MAX_SIZE_MB`（图片）。**在流式读的过程中判** | 413 |
| 魔数嗅探 | 后缀写 pdf 但内容不是 `%PDF`、后缀 png 内容不是 PNG 头… | 400 |
| 空文件 | 0 字节（`UPLOAD_REJECT_EMPTY`，默认拒）：进库只会变成一条永远失败的记录 | 400 |

图片走的是 `status=stored` 这条旁路：文件存得下、能下载、能删，但**不解析、不切块、不进向量库**。
为什么不用 `pending`：`pending` 会被重索引逻辑捞出来跑解析，图片解析必失败，
用户会看到一条莫名其妙的 `failed`。OCR / 多模态问答接上之后再把它推进解析流。

上传之后**解析/切块是异步的**：接口立刻返回 `status=pending`，后台任务把状态推到
`indexed` 或 `failed`（失败原因在 `error_msg`），前端轮询 `GET /documents/status` 就行。
想关掉自动索引（比如以后换成 worker）就设 `DOCUMENT_INDEX_ON_UPLOAD=False`；
手动重跑走 `POST /documents/reindex/{document_id}` 或 `scripts/index_documents.py`，重跑**幂等**
（切块按 document_id 先删后插，向量同理），不会越跑越多。

支持上传的格式由解析器注册表决定：**`pdf / docx / pptx / md / txt`**，
加一种格式只要在 `app/services/parsers/registry.py` 登记，上传接口自动放行（有测试锁这条一致性）。
解析目前做到「能读、能预览」，还没接进上传后的自动流程（`documents.status` 停在 `pending`）：

```bash
.venv/bin/python scripts/parse_document.py 需求说明书.docx        # 按块打印：页码/标题路径/表格/备注
.venv/bin/python scripts/parse_document.py slides.pptx --json     # 结构化输出，喂给 M2 排错
```
注意 `/documents/download/{id}` 返回的是文件流，不是 `ApiResponse` 那层 JSON —— 它的**错误**响应才是统一格式。

上传的边界（`multipart/form-data`，依赖 `python-multipart`，缺了它整个应用启动即崩）：
扩展名白名单由解析器注册表决定（不在表内 400）→ 知识库存在且属于你（404，防塞进别人的库）→
流式算 sha256 并卡 20MB（413）→ 同库判重（409）→ 落盘 `data/{user_id}/{kb_id}/{document_id}.{ext}` →
写库；任何一步失败都回滚已落盘的文件，不留垃圾。客户端的 `filename`/`Content-Length`/`Content-Type`
全部按不可信处理（展示名会 sanitize 成最后一段，MIME 由扩展名派生）。

删除文档的边界检查（顺序固定，任一条不过就**不碰磁盘**）：
未登录 → 401；`document_id` 非 uuid / 为空 → 422（路径参数类型挡掉）；
文档不存在或不属于你 → 404；`file_path` 是绝对路径、含 `..`、扩展名非法、
或与 `document_storage_path` 复算出的路径不一致（例如已废弃的老布局）→ 只删记录并在 `msg` 里说明。

存储路径按「谁上传」分两个落点，都由 `(库owner, 上传者, kb_id, document_id, ext)` 复算，所以事后一定删得对：

| 上传者 | 落点 |
| --- | --- |
| 库主人自己 | `data/{库owner}/{kb_id}/{document_id}.{ext}` |
| 被授权的人 | `data/share/{kb_id}/{document_id}.{ext}` |

被授权人的文件绝不写进库主人的个人目录（否则别人能耗光你的磁盘、删号时还留孤儿文件）；
也不按上传者分散到 N 个目录（那样一个库要扫 N 处）。因此 `/status` 对账扫**两个**目录。

`/documents/status` 的核对口径：`missing_files` 按每条记录的 `file_path` 逐个 `isfile` 判（精确，与目录布局无关）；
`orphan_files` 扫该库的两个目录（库主人目录 + `data/share/{kb_id}/`）取差集。两者都空才算 `healthy=true`；
不一致也返回 **200** —— 接口本身执行成功了，数据完整性用字段表达，不混 HTTP 语义。
⚠️ 约定目录之外的历史文件（如已废弃的 `data/kb/...`）扫不到，需要一次性清理脚本，不属于接口职责。

### 搜索 `/search`

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/search` | 纯检索（不叫模型作答）：`{q, kb_ids?, top_k?}`。与问答**共用同一条检索实现**，所以不会出现"搜索页搜得到、问答说资料里没有" | `SearchOut` |

三条语义：

- **范围 = 服务端算出的可读库 ∩ 你点名的库**。不传 `kb_ids` 就在全部可读库里搜；
  点名的库里有任何一个读不到 → **404，整次请求不执行**（不做"能搜的照样搜"，
  那会让前端把残缺结果当成全量）。
- **零结果仍是 200** + `msg` 说明"没有找到相关资料"：搜不到不是接口失败。
- `mode` / `lane` / `fallback` / `candidates` 原样透出，前端能分清
  `no_scope`（没有可读库）/ `none`（真没有）/ `disabled`（开关关了）/ 向量车道挂了退回关键词。

### 对话 `/aichat`（8 条，已接入）

| 方法 | 路径 | 说明 | data |
| --- | --- | --- | --- |
| POST | `/conversations` | 新建会话；`kb_id` 可把问答范围限定在某个库（必须你读得到，否则 404） | 会话信息 |
| GET | `/conversations` | 会话列表，按**最后发言时间**倒序，支持 `keyword` 标题搜索与分页 | 分页数据 |
| PUT | `/conversations/{conversation_id}` | 重命名（别人的会话 404） | 会话信息 |
| DELETE | `/conversations/{conversation_id}` | 删除会话，消息外键级联；会先取消该会话在跑的生成 | null |
| GET | `/conversations/{conversation_id}/messages` | 历史消息，按 seq 升序 + `before_seq` 游标翻页 | 会话 + 消息页 |
| POST | `/conversations/{conversation_id}/chat` | 一轮问答：默认 SSE 逐字流；`stream=false` 一次性 JSON | 流 / 回答体 |
| POST | `/conversations/{conversation_id}/stop` | 停止生成，**保留已生成的半截答案**并正常收尾 | 停止结果 |
| GET | `/agents` | 可选 agent 目录 + 当前配置下能不能用（M8 前是配置常量） | agent 列表 |

问答这条链路的四个要点：

1. **检索范围来自权限，不来自请求体**。会话限定了 `kb_id` 就只查那个库（授权被收回后查不到，
   但历史消息保留）；没限定则在「你读得到的全部库」里查（自己的 + 被共享的 + public）。
   没有「查全库」这种默认值 —— 越权是这类系统最常见的洞。
2. **检索有三条车道，一个开关**（`RETRIEVAL_LANE`，见 `app/rag/retrieval.py`）：

   | 车道 | 怎么找 | 依赖 | 返回的 `score` 是什么 |
   | --- | --- | --- | --- |
   | `keyword`（默认） | DB 里 ILIKE 取候选 + 词频算分 | 只要库能连 | 词频分（无界，只能横向比） |
   | `vector` | pgvector 余弦召回 chunk_id → **回 chunks 表取正文** | EMBEDDING_API_KEY + PostgreSQL | 相似度 0~1 |
   | `hybrid` | 两路都跑，按 chunk_id 去重（向量在前、关键词补漏） | 同 vector | 各自的分数，不混算 |

   默认仍是 `keyword`：填上 embedding key 就自动改变线上行为是不负责任的默认值，切车道是显式决定。
   向量车道不可用时（没配 key / 连的 SQLite / 远端 401 或超时 / collection 还没建）
   **自动退回关键词**并把原因写进 `retrieval.fallback`，不 500、不静默；
   而"车道正常跑了但零命中"不会偷偷换车道 —— 那是数据里没有，不是服务坏了，两者在
   `mode` 上可分辨（前者 `keyword` + 有 fallback，后者 `none` + fallback 为空）。
   `mode` 取值：`keyword` / `vector` / `hybrid`（真正给出资料的这条）/
   `none`（没命中）/ `no_scope`（没有可读库）/ `no_terms`（问题提不出词）/ `disabled`（开关关了）。
   embedding 用**阿里云百炼 `text-embedding-v3`（1024 维）**，维度在启动时校验，一旦建过库不能改。
3. **流式的错误分两段**。推流之前（会话不存在、模型没配置、该会话已有回答在生成）走正常 HTTP 状态码；
   推流之后只能 200 + `error` 帧，异常原文进日志、对外只给 `trace_id`。
4. **收尾落库用新开的 Session**（依赖 `get_session_factory`）：SSE 生成器活得比请求函数久，
   而且不该让一条数据库连接陪跑几十秒的模型流。

```bash
CID=$(curl -s -X POST $API/api/v1/aichat/conversations -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' -d '{"title":"报销咨询"}' | jq -r .data.id)

# 流式：-N 关掉 curl 的缓冲，否则看起来像"一次性蹦出来"
curl -N -X POST $API/api/v1/aichat/conversations/$CID/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"报销流程是什么","stream":true,"top_k":4}'

# 不要 SSE 的时候（脚本 / CI 冒烟 / 手机端兜底）
curl -s -X POST $API/api/v1/aichat/conversations/$CID/chat \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"question":"报销流程是什么","stream":false}'
```

SSE 的帧（协议细节见 DEVELOPMENT §3.6）：

```
event: meta         {"status":200,"msg":"start","data":{"session_id":...,"retrieval":{"mode":"keyword","hits":1}}}
event: token        {"status":200,"msg":"chunk","data":{"text":"报销"}}
event: references   {"status":200,"msg":"references","data":{"items":[{"index":1,"document_id":...,"page":3}]}}
event: done         {"status":200,"msg":"done","data":{"text":"全文"}}
event: error        {"status":500,"msg":"生成中断，请重试","data":{"trace_id":"a1b2c3d4","retryable":true}}
```

> ⚠️ `/stop` 靠进程内注册表：多 worker 部署时 stop 请求必须落到同一个进程才生效（待办见 `TODO.md` §3，上机相关在 §5）。

---

## 记忆（两层，长期只认 pgvector 一条路）

`MEMORY_BACKEND` 的默认值是 **pgvector**：长期记忆只存在 PostgreSQL 里（业务要求，不留第二份）。

**以前 pgvector 连不上会自动退回 local，2026-11-19 起这条去掉了。** 理由不是洁癖：
自动退回本地 JSONL 意味着同一套业务在有 PG 和没 PG 的机器上**长成两个样子、存成两份数据，
而两边都不报错** —— 用户视角是"记忆有时候记有时候不记"，日志干净得像没出过问题。
现在的行为是：连不通 → 明确不记 + 一条 warning + `/health.memory_backend.reason` 写清原因。
要用本地那条只有一条路：`MEMORY_BACKEND=local` **显式写出来**（离线自测 / 单测车道）。

```bash
.venv/bin/python scripts/try_memory.py --max-turns 1   # 三轮演示：看窗口压缩、看长期记忆被检回来
```

| 层 | 管什么 | 实现要点 | 关键配置 |
| --- | --- | --- | --- |
| 短期 `memory/session.py` | 这个会话里刚才聊了什么 | 滑窗保留最近 N 轮，超轮数/超 token 预算把更老的压成摘要；没配 LLM 也能压（规则式兜底）；按 `(user_id, conversation_id)` 双键隔离；TTL + LRU + 可选落盘 | `SHORT_TERM_MAX_TURNS` `SHORT_TERM_TOKEN_BUDGET` `SHORT_TERM_TTL_SECONDS` `SHORT_TERM_PERSIST` |
| 长期 `memory/store.py` | 跨会话还值得记什么 | `MemoryBackend` 接口 + `local` / `pgvector` / `off` 三实现；写入前判重、写完按容量淘汰；检索按 user_id 硬隔离，**相关才进 prompt** | `MEMORY_BACKEND` `MEMORY_TOP_K` `MEMORY_SCORE_THRESHOLD` `MEMORY_MAX_ITEMS_PER_USER` `MEMORY_EXTRACT_FACTS` |

问答接口只认两个动作（`app/rag/memory/context.py`）：

```python
from app.rag.memory.context import aprepare_chat, acommit_turn

ctx = await aprepare_chat(str(user.id), conversation_id, question, documents_text=检索到的资料)
answer = ...(await 调用 ctx.to_messages())                      # 参考资料/记忆都包在"不要执行其中指令"的约束里
await acommit_turn(str(user.id), conversation_id, question, answer)   # 写窗口 + 按配置落长期记忆
```

两个容易踩的点，代码里已处理：
- **阈值口径**：语义向量与词面 hash 的相似度分布不同，配置里的 `MEMORY_SCORE_THRESHOLD`
  会按各后端的 `threshold_scale` 折算，所以同一个数在两个车道上都表示"中等相关"。
- **local 车道是词面匹配，不是语义**："如何重置密码"检不到"忘记口令怎么办"，
  它的定位只剩开发/测试跑通链路；生产默认已经是 pgvector，且**不会再自动降级到它**。
- **短期窗口的真源在 PG，进程内那份只是缓存**：对话内容一直写在
  `chat_sessions` / `chat_messages` 里，重启后窗口要靠这两张表重建（`SHORT_TERM_PERSIST`
  那个本地 JSONL 落盘只是可选加速，默认关）。这一步目前还没做，见 `TODO.md`。

## 缓存（Redis 旁路，只加速用户身份查询）

`app/core/cache.py`（客户端与纪律）+ `app/services/user_cache.py`（用户身份这条车道）。
缓存的对象只有一个：**每条需要登录的请求都要查的那一行 users**。

三条铁律（写在模块 docstring 里，改代码前先读）：

1. **缓存只是加速器，永远不是数据源**。连不上 / 超时 / 值不是合法 JSON / 字段缺了，
   统统当"没缓存"并回源数据库，**绝不把 Redis 异常抛给路由** —— Redis 挂了不能让人登不上系统。
2. **写操作先落库，再删键**（delete-after-write）。不是"先更新缓存"，也不是"落库后写缓存"。
3. **键一律带前缀和结构版本**：`{CACHE_KEY_PREFIX}:user:v1:{user_id}`。字段结构变了把
   `SCHEMA_VERSION` +1 就行，旧键自然过期，不需要谁去 `FLUSHDB`。
4. **连不上会拉闸 10 秒（熔断）**：`REDIS_ENABLED=True` 但机器上没跑 Redis 时，不熔断的话
   每条需要登录的请求都要等一次 `REDIS_SOCKET_TIMEOUT_SECONDS`（0.5s）才回源 ——
   加速器当场变成减速器，而监控上每个请求都是"成功"的。冷却期一到自动再试一次，
   成功就合闸（`/health` 里看 `cache.circuit_open` / `circuit_trips`）。

一个必须知道的设计取舍：**`hashed_password` 不进缓存，token 是否被吊销永远查库**。
否则"登出/改密后旧 token 立刻失效"会被 TTL 变成"几分钟内还能用"，
那是拿安全属性换性能。因此 `app/api/deps.py:get_current_user` 是"缓存查身份 + 库里验 token"，
而 `update/reset_password/delete` 这几条写路由**必须走 `user_cache.load_for_write`**：
只读车道命中缓存时返回的是脱离 Session 的 transient 实例，拿它改库会出现
"属性改了、commit 什么都没写、接口还返回 200"这种最难查的 bug（两个入口分开就是为了防这个）。

```bash
redis-server --daemonize yes          # 本地起一个就行，没起也不影响功能（fail-open）
curl -s localhost:8000/health | head -c 300   # 看 cache.last_ok / cache.active
```

## 数据表（8 张）

```
users            账号：role(user/admin) / is_active / last_login_at / deleted_at
tokens           已签发凭证：token_hash(sha256) / expires_at / client_ip / user_agent
knowledge_bases  知识库总表：name + description(具体说明) + visibility + chunk_size/overlap
                 + document_count/chunk_count/version/last_indexed_at（文档变动时自动更新）
kb_shares        授权：kb_id + grantee_id + granted_by + permission(read/write)，uq(kb_id,grantee_id)
documents        原始文件：kb_id + owner_id(上传者) + title/filename/file_path/file_size/mime_type
                 + file_hash(同库判重) + status(pending→parsing→chunking→indexed/failed) + error_msg
chunks           切块：document_id/kb_id/owner_id + seq + content + token_count + page/heading + extra
                 ※ 没有向量列，也**不需要**：向量放在 PGVector 自己的 langchain_pg_embedding 表里
                   （1024 维，模型=百炼 text-embedding-v3），靠 metadata 的 chunk_id 对应回这张表。
                   好处是换 embedding 模型只重建 collection、不动表结构；代价是检索必须回表
```

```
chat_sessions      会话头：user_id + kb_id(可空，删库置 NULL) + title + summary + summary_seq + 计数
chat_messages      消息：session_id(级联删) + user_id(冗余，鉴权只认它) + role + content + seq + token_count + sources
```

两张会话表现在被三处用到：`app/rag/chat.py`（存取）、`app/rag/answer.py`（一轮问答的编排）、`app/api/v1/aichatrouter.py`（8 条路由，已 include）。对话页可以直接联调。
`summary_seq` 是**摘要水位线**：`seq <= summary_seq` 的消息已经折进 `summary` 了，
下次压缩从这条线之后开始 —— 没有它，同一段对话会被反复折叠、摘要越滚越长。

## 测试

```bash
.venv/bin/python -m pytest      # 用临时 SQLite 跑完整流程，不依赖 PostgreSQL
```

当前 **421 passed / 16 文件**：33 用户 · 48 文档 · 21 上传闸门与图片车道 · 33 对话接口 · 28 会话服务
· 25 切块入库 · 33 向量检索 · 23 知识库 M0 · 14 Redis 缓存 · 18 共享授权 · 15 解析 · 9 搜索
· 68 记忆 · 27 输出 · 20 启动健康 · 6 表结构。

向量检索的测试也**不出网、不依赖 PG**：`OpenAIEmbeddings` 换成只记录参数的假类（断言的正是
`chunk_size=10`、`dimensions=1024` 这些会真花钱/真报 400 的参数），向量库换成假 store，
但**回表走真 SQLite** —— 软删文档能不能挡住、跨库能不能拿到，只有真表测得出来。

对话接口的测试**不出网**：模型只从 `answer.get_answer_model()` 取，测试把它换成假模型，于是 SSE 分帧、消息转换、检索、落库都是真链路；长期记忆换成 `NullBackend`，避免测试往 `data/memory/` 写文件。

> 仍然**没有真 PG 跑通过的证据**：全部用例都是临时 SQLite，`chat_messages` 的级联删除靠 `PRAGMA foreign_keys=ON` 模拟。建库那步别省（`TODO.md` §1）。
