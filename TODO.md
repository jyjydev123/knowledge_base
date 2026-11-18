# TODO —— 从后端主线一路推到「手机里的 Agent」

> 终局目标：后端 + 前端做完后**装进手机，成为一个能随身问资料的 agent**。
> 所以鉴权、流式、会话持久化、长任务通知这四件事按移动端标准做，不按「本机 localhost 能用」做。

**已完成并测过的不再列在本文件**（用户 / 知识库授权 / 文档 / 切块入库 / 会话落库 / 对话路由 /
两层记忆 / SSE 与结构化输出 / 检索关键词车道 / 健康检查与启动建表 —— 共 25 条路由、321 passed，
细节看 `README.md` 与 `DEVELOPMENT.md` §12）。本文件只留两类：**还没做的**，和
**代码写好但只有 SQLite 证据、必须等真 PG / 向量库才能收口的**。

---

## 1. 先打通数据库与向量库（0.5 天，排在所有前端工作之前）

代码全绿只证明逻辑对，不证明这套东西能在真库上跑：并发、扩展、连接串、向量表都没被真验证过。

- [ ] `.env` 的 `DATABASE_URL` 换成 `~/.pgpass` 里的真实密码（别用超户跑应用：建 `knowledge_base` 角色，只给它那一个库）
- [ ] `createdb -U postgres -h 127.0.0.1 knowledge_base` → `scripts/init_db.py` 建 8 张表 → `psql \dt` 核对
- [ ] `CREATE EXTENSION vector;`（pgvector 0.8.7 已装但没执行过），然后 `MEMORY_BACKEND=pgvector` 跑一遍记忆读写
- [ ] **真 PG 冒烟**：`DATABASE_URL` 指向 PG 再跑一遍现有 321 条测试（现在 100% 走临时 SQLite）
- [ ] 验证 `with_for_update` 并发不丢更新：两个 session 同时 `bump_kb_version`
      （SQLite 直接忽略 `FOR UPDATE`，这条路径至今没被真验证过）
- [ ] 故意把表删掉 → `/health` 必须 503 + 缺表清单；`DB_STRICT_STARTUP=True` 必须拒绝启动
- [ ] 文档向量化入库（**代码就绪、一次都没连库跑过**）：定 embedding 厂商/模型/维度 →
      `scripts/index_documents.py --all` → `--query` 看召回 → `--check` 看库与 collection 状态。
      ⚠️ **维度一旦建列就不能改**，换模型要连维度带 collection 一起换，所以这一步先拍板再动手
- [ ] `MEMORY_BACKEND=pgvector` 的**集成测试**（现在只测了不需要连接的部分：DSN 换算 / 懒构造 / 阈值折算 / 降级）
- 验收：`uvicorn app.main:app` 起来、`/docs` 试通登录+上传+提问、`\dt` 八张表在、chunks 有向量、删表后 `/health` 报 503

## 2. M0 知识库接口（0.5 天）——**唯一的硬阻塞**

`documents.kb_id` 是 NOT NULL，但没有任何 HTTP 途径能创建知识库 → 前端所有页面都是空壳，
`/documents/upload` 和 `chat` 的 `kb_id` 都无来源（现在只能靠脚本建库联调）。

- [ ] `kbrouter.py` 补 5 条：`POST /kb`、`GET /kb/list?scope=owned|shared|all`、`GET /kb/detail`、
      `POST /kb/update`、`DELETE /kb/{kb_id}`；`kb_crud` 补 `create_kb/update_kb/delete_kb`
- [ ] **删库要收磁盘**：`remove_kb_directories_if_empty` 目前只被文档删除路径调用，文档级联删完之后再清目录，
      别忘了 `data/share/{kb_id}/` 那个落点也要一起收
- [ ] `GET /kb/list` 同时返回 owned + shared，并标出**每条的 permission（read/write）**——移动端靠它决定按钮可用性
- [ ] `kb_schemas.py`：`KbCreate(name, description, visibility?, chunk_size?, chunk_overlap?)` /
      `KbOut`（含 `document_count/chunk_count/version/last_indexed_at/permission`）
- [ ] 越权口径沿用 `kb_access`：无 read → 404，能读不能写 → 403
- 验收：建库 `version=1` → 传文档 `version=2`；B 能在 `/kb/list` 看到 A 共享给他的库；A 改不了 B 的库

## 3. 后端剩下的尾巴（都不阻塞前端）

