# 真机验收计划（L2 集成 + L3 端到端）

> 与 `TODO.md` §1 的十个分组**一一对应**，编号 `TC-<组>-<序>`。
> `TODO.md §1` 是"照做的顺序"，本文件是"打勾的表"——测的时候填右边三列，别填左边。
> 判定只有三种：**通过** / **设计内**（行为符合约定，不是 bug）/ **BUG**（写清楚现象+日志尾巴）。

## 0. 前置（都已确认，不用重查）

```bash
sudo systemctl start postgresql@17-main        # PG 17，端口 5432
.venv/bin/python scripts/init_db.py --check    # 8 张表齐
psql -U postgres -h 127.0.0.1 -d knowledge_base -c '\dt'
.venv/bin/python scripts/prune_data.py --apply # 先清测试遗留，否则 §1.3 的孤儿核对全是噪声
./start.sh check                               # 11 项全 OK 再往下
SQL_ECHO=False ./start.sh all                  # 关 SQL 回显，否则看不见分段耗时那行
```
环境事实：`pgvector 0.8.7` 已装在库内；LLM=DeepSeek、Embedding+Vision=百炼同一把 key；
`RETRIEVAL_LANE` 现在还是 `keyword`（§5 验收通过后才切）。

## 1.1 数据库与启动

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-DB-01 | `curl -s localhost:8000/health \| jq .data` | `status=ok`、`missing_tables=[]`；四节 `database/memory_backend/vision/cache` 都在 | | |
| TC-DB-02 | 读 `cache` 节 | `client_ready` 与 `last_ok` 分开看（前者=建了客户端，后者=真通过一次） | | |
| TC-DB-03 | `psql` 里 `DROP TABLE chunks;` 再打 `/health` | **503** + msg 指到 `scripts/init_db.py` | | |
| TC-DB-04 | 补表后 `DB_STRICT_STARTUP=True ./start.sh` | 缺表时**拒绝启动**（不是带着缺表起来）；验完改回 `False` | | |
| TC-DB-05 | 两个终端对同一库各触发一次改名/重索引 | `knowledge_bases.version` 递增两次，无丢更新（`FOR UPDATE` 只有真 PG 算数） | | |
| TC-DB-06 | 拿 SQLite 跑一遍全量再拿 PG 跑一遍 | 结论差异要写下来（本项目 100% 用例走 SQLite，这一步是唯一对照） | | |

## 1.2 文档链路

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-DOC-01 | 传真 md/txt | `pending→parsing→chunking→indexed`；日志 `文档入库完成 … 耗时 解析/切块/写库` 三个数记下来 | | |
| TC-DOC-02 | 传文本型 PDF | indexed；`/documents/detail?document_id=…` 正文预览与页码对得上 | | |
| TC-DOC-03 | 传扫描件 PDF | `failed` + `error_msg` 含「没有从 PDF 中提取到文字」「SCANNED_PDF_VISION」 | | 设计内 |
| TC-DOC-04 | 传 docx / pptx | indexed；表格/幻灯的 page 语义合理 | | |
| TC-DOC-05 | >20MB 文件 | **413**（流式读的过程中判，不是读完才判） | | |
| TC-DOC-06 | 0 字节文件 | **400** | | |
| TC-DOC-07 | 改名的 .exe 冒充 .pdf | **400**（魔数嗅探） | | |
| TC-DOC-08 | 同库重复传同一文件 | **409**，且磁盘不留第二份 | | |
| TC-DOC-09 | `/documents/reindex/{id}` 点两次 | 幂等：chunks 不翻倍 | | |
| TC-DOC-10 | `/documents/list` 分页 + `kb_id` 过滤 + `/download/{id}` | 翻页正确；下回来的文件与原文件 `sha256` 相同 | | |

## 1.3 磁盘 ↔ 数据库对账

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-DISK-01 | `prune_data.py` dry-run | 孤儿只应该是测试遗留；**真文档不在孤儿列表**；漂移=0、缺失=0 | | |
| TC-DISK-02 | `--apply` | 孤儿删掉、空目录自底向上收掉；`find data -type f \| wc -l` 只剩真文档 | | |
| TC-DISK-03 | 删一篇文档 | 磁盘文件真的没了 | | |
| TC-DISK-04 | 删整个知识库 | `data/{owner}/{kb_id}/` 与 `data/share/{kb_id}/` 都收干净；向量也清了（`--check`/检索查不到） | | |
| TC-DISK-05 | 被授权人上传 | 文件落在 `data/share/{kb_id}/`，**没写进库主人目录** | | |
| TC-DISK-06 | 手工往 `data/` 塞一个野文件 → `/documents/status` | `orphan_files` 抓到它 | | |

