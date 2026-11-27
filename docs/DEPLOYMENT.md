# 部署说明

> 目标形态只有一个：**装进手机能用的知识库 agent**。所以"部署"不是 `uvicorn` 起来就完事，
> 而是 后端常驻 + HTTPS + 手机能连 + 流式不被缓冲。四件事缺一件，手机上就是"转圈没反应"。

## 1. 依赖矩阵

| 组件 | 版本 | 必需 | 说明 |
| --- | --- | --- | --- |
| Python | 3.11+（本机 3.14.7） | ✅ | `requires-python >= 3.11` |
| PostgreSQL | 17 | ✅ | **主存储**：业务表 + 切块 + pgvector 向量 |
| pgvector | 0.8.x | ✅ | `CREATE EXTENSION vector`；首次用时代码会自动装（要超户），但**部署时人工执行一次**更稳 |
| Redis | 7/8 | 建议 | 只是用户身份旁路缓存；**挂了不影响可用性**（fail-open 回源 + 熔断） |
| uv | 任意近期版 | ✅ | 项目在 `~/.local/bin/uv`；`uv sync --group dev` 建 `.venv` |
| Node | 20+（本机 20.19.2 跑得动 Vite 8） | 前端 | `package.json` 的 `engines` 是模板带的，不用当真 |
| LLM | DeepSeek `deepseek-chat` | ✅ | 只出文字，**没有视觉输入** |
| Embedding | 百炼 `text-embedding-v3` / 1024 维 / 单请求 ≤10 条 | ✅ | 兼容模式 `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| Vision | 百炼 `qwen-vl-max` | 可选 | 图片描述车道；没 key 就退回"只存档不解析" |
| Pillow | 10+ | 可选 | 送视觉模型前缩放；没装则超限图片明确报错 |
| PyMuPDF | 1.24+ | 可选 | 只有 `SCANNED_PDF_VISION=True` 才需要 |

## 2. 端口与目录

| | |
| --- | --- |
| 后端 | `0.0.0.0:8000`（`/` 探活、`/health`、`/docs`、API 前缀 `/api/v1`） |
| 前端 dev | `0.0.0.0:5173`（vite 代理 `/api`、`/health` → 127.0.0.1:8000） |
| `data/` | **只有用户上传的原件**：`data/{user_id}/{kb_id}/{doc_id}.{ext}` + `data/share/{kb_id}/` |
| `var/` | 可重建的派生物（local 记忆自测 jsonl 等），删了不心疼 |
| `logs/` | `start.sh all` 的内联模式后端日志（`logs/api.log`、`logs/terminal.log`），已 gitignore |
| 切块/向量/记忆 | **不在磁盘上**：全在 PG（`chunks` 表、`langchain_pg_*`、`long_term_memory`） |

## 3. 从零装（干净机器，逐条能复制）

```bash
# ① 数据库
sudo apt install -y postgresql-17 redis-server
sudo apt install -y postgresql-17-pgvector   # 包名按发行版可能是 postgresql-pgvector；
                                             # 装不到就源码编：本机 0.8.7 在 /usr/share/postgresql/17/extension/
sudo systemctl enable --now postgresql@17-main
sudo -u postgres psql -c "ALTER USER postgres PASSWORD '换成你自己的';"
sudo -u postgres psql -c "CREATE DATABASE knowledge_base;"
sudo -u postgres psql -d knowledge_base -c "CREATE EXTENSION IF NOT EXISTS vector;"   # 人工执行，别赌代码自动装
psql -U postgres -h 127.0.0.1 -d knowledge_base -c '\dx'               # 应看到 vector 0.8.x

# ② 后端
cd 项目根
~/.local/bin/uv sync --group dev          # 建 .venv 并装依赖
cp .env.example .env && $EDITOR .env      # 至少要填：DATABASE_URL / SECRET_KEY / 两把 key
.venv/bin/python scripts/init_db.py       # 建表（真源是 app/db/models.py）
.venv/bin/python scripts/init_db.py --check
./start.sh check                          # 11 项配置体检（只读配置，不连服务）
./start.sh                                # 后端；或 ./start.sh all 连前端一起

curl -s localhost:8000/health | head -c 400