- [ ] **`POST /documents/reindex/{id}`**：`failed` 的文档现在没有重跑入口
- [ ] 后台任务可观测性：进程内任务崩了只有日志；**重启时停在 `parsing/chunking` 的文档没人捡**（worker 化时一起做）
- [ ] **断线续传**：`Last-Event-ID` → 会话已落库，所以先判断「这轮答完没有」：答完了让前端读 `/messages` 补齐，
      没答完再考虑重放 token。手机切后台必然打断流，这是对话页上线前最后一个洞（排 M7 之后、M9 之前）
- [ ] `/stop` 是进程内注册表（`app/rag/generation.py`），**多 worker 不共享** → 粘性连接或 Redis 广播
- [ ] `POST /search` 独立搜索接口（前端要做纯搜索框时再加）。检索实现落在 `app/rag/retrieval.py`：
      换**向量车道**时只动内部，返回形状 `RetrievedContext{text,sources,mode}` 不变，
      ⚠️ 越权那几条测试（跨用户搜不到、软删文档不参与、授权被收回就查不到）**一行都不许改**
- [ ] M1 遗留：一次性清理脚本（废弃布局 `data/kb/...` 的历史文件，`/status` 扫不到它们）；
      上传大小限制从路由常量 20MB 提成配置项（手机端传大文件前先定）

## 4. M7 前端（1.5 天）——Vue 3 + Capacitor 已拍板，还没动代码

`frontend/` 目前还是 vite 空模板（只有 `App.vue` + `main.ts`，依赖只有 `vue`）。
选型结论与理由（为什么 Capacitor 而不是手写壳 / Tauri）见 `DEVELOPMENT.md` §13，这里只留待办。
本机 node v20.19.2 < `frontend/package.json` 要求的 ≥22.18，且没装 pnpm —— 动手前先升。

- [ ] 依赖：`vue-router` + `pinia` + `@tanstack/vue-query` + `naive-ui` + `marked` + **`dompurify`**
      （LLM 输出直接 `v-html` = 开放 XSS，这条不是可选项）+ `openapi-typescript` + `openapi-fetch`
- [ ] `scripts/export_openapi.py`：离线把 schema dump 成 `frontend/src/api/schema.d.ts`（别让前端请求运行中的后端）
- [ ] 统一封装：按 `{status,msg,data}` 解包，`status!==200` 抛错并展示 `msg`；401 跳登录
- [ ] SSE 封装：`fetch` + `ReadableStream`（`EventSource` 不能带 Authorization 头），按
      `meta/token/references/done/error` 分流，`done` 用整段文本落定气泡，`error` 显示 `msg` + `trace_id`
- [ ] 开发代理 `vite.server.proxy`：`/api` → `127.0.0.1:8000`（免 CORS）。⚠️ 壳里没有这层代理
- [ ] 页面顺序：① 登录/注册 ② 知识库列表(owned+shared+permission 徽标)+建库 ③ 文档管理(上传/状态轮询/下载/删除)
      ④ 共享授权面板 ⑤ 对话页(流式 + 引用展开 + 会话侧栏)
- 验收：桌面上「建库 → 上传 → 看到 indexed → 发问 → 带来源的流式答案」全程不碰 `/docs`

## 5. M8 Agent 化 + M9 上机

**M8**（现在是坏的草稿：`create_agent(model="deepseek:...")` 需要 `langchain-deepseek`，或改传 `get_chat_model()` 实例）：

- [ ] 工具注册表 `app/rag/tools.py`：`kb_list` / `kb_search`(带 kb_id 范围) / `document_read` /
      `memory_read` / `memory_write` / `document_upload_hint`，每个都是已有 service/crud 的薄封装，
      参数用 JSON Schema 描述，模型只能选、不能自由拼 SQL 或路径
- [ ] **铁律：工具一律以 `current_user` 身份执行，参数里绝不接受 `user_id`**；权限继续走 `kb_access`。
      删库这类破坏性动作必须前端二次确认，不许自动执行
- [ ] `chat_sessions` 加 `agent` 列并把 `/agents` 从配置常量变成真选择（现在选了也不落库）
- [ ] 可观测：每轮记录用了哪些工具、召回哪些 chunk、token 用量（进 `chat_messages`）
- 验收：问「上周定的报销标准是什么」→ 它自己检索 + 读文档 → 带来源作答；问「把我库删了」→ 要求确认

**M9**（先决条件不是壳，是后端能被手机访问）：