## 1.4 视觉车道（本轮新实现，重点）

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-VIS-01 | 手机拍票据上传 | `pending → indexed`，无 401/超时 | | |
| TC-VIS-02 | 看 `/documents/detail` 的描述 | 金额、日期、标题**逐字转录**出来了（质量不行要记原文） | | |
| TC-VIS-03 | 上传 >4MB 原图 | 自动缩到 `VISION_MAX_PIXELS` 以内并成功；或给出可操作报错 | | |
| TC-VIS-04 | 透明通道 PNG | indexed（png 走无损，不转 JPEG） | | |
| TC-VIS-05 | 传 `.tiff` / `.heic` | **400**（图片白名单只有 6 种） | | |
| TC-VIS-06 | `POST /search` 用图里的数字搜 | 命中这张图的描述块 | | |
| TC-VIS-07 | 清掉两把 key 重启 → 传图 | 回到 `status=stored`（**不是 failed**），`/health.vision.reason` 说清原因 | | |
| TC-VIS-08 | key 填回来 → 对那张 stored 的点 reindex | 补上描述，不用删了重传 | | |
| TC-VIS-09 | （可选）`uv add pymupdf` + `SCANNED_PDF_VISION=True` 重传扫描件 | 只描述前 5 页；meta 的 `pages_described` 看得见"没全看" | | |

## 1.5 检索与向量

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-RET-01 | `index_documents.py --check` | 报"车道可用" | | |
| TC-RET-02 | `--all` | 全部 indexed 文档进向量库，无批次超限 | | |
| TC-RET-03 | `--kb <id> --query 原话 --lane vector` | 有 0~1 相似度 + 出处（chunk 回表取正文，不是向量表里存的） | | |
| TC-RET-04 | 同问 `--lane hybrid` | 与 vector 的召回差别写一句 | | |
| TC-RET-05 | 改错 `EMBEDDING_API_KEY` 重启后提问 | **显式降级**到 keyword，meta 帧 `retrieval.fallback` 有值，前端显示"已退回" | | |
| TC-RET-06 | 问一个库里绝对没有的词 | 零命中 + **不换车道**（`fallback` 为空） | | |
| TC-RET-07 | A 库提问，B 库有更强的词 | 不越界召回；共享库只搜"我能读的" | | |
| TC-RET-08 | 通过后把 `RETRIEVAL_LANE=vector` 切过来复跑 TC-RET-03/06 | 前端 `sources[].score` 不做绝对阈值判断 | | |

## 1.6 对话与流式（本轮修了两个前端 bug，重点复验）

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-CHAT-01 | 新建对话 → 立刻提问 | 问题气泡**马上**出现，回答气泡出现"生成中" | | |
| TC-CHAT-02 | 全程不刷新 | token **逐字**往外蹦（不是最后整段贴出来） | | |
| TC-CHAT-03 | 同会话连点两次发送 | 第二条 **409**「已有一条正在生成」，且**界面看得见** | | |
| TC-CHAT-04 | 生成中点 `/stop` | 保留已生成的半截答案（不是"回答中断"） | | |
| TC-CHAT-05 | 回答结束刷新页面 | 问题+答案都在、顺序对、`sources` 角标在、首轮标题自动改了 | | |
| TC-CHAT-06 | 流式途中切走会话再回来 | 已知行为：那一路跑完才放开输入（TODO §4）——只记录手感 | | 设计内 |
| TC-CHAT-07 | 首轮不选知识库提问 | `retrieval_mode=no_scope` 且界面提示"没限定范围" | | |
| TC-CHAT-08 | 关掉 `stream` 走 JSON 分支（脚本/CI 用） | `answer` + `turn.appended=2` + `message_count` 递增 | | |

