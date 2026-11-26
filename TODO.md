# TODO —— 剩下的只有三类：真机验收 / 前端 / 上线前收尾

> 终局目标：后端 + 前端做完后**装进手机，成为一个能随身问资料的 agent**。
> 所以鉴权、流式、会话持久化、长任务通知这四件事按移动端标准做，不按「本机 localhost 能用」做。

**已完成并测过的不再列在本文件**：后端 **33 条端点全部实现**（用户 8 / 知识库 5 + 授权 3 / 文档 8 含图片车道
与重索引 / 对话 8 / 独立搜索 1），加切块入库、向量化入库、检索三车道（关键词/向量/混合）、两层记忆、
SSE 与结构化输出、Redis 旁路缓存、健康检查与启动建表 —— **421 passed**；
**前端第一版也已落地**（Vue3 + Vite + TS + Naive UI，6 个页面，`npm run build` 与 `vue-tsc` 全绿）。
细节与踩过的坑在 `README.md` 与 `DEVELOPMENT.md` §12，本文件只留**真没做的**。

**章节编号刻意不重排**（§2 只留一行完成标记）：README / DEVELOPMENT / 代码注释里有一批 `TODO.md §N` 的引用，
重排编号会逼着改七八处注释，而那些文件现在正在 review。以后大改结构时再一起收。

---

## 1. 真机验收：PG + pgvector + 记忆车道（0.5 天，排在所有前端工作之前）——**唯一硬阻塞**

421 条测试全绿只证明逻辑对，证明不了这套东西能在真库上跑：**现在一次都没连过 PostgreSQL**，
全部用例跑在临时 SQLite + 假 vectorstore 上。

数据库：

- [ ] `.env` 的 `DATABASE_URL` 填本机 postgres 超户的真实密码（本地就这么跑，不用另建角色；
      `.env` 已 gitignore，密码别提交）。用超户是因为 `PGVector(create_extension=True)`
      首次使用要执行 `CREATE EXTENSION vector`，普通角色做不了这步
- [ ] `createdb -U postgres -h 127.0.0.1 knowledge_base` → `scripts/init_db.py` 建 8 张表 → `psql \dt` 核对
- [ ] 扩展首次用时自动装，但要**核对一次真装上了**：`psql` 里 `\dx` 能看到 `vector`（版本应是 0.8.x）
- [ ] **真 PG 冒烟**：`DATABASE_URL` 指向 PG，把现有测试再跑一遍（现在 100% 走 SQLite）
- [ ] 验证 `with_for_update` 不丢更新：两个 session 同时 `bump_kb_version`（SQLite 直接忽略 `FOR UPDATE`）
- [ ] 故意把表删掉 → `/health` 必须 503 + 缺表清单；`DB_STRICT_STARTUP=True` 必须拒绝启动

向量库（embedding 已定：阿里云百炼 `text-embedding-v3` / 1024 维 / 单请求 10 条，四条硬约束写在
`.env.example` 与 `README.md` 里，代码里都有校验，这里不再重复论证）：

- [ ] `.env` 填 `EMBEDDING_API_KEY` → `scripts/index_documents.py --check` 该报"可以跑"
- [ ] **入库真跑一次**：`--all` → `--kb <id> --query 「资料里的原话」 --lane vector`
      看到相似度分数 + 出处就对；再 `--lane hybrid` 对比一次召回差别
- [ ] 上面验收通过后才把 `RETRIEVAL_LANE` 从 `keyword` 切到 `vector`（或 `hybrid`）。
      ⚠️ 切完 `sources[].score` 的量纲会变（词频分 → 0~1 余弦相似度），前端别拿它做绝对阈值判断
- [ ] `RETRIEVAL_MIN_SCORE` 现在默认 0（不过滤）。真资料灌进去之后按召回质量调，不要拍脑袋
- [ ] `MEMORY_BACKEND=pgvector`（现在已是默认）的**集成验证**：跨会话还记不记得、
      用户之间检不检得到对方的记忆、连不上 PG 时是不是真的"不记 + 给原因"而不是退回本地文件
- [ ] 万一"私有资料不上第三方"成了硬要求：换本地 Ollama `bge-m3`（1024 维，同一套兼容代码），
      改 `EMBEDDING_BASE_URL/MODEL/DIMS` 三个值 + **清空 collection 全库重跑**（维度相同 ≠ 向量空间通用）
- 验收：`uvicorn app.main:app` 起来、`/docs` 试通登录→建库→上传→提问、`\dt` 八张表在、
      chunks 有向量、删表后 `/health` 报 503