# ③ 前端
cd frontend && npm install && npm run dev        # 开发
npm run build                                    # 产物在 dist/（当前没有静态托管，见 §5）
```

⚠️ **别跑 `init_db.sql`**：它是 `models.py` 生成的**快照**（`--snapshot` 刷新），
`CREATE EXTENSION` 那行还是注释掉的，也不含 PGVector 自己的两张表。真源只有 `models.py`。

## 4. 配置项（`.env`，完整注释在 `.env.example`）

| 组 | 变量 | 默认 | 生产建议 |
| --- | --- | --- | --- |
| 存储 | `DATA_DIR` / `VAR_DIR` | `data` / `var` | 换成挂载盘绝对路径；注意**相对路径是相对进程 CWD**（systemd 必须写 `WorkingDirectory`） |
| 数据库 | `DATABASE_URL` | — | 上线换**专用角色**（只授本库），别用超户；超户只是为了 `CREATE EXTENSION` 那一步 |
| | `SQL_ECHO` | `False` | 生产必须 False（每条 SQL 都打，量大时它就是性能问题） |
| | `DB_STRICT_STARTUP` / `AUTO_CREATE_TABLES` | `False` / `False` | 生产 `STRICT=True`：缺表就别起来，编排系统能摘掉它 |
| 安全 | `SECRET_KEY` | 内置占位 | **必须换随机**；换了等于全员掉线，选个维护窗口 |
| | `ACCESS_TOKEN_EXPIRE_MINUTES` | `1440` | 移动端 24h 强制重登不可接受 → 上 refresh token（TODO §5 M9） |
| | `APP_DEBUG` | `True` | 生产 `False`（开着的话 500 会把异常原文吐给客户端） |
| | `CORS_ORIGINS` | `http://localhost:5173,http://127.0.0.1:5173` | 从 `*` 收成具体 origin。⚠️ `main.py` 里 `allow_credentials = "*" not in origins`，写 `*` 等于**静默关凭据** |
| 上传 | `UPLOAD_MAX_SIZE_MB` / `IMAGE_MAX_SIZE_MB` | `20` / `8` | 手机流量场景建议再收一档；超限 413 |
| | `UPLOAD_REJECT_EMPTY` | `True` | 保持 True（空文件进库永远失败） |
| LLM | `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` | — / deepseek / deepseek-chat | **模型与端点必须配套**（本项目 .env 中就中过：模型 deepseek、base 指 api.openai.com） |
| Embedding | `EMBEDDING_*`（KEY/BASE_URL/MODEL/DIMS/BATCH_SIZE） | — / 百炼 / v3 / 1024 / 10 | 换厂商=换向量空间：`DIMS` 与 `BATCH_SIZE` 有启动校验，且必须**清空 collection 全库重跑** |
| Vision | `VISION_*` + `IMAGE_PARSE_ENABLED` + `SCANNED_PDF_VISION` | 见 `.env.example` | key 留空自动复用 `EMBEDDING_API_KEY`；默认 key 两处通用 |
| 检索 | `RETRIEVAL_LANE` / `RETRIEVAL_MIN_SCORE` | `keyword` / `0` | 向量验收通过才切 `vector`/`hybrid`；阈值按真资料调，别拍脑袋 |
| 记忆 | `MEMORY_BACKEND` | `pgvector` | 保持默认。**连不上 PG 不自动退 local**（两份数据两套行为是最难查的一类问题） |
| | `SHORT_TERM_*` | 见配置 | `SHORT_TERM_PERSIST=False` 时窗口只在进程内 → 重启即丢（TODO §3） |
| 缓存 | `REDIS_ENABLED` / `REDIS_URL` / `USER_CACHE_TTL_SECONDS` | True / localhost:6379/0 / 300 | 缓存只加速鉴权，**吊销 token 永远查库** |
| 索引 | `DOCUMENT_INDEX_ON_UPLOAD` | `True` | 换成 worker 队列时设 False，改由消费者调 `process_document` |

## 5. 四种运行形态

### A. 本机 / 局域网开发（就是现在）

```bash
./start.sh all              # 后端另开终端窗口 + 前端 5173
NOTERM=1 ./start.sh all     # 没图形会话（ssh/tty）时：日志合流到当前终端
NO_RELOAD=1 ./start.sh      # 手机联调时关热重载：改一行代码就断一次 SSE，很干扰判断
```
前端在 dev 下走 vite 代理，**不用管 CORS**；`VITE_API_BASE` 留空即可。

### B. 服务器常驻（systemd）

**⚠️ 必须单 worker（`--workers 1`）**：两样状态是进程内的——
① 生成注册表（`/stop` 靠它，多 worker 时 stop 请求落到别的进程就失效）
② 短期记忆窗口（多 worker = 每个进程一份窗口，答着答着就"忘了前面"）。
要上多 worker，先把 TODO §3 的 Redis 广播 / 窗口重建做掉。

```ini
# /etc/systemd/system/kb-api.service
[Unit]
Description=knowledge_base API
After=network.target postgresql.service redis.service

[Service]
WorkingDirectory=/srv/knowledge_base          # 相对路径 data/ var/ 都以此为基准
ExecStart=/srv/knowledge_base/.venv/bin/uvicorn app.main:app \
          --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=3
Environment=TZ=Asia/Shanghai
# .env 由 pydantic-settings 从 WorkingDirectory 读，不需要 EnvironmentFile

[Install]
WantedBy=multi-user.target
```

