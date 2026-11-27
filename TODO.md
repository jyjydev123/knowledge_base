# TODO —— 只剩三件事：真机验收 / 前端优化 / 接钉钉

> **不再排新功能**。原来那些 backlog（后端 6 条尾巴、M8 agent 工具、M9 上机四座桥、P1 上线前、
> 表结构两列、P2 一长串）已从本文件删除，需要时从 git 历史找回（`git log -p -- TODO.md`）。
> §3 / §5 / §6 / §7 **只留一行说明、不删标题**：代码注释与 `docs/` 里有二十多处 `TODO §N` 引用，
> 标题没了那些引用就指空。⚠️ "撤销排期"不等于"问题解决了"——**那些洞还在**，只是不再规划。

已完成并测过的不列在这里：33 条端点、切块入库、向量化、检索三车道、两层记忆、SSE 与结构化输出、
Redis 旁路缓存、健康检查、**视觉车道（图片→描述）**、磁盘对账脚本、启动体检、前端第一版
—— **445 passed / 17 个测试文件**，细节与踩坑见 `README.md` 与 `DEVELOPMENT.md` §12。

---

## 1. 真机验收（**现在就是做这件事**）

逐条打勾的表在 **[docs/TESTPLAN.md](docs/TESTPLAN.md)**（`TC-*` 编号，操作/预期/实际/判定四列）。
本节只留顺序和前置，别在两个文件里各抄一遍。

```bash
sudo systemctl start postgresql@17-main        # 已确认：PG17 起着、8 张表齐、vector 0.8.7 在库内
.venv/bin/python scripts/prune_data.py --apply # 先清测试遗留，否则 §1.3 的孤儿核对全是噪声
./start.sh check                               # 11 项配置体检，全 OK 再往下
SQL_ECHO=False ./start.sh all                  # 关 SQL 回显，否则看不见"分段耗时"那行
# 浏览器 http://<本机局域网IP>:5173
```

十个组（每组 5–10 条，展开在 TESTPLAN 对应小节）：

| 组 | 验什么 | 一句话判据 |
| --- | --- | --- |
| 1.1 | 数据库与启动 | `/health` 四节读一遍；故意缺表必须 503；`FOR UPDATE` 并发不丢 version |
| 1.2 | 文档链路 | 状态机走完；日志三个耗时数记下来；闸门四条 413/400/400/409 |
| 1.3 | 磁盘↔库对账 | prune 的孤儿只该是测试垃圾；删文档/删库两处目录都收干净 |
| 1.4 | **视觉车道**（本轮新实现） | 手机拍的图 → indexed，金额日期**逐字转录**出来；没 key 时是 `stored` 不是 `failed` |
| 1.5 | 检索与向量 | `--all` 真跑一次；降级要在 meta 帧 `retrieval.fallback` 可见；全绿才切 `RETRIEVAL_LANE` |
| 1.6 | **对话与流式**（本轮修了两个前端 bug） | 新建即发即见气泡、逐字往外蹦、409/503 看得见 |
| 1.7 | 权限与共享 | `read` 上传 403；越权一律 404 不泄露"存在" |
| 1.8 | 两层记忆 | 首次写才建 `long_term_memory`；停 PG 必须"不记 + 给原因"，不许偷偷退本地文件 |
| 1.9 | 缓存与稳定性 | 停 Redis 业务照常；熔断 ~10s 自愈 |
| 1.10 | 手机浏览器 | M9 前置，顺序不能反 |

## 2. ~~M0 知识库接口~~ —— 已完成（2026-11-19），编号保留见文件头

## 3. 后端尾巴 —— 撤销排期

原 6 条（SSE 断线续传 `Last-Event-ID`、重启后短期窗口从 `chat_messages` 重建、启动捡 `parsing/chunking`、
`/stop` 多 worker、登录失败锁定与限流、`data/kb/` 清理脚本）：**清理脚本已做**（`scripts/prune_data.py`），
其余五条撤销。**行为不变，洞照旧**：断线只能靠刷新补看、重启后短期窗口是空的、`/stop` 只在单 worker 有效。
第 7 条「测试隔离 `DATA_DIR` 指到 tmp」也撤销（代价：每跑一次全量工作区多几十个孤儿目录，`prune` 收尾）。
另：`scripts/init_db.py --drop` **没有交互确认**就 `drop_all`，连错库的代价是整个库——撤销排期，但用之前自己当心。

## 4. 前端优化（保留）

`TODO §4` 原本第一、二条已做完（前端已实现 + 新建对话的两个 bug 已修，见 §1.6 与 DEVELOPMENT §12）。
剩下这五条是"能用但不够好"，值得继续做，不含新功能：

- [ ] **类型层换成生成物**：`.venv/bin/python scripts/export_openapi.py --types` 出 `schema.d.ts`，
      `client.ts` 改 `openapi-fetch`。现在 `src/api/types.ts` 是手写的 —— 后端改字段前端**不会**编译报错，
      只会运行时读到 `undefined`（当前最大的类型债）