- [ ] `uvicorn --host 0.0.0.0` + 防火墙放行；`CORS_ORIGINS` 从 `*` 收成具体 origin
      （`http://localhost` Android / `capacitor://localhost` iOS / 部署域名）。
      `main.py` 的 `allow_credentials="*" not in origins` 意味着改用 cookie 时 `*` 会静默关掉凭据
- [ ] **HTTPS**：自签证书在 WebView 里要靠原生信任链（麻烦）→ `tailscale serve` / Caddy / LE DNS-01
- [ ] 反代：SSE 必须 `proxy_buffering off` + `proxy_read_timeout ≥ 300s`
- [ ] Capacitor：`npm i @capacitor/{core,cli,android}` → `cap init` → `cap add android` → `cap open android` 出 debug APK
- [ ] 四座桥逐个实测：① 文件/相册上传（`@capacitor/filesystem`，对应后端 multipart 与白名单）
      ② 返回键（拦 `webview.goBack()`）③ 存储持久（token 放 `Preferences` 而不是 `localStorage`，
      否则系统清缓存 = 用户莫名掉登录）④ 通知（解析/重索引/长答案完成）
- [ ] 键盘与滚动：`windowSoftInputMode="adjustResize"` + 输入框高度用 `visualViewport`
      —— 聊天类 WebApp 最高频的真机 bug
- [ ] debug 构建开 `setWebContentsDebuggingEnabled(true)`，否则手机上出了事两眼一抹黑
- [ ] refresh token（移动端刚需，24h 强制重登在手机上不可接受）；`min_client_version` 上报
- 验收：**手机浏览器**先跑通 §4 那条链路，再侧载 APK 跑同一条（顺序不能反）

## 6. P1 上线前必须过一遍

- [ ] **alembic**：ORM + `init_db.sql` 两份真源迟早漂移（`--snapshot` 是快照不是迁移）
- [ ] 忘记密码（现在 `reset_password` 要求已登录，忘了就没路）→ 邮箱验证码 + `password_reset_tokens` 表 + SMTP
- [ ] 登录防护：失败锁定 + 限流（`slowapi`/Redis）；注册接口仍会暴露「邮箱已被注册」
- [ ] 软删 + 冷静期恢复（`users.deleted_at` 列已有，注销现在走硬删）
- [ ] 审计日志：改密 / 注销 / 管理员列表 / 授权变更
- [ ] 公网暴露前：`SECRET_KEY` 换随机、`APP_DEBUG=False`、`/docs` 关闭、上传目录权限与磁盘配额
- [ ] 日志结构化 + 请求 ID，错误日志不含密码/token，LLM 日志不写用户资料原文
- [ ] Docker：`pgvector/pgvector:pg17` + api + worker + redis，数据卷持久化
- [ ] CI：GitHub Actions 跑 `pytest` + `~/.local/bin/uvx ruff check`（venv 里没装 ruff；老代码还有基线问题，先只卡新增）
- [ ] 脚本补齐：`scripts/reindex.py`、`backup.sh`、`create_admin.py`

## 7. 表结构还缺什么

- [ ] `chunks.embedding vector(N)`：还没加。当前文档向量存在 PGVector 自己的两张表里，靠 metadata 的
      `chunk_id` 对应回来；要不要在业务表开这列取决于 §1 的维度拍板。文档向量与记忆向量分 collection
      （`kb_documents` / `long_term_memory`），同一套 `langchain_pg_*` 表 —— 正因如此**任何重建只能按
      `document_id` 精确删，绝不能 `drop_tables()`**（那会把长期记忆一起删掉）
- [ ] `password_reset_tokens`（见 §6）
- [ ] `chat_sessions.agent`（见 §5）

## 8. P2 暂缓

rerank 与混合检索（BM25/tsvector + 向量）、查询改写（HyDE）、分块策略可配置、图片 OCR、
表格结构化、`.doc/.ppt` 预转、语音输入与答案朗读、知识库邀请链接/加入申请、标签与目录树、
批量导入与去重、引用高亮与段落定位、token 计费统计、离线包（常用库缓存到手机本地）。

---

**执行顺序**：§1 数据库/向量库 → §2 M0（前端唯一阻塞）→ §4 前端 ①-④ → 前端 ⑤ 对话页 →
§3 断线续传 → §5 M8/M9 → §6 收尾。
每完成一个里程碑：`.venv/bin/python -m pytest` 全绿 + 更新本文件 + 一次 commit（push 你自己来）。