## 2. ~~M0 知识库接口~~ —— 已完成（2026-11-19），编号保留见文件头

5 条路由 + `kb_crud` 四个方法 + 删库收磁盘与向量，23 条用例。写在这里只是占位，别去别处找。

## 3. 后端剩下的尾巴（都不阻塞前端；**不是"没写完"，是故意留给 worker 化 / 上线前的**）

- [ ] **断线续传**：`Last-Event-ID` → 会话已落库，所以先判断"这轮答完没有"：答完了让前端读
      `/messages` 补齐，没答完再考虑重放 token。手机切后台必然打断流，这是对话页上线前最后一个洞
- [ ] **重启后短期窗口从 `chat_messages` 重建**：窗口对象现在是进程内缓存，重启即空，而对话内容
      一直躺在 PG 里 —— 用户视角是"聊到一半它忘了前面"。手机切后台/杀进程必踩
- [ ] 启动时**捡停在 `parsing/chunking` 的文档** + 后台任务可观测性（现在进程内任务崩了只有日志）
      —— 这两条和 worker 化是同一件事，别单独做半套
- [ ] `/stop` 是进程内注册表（`app/rag/generation.py`），**多 worker 不共享** → 粘性连接或 Redis 广播
- [ ] 登录/注册的**失败锁定与限流**：直接基于已有的 `app/core/cache.py` 加计数器（Redis 已经在了），
      不必引 slowapi。注册接口仍会暴露"邮箱已被注册"也在这一步一起处理
- [ ] M1 遗留：一次性清理脚本（废弃布局 `data/kb/...` 的历史文件，`/status` 扫不到它们）

## 4. M7 前端 —— 第一版已实现（2026-11-26），剩联调与壳

`frontend/src/`：api 层 4 个文件 + 6 个页面（登录 · 知识库 · 文档 · 授权 · 对话 · 搜索 · 我的）+ 4 个组件。
栈按 §13 的拍板落地：vue-router / pinia / naive-ui / @tanstack/vue-query / marked + dompurify。

已实测：`npm run build` 通过、`npx vue-tsc --noEmit` 干净、`vite` dev server 起得来（本机 node v20.19.2
跑得动 vite 8，`package.json` 里 engines 写的 >=22.18 是模板带的，不影响）。

还没做的（按值得做的顺序）：

- [ ] **和真后端跑一遍全链路**：建库 → 上传 → 看到 indexed → 提问 → 带来源的流式答案。
      前端现在的一切证据都是"编译期"的，没有一条请求真的打到过 8000 端口
- [ ] 类型层换成生成物：`.venv/bin/python scripts/export_openapi.py --types` 出 `schema.d.ts`，
      `client.ts` 改用 `openapi-fetch` —— 现在 `src/api/types.ts` 是手写的，
      后端改字段时前端**不会**编译报错，只会运行时读到 undefined（这是当前最大的类型债）
- [ ] 上传进度：`fetch` 拿不到进度事件，大文件要显示进度得换 XHR（或分片），现在只有 loading 态
- [ ] 会话历史翻页：现在一次拉 100 条，长会话要用 `before_seq` 游标做"加载更早"
- [ ] 图片资源的预览：`stored` 那条车道能下载能列，但详情页不显示图本体（要 `URL.createObjectURL` 那套）
- [ ] `--host 0.0.0.0` 已经开了，但**手机浏览器实测**还没做（这是 §5 M9 的前置，顺序不能反）
- 验收：桌面上全程不碰 `/docs` 走通那条链路 + 手机浏览器同一套走通

## 5. M8 Agent 化 + M9 上机

**M8**（现在是坏的草稿：`create_agent(model="deepseek:...")` 需要 `langchain-deepseek`，
或改传 `llm.get_chat_model()` 实例）：

- [ ] 工具注册表 `app/rag/tools.py`：`kb_list` / `kb_search`(带 kb_id 范围) / `document_read` /
      `memory_read` / `memory_write` / `document_upload_hint`，每个都是已有 service/crud 的薄封装，
      参数用 JSON Schema 描述，模型只能选、不能自由拼 SQL 或路径
- [ ] **铁律：工具一律以 `current_user` 身份执行，参数里绝不接受 `user_id`**；权限继续走 `kb_access`。
      删库这类破坏性动作必须前端二次确认，不许自动执行
