# 运维与排障

> 面向"已经跑起来了，接下来怎么活着"。部署方式见 `docs/DEPLOYMENT.md`，测试口径见 `docs/TESTING.md`。

## 1. 日常命令速查

```bash
./start.sh check                                  # 配置体检（11 项，只读配置不连服务）
curl -s localhost:8000/health | jq .data          # 真连一次库 + 四节车道状态
.venv/bin/python scripts/init_db.py --check       # 表结构 vs models.py 是否漂移

# 索引
.venv/bin/python scripts/index_documents.py --check
.venv/bin/python scripts/index_documents.py --kb <kb_id>
.venv/bin/python scripts/index_documents.py --all --limit 500
.venv/bin/python scripts/index_documents.py --kb <id> --query 报销 --lane vector   # 线上同一条检索代码

# 磁盘
.venv/bin/python scripts/prune_data.py            # 对账（默认 dry-run）
.venv/bin/python scripts/prune_data.py --apply    # 删孤儿文件 + 收空目录
du -sh data/* var/* ; df -h .

# 数据库
psql -U postgres -h 127.0.0.1 -d knowledge_base -c '\dt'
psql -U postgres -d knowledge_base -c "select status, count(*) from documents group by status;"
psql -U postgres -d knowledge_base -c "select count(*) from chunks;"
psql -U postgres -d knowledge_base -c "select pg_size_pretty(pg_database_size('knowledge_base'));"

# Redis
redis-cli ping ; redis-cli info stats | grep -E 'keyspace_|instantiated'
redis-cli --scan --pattern 'kb:user:*' | head
```

## 2. `/health` 怎么读

```json
{ "status": "ok", "database": {...}, "memory_backend": {...}, "vision": {...}, "cache": {...} }
```

| 节 | 看什么 | 注意 |
| --- | --- | --- |
| `database` | `ok` / `missing_tables` / `dialect`；`ephemeral=true` 表示连的是内存库（测试车道），**不代表持久化状态** | 状态码 503 是给编排系统看的，body 只给类别不给连接串（里面有密码） |
| `memory_backend` | `configured` / `ok` / `reason` | `reason` 说清"为什么不记"，比"看起来在跑"重要 |
| `vision` | `configured`(模型名或 `off`) / `ok` / `reason` / `scanned_pdf` | 没配 key 时图片会安静地停在 `stored`，这里是你唯一的线索 |
| `cache` | `client_ready`（建了客户端）与 `last_ok`（真通过一次）**是两件事** | Redis 挂了不改 HTTP 状态码（fail-open），别指望探活把它摘掉 |

## 3. 备份与恢复（**没演练过的备份等于没有备份**）

```bash
# 备份：库 + 原件 + 配置，三样缺一不可（只有库 = 文件全丢，只有 data/ = 索引全丢）
pg_dump -U postgres -h 127.0.0.1 -Fc knowledge_base > /backup/kb-$(date +%F).dump
tar -czf /backup/kb-files-$(date +%F).tgz data/ var/ .env
# cron 示例（每天 03:20，保留 14 天）
20 3 * * *  cd /srv/knowledge_base && pg_dump -U postgres -Fc knowledge_base > /backup/kb-$(date +\%F).dump && find /backup -name 'kb-*.dump' -mtime +14 -delete
```

```bash
# 恢复
dropdb --if-exists -U postgres knowledge_base && createdb -U postgres knowledge_base
pg_restore -U postgres -d knowledge_base /backup/kb-2026-11-26.dump
tar -xzf /backup/kb-files-2026-11-26.tgz -C /srv/knowledge_base
.venv/bin/python scripts/init_db.py --check      # 确认表齐（pg_restore 建的是备份那一刻的结构）
.venv/bin/python scripts/prune_data.py           # 确认磁盘↔库对得上（漂移=0 才算恢复干净）
```

恢复后必查的三条：`/health` 是 ok、`documents` 状态分布合理、抽一个库问一句话能得到带来源的回答。

## 4. 容量怎么估（先跑一段时间再决定收多紧）

```sql
select (select count(*) from documents) as 文档, (select count(*) from chunks) as 切块,
       (select coalesce(sum(token_count),0) from chunks) as token,
       pg_size_pretty(pg_total_relation_size('chunks')) as 切块占用;
```
- **磁盘原件**：平均一份文档几百 KB～20MB（`UPLOAD_MAX_SIZE_MB=20` 是硬上限，手机相册图 8MB）；
- **chunks 表**：正文文本，量级 = 全部资料字符数 ÷ 块大小，`chunk_overlap` 会再放大 10~20%；
- **向量**：1024 维 float4 ≈ **4KB/条** + 索引开销，1 万条切块就是几十 MB 起，远小于你的文本表；
- **调用量**：embedding 一个请求最多 10 条（`EMBEDDING_BATCH_SIZE`，百炼硬限制），
  全库重建 = 切块数 ÷ 10 次请求；视觉描述 = 每张图 1 次请求，扫描件每页 1 次（所以默认关）。

## 5. 故障对照表