- [ ] **上传进度**：`fetch` 拿不到进度事件，大文件（20MB 上限）只有 loading 态 → 换 XHR 或分片
- [ ] **历史翻页**：一次拉 100 条，长会话要用 `before_seq` 游标做"加载更早"
- [ ] **图片预览**：详情页现在只能下载，不显示图本体（`URL.createObjectURL` 那套）
- [ ] **流式途中切走会话**：那一路请求会跑完才放开输入框（`streaming` 占位），改成切走即 abort 或标出"在跑"
- [ ] 手机浏览器实测本身是**验收**不是优化 → 见 §1.10

## 5. ~~M8 Agent 工具 / M9 上机~~ —— 撤销排期

`app/rag/tools.py`、`chat_sessions.agent` 列、Capacitor 四座桥（文件/返回键/Preferences/通知）、
HTTPS 与反代参数、refresh token —— 全部撤销。**手机化这条路暂时停在 §1.10（浏览器实测）**。
需要时先看 git 历史，别照着这份文件开工。

## 6. ~~P1 上线前~~ —— 撤销排期

alembic、忘记密码、软删冷静期、审计日志、Docker、CI、公网暴露前的收紧清单 —— 全部撤销。
但 `docs/OPERATIONS.md` §6 与 `docs/DEPLOYMENT.md` §7 里那两条**不是排期问题而是操作问题**：
`SECRET_KEY` 是占位值就别往外网暴露；`init_db.py --drop` 敲下去不会问你第二遍。

## 7. ~~表结构还欠什么~~ —— 撤销

`password_reset_tokens` 与 `chat_sessions.agent` 都不加了（`chunks.embedding` 当初就决定不加）。
唯一新增的表结构需求挪到 §8：**钉钉身份绑定表**。

---

## 8. 接入钉钉（本文件唯一保留的新功能）

把知识库变成钉钉里能问的东西。三种玩法按投入从小到大，**先只做第 1 种**：

### 8.1 群机器人问答（先做这个）

- 走**钉钉 Stream 模式**（`dingtalk-stream` SDK 反向长连接），**不要**做 HTTP 回调：
  本项目现在没有 HTTPS 也没有公网（§5 撤销了），Stream 模式刚好绕开这一整层前置
- 群里 `@机器人 问题` → 调**已有的** `POST /api/v1/aichat/conversations/{id}/chat`
  的 `stream=false` 分支（钉钉侧没有 SSE，这条分支本来就为脚本/CI 留着）→ 回答 + `sources` 转 markdown
- **铁律：以发消息的人的身份检索。** unionId 先换成内部 `user_id`，再走 `get_current_user` 那套依赖注入
  与 `kb_access` 判定。绝不允许"机器人借用库主人身份"读全部库 —— 这是这条功能唯一会把权限做穿的地方
- 未绑定 / 无权限的库：回一句明确提示（"你还没绑定账号"/"这个问题在我的可读范围里没找到资料"），
  **不许静默不回**，也不许把别人的库名泄露出来

### 8.2 任务通知（第二步）

解析完成/失败、重索引完成、长答案完成 → 工作通知或群消息。
后端已有 `document_crud.update_status` 这个收敛点，挂一个回调即可；别在路由里散着写。

### 8.3 免登 / 扫码登录（可选，最后做）

企业应用 SSO：unionId ↔ `users.id` 绑定后免账号密码。手机端体验最好的一条，但要企业应用凭证，
依赖前两步跑顺。

### 8.4 落地要点

- 新目录 `app/integrations/dingtalk/`：`client.py`（access_token，走已有的 `core/cache.py` + Redis）
  / `stream.py`（回调订阅）/ `binding.py`（unionId↔user）。**不要**塞进 `app/rag/`
- 新表 `dingtalk_bindings(user_id, union_id, corp_id, created_at)`：唯一必需的表结构变更。
  unionId **不当** user_id 用，只做映射；`user_id` 外键指向 users，级联删除
- 并发与限流：同一会话同时只一路生成 → 复用已有的 generation registry（409 那条）；
  消息频率用 `core/cache.py` 加计数器，别引新依赖
- 配置：`DINGTALK_ENABLED` / `DINGTALK_CLIENT_ID` / `DINGTALK_CLIENT_SECRET` / `DINGTALK_ROBOT_CODE`
  / `DINGTALK_ALLOWED_CONVERSATIONS`（群白名单，默认空=全拒）/ `DINGTALK_DEFAULT_KB_ID`（可选）
  / `DINGTALK_ANSWER_TOP_K`。key 只进 `.env`
- 车道不可用（未启用/没凭证/token 取不到）时的降级口径与视觉车道一致：**明确报错 + `/health` 给原因**，
  不静默返回空答案；`/health` 加一节 `dingtalk`，`./start.sh check` 加一行

### 8.5 明确不做

通讯录与部门同步、审批流/待办、钉钉文档抓附件入库、AI 助理平台、多租户、消息卡片交互回写。

### 8.6 验收

群里 @机器人 问一句资料里有的内容 → 30 秒内收到带来源的 markdown；问一句只有第三方账号能看的库 →
答不上且不泄露库名；`DINGTALK_ENABLED=False` 时服务照常起、`/health.dingtalk.reason` 说清原因。

---

**执行顺序**：§1 从上往下打完（这是当前唯一阻塞）→ §4 挑值得的做 → §8 接钉钉。
每完成一个里程碑：`.venv/bin/python -m pytest -o addopts="" -q` 全绿 + 更新本文件 + 一次 commit。