- [ ] `chat_sessions` 加 `agent` 列（见 §7）并把 `/agents` 从配置常量变成真选择（现在选了也不落库）
- [ ] 可观测：每轮记录用了哪些工具、召回哪些 chunk、token 用量（进 `chat_messages`）
- 验收：问「上周定的报销标准是什么」→ 它自己检索 + 读文档 → 带来源作答；问「把我库删了」→ 要求确认

**M9**（先决条件不是壳，是后端能被手机访问）：

- [ ] `uvicorn --host 0.0.0.0` + 防火墙放行；`CORS_ORIGINS` 从 `*` 收成具体 origin
      （`http://localhost` Android / `capacitor://localhost` iOS / 部署域名）。
      ⚠️ `main.py` 的 `allow_credentials="*" not in origins`：用 `*` 时凭据会被静默关掉
- [ ] **HTTPS**：自签证书在 WebView 里要靠原生信任链（麻烦）→ `tailscale serve` / Caddy / LE DNS-01
- [ ] 反代：SSE 必须 `proxy_buffering off` + `proxy_read_timeout ≥ 300s`
- [ ] Capacitor：`npm i @capacitor/{core,cli,android}` → `cap init` → `cap add android` → `cap open android`
- [ ] 四座桥逐个实测：① 文件/相册上传（`@capacitor/filesystem`，对应后端 multipart 与**两套白名单**：
      文档 `UPLOAD_MAX_SIZE_MB` / 图片 `IMAGE_MAX_SIZE_MB`，超限 413、后缀骗人 400）
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
- [ ] 登录防护：失败锁定 + 限流（见 §3，用已有的 cache.py 做计数）
- [ ] 缓存的连带影响要一起想清楚：`USER_CACHE_TTL_SECONDS` 之内被管理员禁用/删除的用户，
      身份字段可能还是旧的（token 吊销那关不受影响，仍查库）。真要即时生效就"禁用时删键 + 短 TTL"
- [ ] 软删 + 冷静期恢复（`users.deleted_at` 列已有，注销现在走硬删）
- [ ] 审计日志：改密 / 注销 / 管理员列表 / 授权变更
- [ ] 公网暴露前：`SECRET_KEY` 换随机、`APP_DEBUG=False`、`/docs` 关闭、上传目录权限与磁盘配额；
      **数据库账号从超户换成专用角色**（只授 `knowledge_base` 这一个库，扩展安装留给人工执行）
- [ ] 日志结构化 + 请求 ID；错误日志不含密码/token，LLM 日志不写用户资料原文
- [ ] Docker：`pgvector/pgvector:pg17` + api + worker + redis，数据卷持久化
- [ ] CI：GitHub Actions 跑 `pytest` + `~/.local/bin/uvx ruff check`（venv 里没装 ruff；
      老代码有既有基线问题，先只卡新增文件）
- [ ] 脚本补齐：`scripts/reindex.py`、`backup.sh`、`create_admin.py`

## 7. 表结构还缺什么（只有两项，都不急）

- [ ] `password_reset_tokens`（配合 §6 的"忘记密码"一起做）
- [ ] `chat_sessions.agent`（配合 §5 M8）
- 已决定**不加** `chunks.embedding` 列：文档向量存在 PGVector 自己的 `langchain_pg_*` 两张表里，
  靠 metadata 的 `chunk_id` 对应回 chunks 行。好处是换 embedding 模型只重建 collection、不动表结构；
  代价是检索必须回表（已实现）。要真加这列，先回答"两份向量数据谁是真的"。

## 8. P2 暂缓（需要决策或外部依赖，不是写代码的事）

rerank（多一次模型调用，先等真资料跑起来再评估值不值）、查询改写（HyDE）、图片 OCR
（`stored` 那批资源接上 OCR/多模态之后才推进解析流）、表格结构化、`.doc/.ppt` 预转（要 LibreOffice）、
语音输入与答案朗读（要 ASR/TTS 厂商）、知识库邀请链接/加入申请（**要新表**）、标签与目录树、
批量导入（同库 sha256 判重已有）、引用高亮与段落定位（`sources` 已带 `chunk_id/page/heading`，
差前端渲染）、token 计费统计、离线包（常用库缓存到手机本地）。

---

**执行顺序**：§4 前端与真后端联调（页面已经有了，现在缺的是"跑通一次"）→ §1 真 PG/向量库验收 →
§3 断线续传 / 短期窗口重建 → §5 M8/M9 上机 → §6-§7 收尾。
每完成一个里程碑：`.venv/bin/python -m pytest` 全绿 + 更新本文件 + 一次 commit（push 你自己来）。