| 现象 | 最可能的原因 | 一条命令定位 | 处置 |
| --- | --- | --- | --- |
| 前端整屏 404 / 接口全红 | `VITE_API_BASE` 只给了主机名没带前缀，或改完 env 没重新 build（vite 是**编译期**注入） | `grep VITE_API_BASE frontend/.env*`；浏览器 Network 看请求 URL | dev 用 vite 代理留空；壳/生产填绝对地址 |
| 桌面 `PORT=9000` 起后端后页面全报错 | vite 的 `/api` 代理目标写死 `127.0.0.1:8000` | `./start.sh` 启动时会警告 | 用默认端口，或改 `frontend/vite.config.ts` 的 target |
| 流式"等十几秒然后一次性全出来" | 反代开了缓冲（`proxy_buffering on`） | 直连 8000 再试一次，能逐字就是代理的锅 | `proxy_buffering off` + `proxy_read_timeout ≥ 300s` |
| 上传报 413 | 真超限（文档 20MB / 图片 8MB，在流式读的过程中判） | 看返回 msg 里的上限 | 压缩后重传，或明确调 `UPLOAD_MAX_SIZE_MB`（同时想清楚磁盘配额） |
| 上传报 400「后缀与内容不符」 | 改名的假文件，或 webp 只对了 RIFF 没对 WEBP | 后端日志 `file_types` 的嗅探记录 | 让用户从原始 App 导出 |
| 文档一直 `pending` / 停在 `chunking` | 后台任务是**进程内**的：进程重启就没人捡 | `psql -c "select id,status from documents where status <> 'indexed';"` | 点 `POST /documents/reindex/{id}`；启动捡漏还没做（TODO §3） |
| 扫描件 PDF 变 `failed` | 设计内：pypdf 提不到字 | 看 `error_msg`（会写"可能是扫描件"+ 怎么开兜底） | `uv add pymupdf` + `SCANNED_PDF_VISION=True` 后 reindex |
| 图片传上去是 `stored` 不是 `indexed` | 视觉车道不就绪（没 key / `IMAGE_PARSE_ENABLED=False`） | `curl -s localhost:8000/health \| jq .data.vision` | 配 key 后对那张图点 reindex（不用删了重传） |
| 回答"发出去没反应，刷新才看到问题" | 已知前端 bug（`activeId` 赋值触发拉空历史 + 原始对象不触发渲染） | 拉最新代码 | **已修**（`ChatView.send()`，见 TODO §4）；复验步骤 TESTPLAN §1.6 |
| 提问永远没有资料 | 检索范围是空的：会话没绑库 / 库还没 indexed / `RETRIEVAL_LANE` 还是 keyword 而库里有向量 | meta 帧 `retrieval.mode` 是 `no_scope` 还是 `none` | `no_scope`→选库；`none`→零命中不是故障，别乱调阈值 |
| 语义检索完全没效果 | 向量从没入库过（`--all` 没跑） | `psql -c "\dt"` 有没有 `langchain_pg_embedding` | 先 `index_documents.py --check` 再 `--all` |
| `/health` 503 报缺表 | 没跑建表 / 连错库 | 看 `database.missing_tables` | `scripts/init_db.py`；确认 `DATABASE_URL` 的库名 |
| 手机连不上电脑 | 不是后端问题就是网络：`HOST` 必须是 `0.0.0.0`、防火墙、不同网段 | 手机浏览器直接开 `http://<电脑IP>:8000/health` | 通则再开前端；`./start.sh` 默认就是 0.0.0.0 |
| 点"停止"没反应 | 生成注册表是进程内的，多 worker 时请求落到别的进程 | 确认只起了 1 个 worker | 单 worker，或先做 TODO §3 的 Redis 广播 |
| 重启后"它忘了前面聊的" | 短期窗口在进程内（`SHORT_TERM_PERSIST=False`） | `psql -c "select count(*) from chat_messages where session_id=...;"` 内容其实都在 | 重建窗口的活儿在 TODO §3；临时打开 PERSIST 也只是半解 |
| 用户莫名掉登录 | 前端 token 存 localStorage，浏览器/WebView 清了缓存 | 看 `utils/storage.ts` 那份是不是空的 | 壳里换 `Preferences`（TODO §5 M9） |
| 百炼返回 429 / 配额 | embedding 或 vision 超量 | 后端日志里的 `Retries/429` | 降 `VECTOR_INDEX_BATCH_SIZE`、分批 `--limit`；视觉扫描件默认关就是这个原因 |
| 磁盘涨得快 | 上传没有配额（只有单文件大小上限） | `du -sh data/*` + 按用户/库排 | 上配额或收 `UPLOAD_MAX_SIZE_MB`；孤儿用 `prune_data.py --apply` 收 |

## 6. 安全应急

| 场景 | 处置 |
| --- | --- |
| `SECRET_KEY` 泄漏 / 提交进 git | 换随机值 → 所有 access token 立即失效（用户重登）；`tokens` 表里的吊销记录不受影响 |
| 某账号要立刻禁用 | 吊销该用户 token（查库那条路不受 300s 缓存影响）；身份字段在 `USER_CACHE_TTL_SECONDS` 内可能还是旧的 → 想即时就删键 `kb:user:v1:{user_id}` |
| 误删知识库/文档 | 现在是**硬删**（`uq(owner_id,name)` 不含 `deleted_at`，所以删库必须真删）。唯一出路是恢复备份 → 冷静期方案在 TODO §6 |
| `.env` 里有真 key | 已 gitignore，但**别贴进 issue/截图**；百炼控制台可即时禁用重发（明文只显示一次，丢了只能重建） |
| 公网暴露 | 优先不上公网，`tailscale serve` 就够手机用；真要上：HTTPS + 收 CORS + 关 `/docs` + 专用 DB 角色 + 登录限流（TODO §3/§6） |

## 7. 已知没做的（别当故障查）

断线续传、重启后短期窗口重建、启动捡 `parsing/chunking` 的文档、`/stop` 多 worker、
登录失败锁定与限流、alembic 迁移、忘记密码、软删冷静期、审计日志、Docker/CI ——
在 `TODO.md` §3/§5/§6 里**已撤销排期**（标题保留给引用定位，正文只剩一行说明）—— 洞照旧，只是不再规划。