### C. Docker Compose（草案，TODO §6 还没落地）

```yaml
services:
  db:
    image: pgvector/pgvector:pg17
    environment: { POSTGRES_DB: knowledge_base, POSTGRES_PASSWORD: "${PG_PASSWORD}" }
    volumes: ["pgdata:/var/lib/postgresql/data"]
  redis: { image: redis:7-alpine }
  api:
    build: .
    environment: { DATABASE_URL: "postgresql+asyncpg://postgres:${PG_PASSWORD}@db:5432/knowledge_base",
                   REDIS_URL: "redis://redis:6379/0" }
    volumes: ["./data:/app/data", "./var:/app/var"]     # 原件必须挂出来，否则重建容器=文档全没
    ports: ["127.0.0.1:8000:8000"]
    depends_on: [db, redis]
volumes: { pgdata: {} }
```
容器里**不要**用超户连库；`CREATE EXTENSION vector` 在初始化阶段人工执行（`pgvector` 镜像已带扩展文件）。

### D. 手机壳（Capacitor，TODO §5 M9）

```bash
cd frontend && npm i @capacitor/core @capacitor/cli @capacitor/android
npx cap init && npx cap add android && npx cap open android
```
四条硬要求，少一条就是"手机上一片 404"：
1. `VITE_API_BASE` 必须是**绝对地址**（壳里没有 vite 代理，留空会变成请求 `capacitor://localhost/api/v1`）；
2. 后端要 **HTTPS**（WebView 走原生信任链，自签证书很麻烦 → `tailscale serve` / Caddy / LE DNS-01）；
3. token 放 `Preferences` 而不是 `localStorage`（系统清缓存 = 用户莫名掉登录）；
4. 拦返回键 + `windowSoftInputMode="adjustResize"`（键盘顶起是聊天类 WebApp 最高频真机 bug）。
路由已做了适配：检测到 `window.Capacitor` 就自动切 hash history。

## 6. 反向代理（SSE 这两个参数不写就等于没配）

```nginx
location /api/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_buffering off;              # 否则流式回答被攒成一坨：等十几秒然后一次性全出来
    proxy_cache off;
    proxy_read_timeout 300s;          # 长答案 + 心跳（默认 15s 一帧 : ping）
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```
Caddy 更省事（自动 HTTPS 且默认不缓冲 SSE）：
```caddy
your.domain { reverse_proxy 127.0.0.1:8000 }
```

## 7. 上线前检查表

- [ ] `SECRET_KEY` 换随机、`APP_DEBUG=False`、`/docs` 关掉或加鉴权（它默认公开，等于把接口清单送出去）
- [ ] `CORS_ORIGINS` 收成具体 origin（确认 `allow_credentials` 没被 `*` 静默关掉）
- [ ] 数据库换专用角色 + 只授 `knowledge_base` 一个库；`SQL_ECHO=False`
- [ ] `DB_STRICT_STARTUP=True`（缺表拒绝启动，让编排摘实例而不是端着一个残废服务）
- [ ] HTTPS + 反代 SSE 两个参数；`--workers 1` 或者已经做完 Redis 广播
- [ ] `data/`、`var/` 落在有备份的卷上，并配磁盘配额（上传是 20MB/张，被刷就是磁盘事故）
- [ ] 备份与恢复演练过一次（见 `docs/OPERATIONS.md` §3）
- [ ] 日志里没有密码/token/用户资料原文；错误带 trace_id
- [ ] `docs/TESTPLAN.md` §1.1–§1.6 全绿

## 8. 升级与回滚

**现在没有 alembic**（TODO §6）。表结构真源是 `app/db/models.py`，`init_db.py` 只**建缺失的表**，
**不会改已有表的列**。所以加/改字段的路子是：

```bash
.venv/bin/python scripts/init_db.py --check                  # 先看有没有漂移
.venv/bin/python scripts/init_db.py --snapshot > init_db.sql # 刷新快照（别手改这个文件）
psql -U postgres -d knowledge_base -c "ALTER TABLE ... ;"    # 手工 DDL，加列优先、别改/删列
.venv/bin/python scripts/init_db.py --check                  # 再确认齐了
```

回滚顺序（越靠前越安全）：
1. 代码回滚 = `git checkout <tag> && systemctl restart kb-api`（**不要**顺手 drop 表）；
2. 数据回滚 = 恢复 `pg_dump`（见 OPERATIONS §3）+ 恢复 `data/` 卷；
3. 只想要"重来一遍"的开发环境：`init_db.py --drop` —— ⚠️ **它没有交互确认，敲下去 `drop_all` 就直接执行**，先确认自己连的是哪个库（读的是 `.env` 的 `DATABASE_URL`）。

⚠️ 换 embedding 模型/维度 = 旧向量全部作废：必须清空 collection 后 `index_documents.py --all` 全库重跑。