## 1.7 权限 / 共享 / 用户

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-AUTH-01 | `read` 授权的被授权人上传 | **403** | | |
| TC-AUTH-02 | `write` 授权上传 + reindex | 允许；文件落 `data/share/` | | |
| TC-AUTH-03 | 访问别人的文档/库/会话 | **404**（不是 403，不泄露"存在"） | | |
| TC-AUTH-04 | 撤销授权后再访问 | 立即失效（身份缓存不影响 kb 权限判定） | | |
| TC-AUTH-05 | 无 token / 坏 token / 过期 token | 401 且前端自动跳登录页 | | |
| TC-AUTH-06 | 注册重名 / 重邮箱；改资料；改密后用旧 token；注销 | 各行为符合预期（改密后 token 吊销走查库，不受缓存影响） | | |

## 1.8 两层记忆

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-MEM-01 | 聊完一轮后 `\dt` | 出现 `long_term_memory`（外加 `langchain_pg_*` 两张） | | |
| TC-MEM-02 | A 会话说过事实 → 新建 B 会话提问 | 带得上（相关才进 prompt） | | |
| TC-MEM-03 | 换账号问同样的话 | **检不到**别人的记忆 | | |
| TC-MEM-04 | 同一事实连说三次 | 判重生效，不多条 | | |
| TC-MEM-05 | 说超过 `MEMORY_MAX_ITEMS_PER_USER` 条 | 淘汰最旧，不无限涨 | | |
| TC-MEM-06 | 连聊 8 轮（`SHORT_TERM_MAX_TURNS=6`） | 老几轮压成摘要，"我第一个问题是什么"仍答得上 | | |
| TC-MEM-07 | `sudo systemctl stop postgresql@17-main` 后提问 | 明确"不记 + `/health` 给原因"；**`data/memory`（现 `var/memory`）不许新增 jsonl** | | |

## 1.9 缓存与稳定性

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-CACHE-01 | 同一用户连打 20 次 `/user/me` | 第二次起走 Redis（`redis-cli info stats \| grep keyspace_` 看 hits 涨） | | |
| TC-CACHE-02 | 停 Redis 再打接口 | 业务照常（fail-open 回源）；日志一条 warning；`/health.cache` 变红但**不改状态码** | | |
| TC-CACHE-03 | Redis 起回来 | 熔断 ~10s 内半开自愈，不用重启进程 | | |
| TC-OPS-01 | 服务 Ctrl+C 再起，接着上一个会话提问 | 短期窗口丢了就记下来（TODO §3 已撤销排期，洞照旧） | | 已知洞 |
| TC-OPS-02 | 传大文件途中杀进程 | 不留半截文件、不留"记录有/文件没有"的脏数据 | | |

## 1.10 手机浏览器（M9 前置）

| # | 操作 | 预期 | 实际 | 判定 |
| --- | --- | --- | --- | --- |
| TC-PHONE-01 | 手机同局域网开 `http://<电脑IP>:5173` | 能登录、建库、提问、看流式 | | |
| TC-PHONE-02 | 从**相册选图**上传 | 走 multipart 到后端，尺寸/格式闸门正确 | | |
| TC-PHONE-03 | 键盘弹起 | 输入框不被挡、列表滚到底（`visualViewport`） | | |
| TC-PHONE-04 | 切后台 30s 再回来 | 流断了能不能补看（对应断线续传，已撤销排期）——只记录现象 | | 已知洞 |
| TC-PHONE-05 | 清浏览器数据 | 掉登录但会话数据还在；记下手感（壳里 token 要换 Preferences） | | |

## 通过标准

- §1.1–§1.3 全绿才算"后端能上真库"；
- §1.4 全绿才算"图片能进检索"；§1.5 的 TC-RET-01~07 全绿才允许把 `RETRIEVAL_LANE` 切到 `vector`；
- §1.6 的 TC-CHAT-01/02/03 全绿才算"对话可用"（前端才考虑 commit 与打包）；
- §1.10 全绿才开 M9（Capacitor），顺序不能反。

## 出问题时怎么记（照这个格式，别只写"不行"）

```text
TC-DOC-01  BUG
现象：status 一直停在 chunking
日志尾巴：文档入库完成 … 耗时 解析=12.4s 切块=0.02s 写库=1.9s  ← 或贴报错最后 5 行
接口/入参：POST /api/v1/documents/upload?kb_id=xxx，文件 3.2MB 的 a.pdf
判定依据：/documents/status 里 pending=0 chunking=1；DB 里该文档 status=chunking
```
