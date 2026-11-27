# 测试说明

> 一句话：**445 条用例跑在临时 SQLite + 假 embedding + 假模型上，全程不出网**。
> 它证明的是逻辑对，**证明不了能在真 PG / 真百炼上跑** —— 那部分在 `docs/TESTPLAN.md`（人工验收）。

## 1. 三层测试分别是什么

| 层 | 跑法 | 覆盖 | 不覆盖 |
| --- | --- | --- | --- |
| **L1 自动化**（本文件） | `.venv/bin/python -m pytest` | 路由/鉴权/权限/状态机/切块/检索逻辑/缓存/SSE 分帧/解析器/视觉车道，5 个 router 的 33 条端点 | 真 PG 的 `FOR UPDATE`、pgvector 相似度、真实模型输出质量、浏览器/手机 |
| **L2 集成验收** | `docs/TESTPLAN.md` §1.1–§1.9 手工打勾 | 真 PostgreSQL + pgvector + 百炼 embedding + 百炼 qwen-vl + Redis | 真机 UI |
| **L3 端到端** | `docs/TESTPLAN.md` §1.6/§1.10（浏览器 + 手机） | 前端全链路、流式手感、手机壳 | —— |

## 2. 怎么跑

```bash
# 日常（最常用）：安静跑，只看结论
.venv/bin/python -m pytest -o addopts="" -q

# 只跑某一文件 / 某条用例（用例名是中文，记得引号）
.venv/bin/python -m pytest -o addopts="" -q tests/test_vision.py
.venv/bin/python -m pytest -o addopts="" -q "tests/test_chat_db.py::test_超轮数后摘要落库并推进水位线"

# 定位刚写坏的那一条：-x 一红就停，--tb=short 少刷屏
.venv/bin/python -m pytest -o addopts="" -q -x --tb=short tests/test_vision.py
```

**为什么要 `-o addopts=""`**：`pyproject.toml` 的 `[tool.pytest.ini_options]` 里 `addopts = "-q --disable-warnings"`。
`--disable-warnings` 会把"测试自己制造的问题"（未 await 的协程、弃用 API、连接没关）一起吞掉——
本项目踩过两次这类坑都是靠那些不起眼的 warning 发现的。跑的时候把默认参数摘掉，看全。

**耗时**：全量约 **90–120 秒**。单文件 3–10 秒。
**必须在正常 shell 里跑**：受限沙箱里 `aiosqlite` 的线程会被永久挂住（表现为 0% 无输出），
不是测试死了，是环境不让它建 socket/线程。

## 3. 基线与环境钉子（`tests/conftest.py` 顶部）

| 钉子 | 值 | 为什么 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite+aiosqlite:///:memory:`（每条用例一个临时库文件） | 不依赖本机装没装 PG |
| `SQL_ECHO` | `False` | 否则日志被 SQL 淹掉 |
| `DOCUMENT_INDEX_ON_UPLOAD` | `False` | 测试里全局引擎是没建表的内存库，后台任务只会制造 `no such table` 噪声；索引逻辑用 `process_document(session=...)` 直接测 |
| `MEMORY_BACKEND` | `local` | pgvector 车道在测试里连不上，会自动"不记"，68 条记忆用例就变成"什么也没测"。**这个选择必须是显式的**（生产上 pgvector 连不上不许偷偷退 local，代码里正是这么定的） |
| `REDIS_ENABLED` | `False` | 本机可能装也可能没装 Redis；靠外部服务状态的测试比没写更糟。缓存行为在 `tests/test_cache_user.py` 用假客户端显式测 |
| **`LLM_API_KEY` / `EMBEDDING_API_KEY`** | **硬置空**（不是 `setdefault`） | 2026-11-26 踩的坑：`.env` 一旦填了真 key，"没配 key 就该明确报错"的用例变成 DID NOT RAISE，而短期记忆折叠那条会**真的联网调 DeepSeek**，摘要内容随机 → 断言随机失败 + 烧配额。环境变量优先级高于 `.env` 文件，空串能压住 |

需要"有 key"场景的用例，自己在用例里 `monkeypatch.setattr(settings, ...)` 造，不靠本机环境。

## 4. 写用例的六条规矩

1. **绝不出网**。唯一出网口都被显式包住：LLM 走 `app/rag/llm.py`，视觉走 `app/services/vision._invoke`，
   embedding 走假 `OpenAIEmbeddings`。新写一层外部调用时，照 `tests/test_vision.py` 的做法加一条
   **autouse 兜底**：漏打桩就直接 `AssertionError`，而不是悄悄发一次真请求。
2. **不碰真数据库/真 Redis**：连不上就明确的，就在用例里换成假对象；`/health` 之类必须保持确定性。
3. **路径必须走同一套规则**：要造磁盘文件就用 `document_storage_path()` 算，别手写字符串
   （否则测试通过而线上删不掉文件）。
4. **断言要写"为什么这么设计"**：例如意图错误的越权是 **404 不是 403**（不泄露"存在"这件事）；
   零命中**不算**检索故障、不许自动换车道。这类断言比功能本身更容易被后人"顺手改掉"。
5. **既有基线问题不许顺手修**：`app/db/crud/kb_crud.py:42` E501、`app/api/v1/documentrouter.py:292` B904、
   `tests/test_kb_share_api.py`、`tests/test_parsers.py` 的 lint 项是历史包袱，改动它们会把 review 的注意力带偏。
6. **测试隔离还没做**（TODO §3）：`DATA_DIR` 已经可配，但 5 个测试文件的期望路径还写死 `data/...`，
   所以每跑一次全量会在工作区留下几十个孤儿目录（内容是 `# t\n\nx`）。收尾靠 `scripts/prune_data.py --apply`。

## 5. 静态检查

```bash
# venv 里没装 ruff，用 uvx；只卡自己写过的文件（理由见上面第 5 条）
~/.local/bin/uvx ruff check --output-format=concise <文件列表>
~/.local/bin/uvx ruff check --fix --output-format=concise tests/test_xxx.py   # 自动修 import 排序这类
```
规则来自 `pyproject.toml`：行宽 120、`select = E,F,I,UP,B`、`ignore = B008`（FastAPI 依赖注入惯用默认值）。

## 6. 前端测试

没有单元测试框架（`vitest` 没装）。目前的证据是**编译期**的三条 + 一条人工：

```bash
cd frontend
npx vue-tsc --noEmit -p tsconfig.app.json     # 0 error 才算过
npm run build                                  # vite 构建通过
npm run dev                                    # 起得来、/ 和 /src/main.ts 都 200
```
⚠️ `src/api/types.ts` 是**手写**的（后端改字段前端不会编译报错，只会运行时 undefined）——
这是当前最大的类型债，换生成物的路子写在 `TODO.md` §4。
真实请求链路一律走 `docs/TESTPLAN.md` §1.6/§1.10 的人工验收。

## 7. 这份测试证明了什么、没证明什么

**证明了**：状态机流转、鉴权与越权、分页与参数边界、软删不污染检索、切块与 token 统计、
SSE 帧协议（含 `: ping` 心跳）、缓存旁路与熔断、判重与容量淘汰、视觉车道的每一条降级分支。

**没证明**（全部列在 `docs/TESTPLAN.md`）：pgvector 的真实相似度与距离语义、`FOR UPDATE` 的并发正确性、
百炼 embedding/vision 的真实返回质量、模型答得对不对、浏览器与手机上的手感、断线后的行为。
