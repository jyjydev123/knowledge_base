"""统一日志：一个入口、两份输出、一条访问日志、一个 request_id（TODO.md §6.3）

**为什么必须有这个文件**（2026-11-28 实测，不是推测）：
    项目里十几处 `logging.getLogger("app.*")` 从来没有配过 handler/formatter，
    也没有 basicConfig；uvicorn 的 LOGGING_CONFIG 只配 `uvicorn`/`uvicorn.error`/
    `uvicorn.access` 三个 logger，**不碰 root**（`"root" in LOGGING_CONFIG == False`）。
    root 上没有 handler 时，Python 用 `logging.lastResort` 兜底，而它只放行 WARNING 以上：

        .venv/bin/python -c "import logging as l; l.getLogger('a').info('A'); l.getLogger('a').warning('B')"
        # 只出来 B —— A 被静默丢弃，B 还是裸的（没时间戳、没级别前缀）

    也就是说 `process_document` 那句"耗时 解析/切块/写库"（logger.info）**一条都没输出过**，
    而 SQL 回显能看见纯属意外：SQLAlchemy 在 echo=True 时自己往 stdout 挂了一个 handler
    （sqlalchemy/log.py:_add_default_handler）。排错时"后端什么都没打"其实是"被丢了"。

三条设计决定：

1. **一个 formatter 管所有人**。uvicorn 那三个 logger 的 handler 清空、`propagate=True`，
   SQLAlchemy 自带的 stdout handler 也摘掉，全部交给 root 的 handler —— 否则同一个请求
   会打两遍（一遍 uvicorn 的格式、一遍我的），日志里出现两种时间戳格式最难对齐时间线。
2. **中间件用纯 ASGI 类，不用 BaseHTTPMiddleware**。后者会把响应体包进 anyio 流，
   对 SSE 这种跑几十秒的流式响应有缓冲/取消语义的坑，而本项目最贵的链路就是流式问答，
   不值得为一个 request_id 去赌框架实现细节。
3. **文件日志建不起来也不能影响启动**。目录不存在会 mkdir；只读文件系统/权限不对时
   退回"只有终端"并在 stderr 提示一次，绝不抛异常 —— 日志层把自己搞成故障源是本末倒置。
"""

from __future__ import annotations

import logging
import logging.config
import sys
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from starlette.datastructures import Headers, MutableHeaders

from app.core.config import settings

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s [%(request_id)s] %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
REQUEST_ID_HEADER = "X-Request-ID"
REQUEST_ID_PLACEHOLDER = "-"

# 当前请求的 id。用 ContextVar 而不是把 id 塞进每个 logger 调用：
# 后台任务（BackgroundTasks 里的解析）、SSE 生成器都和请求跑在同一个 context 里，
# 所以整条链路的日志自动带上同一个 id，不需要每个函数互相传递。
REQUEST_ID: ContextVar[str] = ContextVar("kb_request_id", default=REQUEST_ID_PLACEHOLDER)

_LEVELS = {"NOTSET": 0, "DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}

_access_logger = logging.getLogger("app.request")
_summary: dict[str, Any] = {}


def new_request_id() -> str:
    """12 位十六进制。够短（能贴进工单），也够唯一（单机一天几万请求不会撞）"""
    return uuid.uuid4().hex[:12]


def get_request_id() -> str:
    return REQUEST_ID.get() or REQUEST_ID_PLACEHOLDER


def bind_request_id(value: str | None = None) -> Any:
    """设置当前 request_id，返回 token（配合 reset_request_id，一般在中间件里用）"""
    return REQUEST_ID.set(value or new_request_id())


def reset_request_id(token: Any) -> None:
    REQUEST_ID.reset(token)


class RequestIdFilter(logging.Filter):
    """给每条记录补 `request_id` 字段

    formatter 里有 `%(request_id)s`，**没挂这个 filter 的 handler 会 KeyError**，
    所以 handler 必须成对配置（setup_logging 里就是这么做的）。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True


def _resolve_level(raw: Any, default: int = logging.INFO) -> int:
    """认得 INFO / info / 20 三种写法；认不出来就用默认值而不是抛异常"""
    text = str(raw or "").strip().upper()
    if text.isdigit():
        return int(text)
    return _LEVELS.get(text, default)


def _level_name(raw: Any, default: str = "INFO") -> str:
    text = str(raw or "").strip().upper()
    return text if text in _LEVELS else default


def _build_config() -> tuple[dict[str, Any], dict[str, Any]]:
    """拼 dictConfig，并返回这轮的摘要（给 status()/preflight 用）"""
    level_name = _level_name(settings.LOG_LEVEL)
    # `./start.sh api --log-level debug` 是 start.sh 用法表里写着的入口，而 uvicorn 在我们之前
    # 就按这个参数配好了它自己的 logger。这里取"更响的那个"，否则命令行参数会静默失效
    # （症状是"我明明加了 --log-level debug 却什么 DEBUG 都没有"）。
    #
    # 判据用"它身上还挂着 handler"而不是"它的 level != NOTSET"：我们第一次配置之后会把
    # uvicorn 的 handler 清空、并把它的 level 设成我们的值 —— 如果只看 level，
    # 第二次 setup 就会把自己写下的 INFO 当成命令行的 DEBUG，LOG_LEVEL=WARNING 永远改不动。
    uvicorn_logger = logging.getLogger("uvicorn")
    uvicorn_level = uvicorn_logger.level if uvicorn_logger.handlers else logging.NOTSET
    if 0 < uvicorn_level < _LEVELS[level_name]:
        level_name = next((key for key, value in _LEVELS.items() if value == uvicorn_level), level_name)
    log_dir = str(settings.LOG_DIR or "logs")
    file_path = Path(log_dir) / str(settings.LOG_FILE_NAME or "app.log")

    handlers: dict[str, Any] = {}
    root_handlers: list[str] = []

    if settings.LOG_TO_CONSOLE:
        handlers["console"] = {
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stdout",
            "formatter": "kb",
            "filters": ["request_id"],
        }
        root_handlers.append("console")

    file_ok = False
    if settings.LOG_TO_FILE:
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # 先探一次可写：RotatingFileHandler 建起来就晚了 —— 它是在**滚动时**
            # 才去 rename，权限不对的话症状是"跑三天突然崩一次"，最难查。
            with file_path.open("a", encoding="utf-8"):
                pass
            handlers["file"] = {
                "class": "logging.handlers.RotatingFileHandler",
                "filename": str(file_path),
                "maxBytes": max(int(settings.LOG_MAX_BYTES), 1024),
                "backupCount": max(int(settings.LOG_BACKUP_COUNT), 0),
                "encoding": "utf-8",
                "formatter": "kb",
                "filters": ["request_id"],
            }
            root_handlers.append("file")
            file_ok = True
        except OSError as exc:
            print(f"[logging] 文件日志不可用（只用终端输出）: {file_path}: {exc}", file=sys.stderr)

    # 这些 logger 的 handler 全部清空 + propagate，让 root 统一格式化（见文件头第 1 条）
    shared = {"handlers": [], "propagate": True}
    config: dict[str, Any] = {
        "version": 1,
        # 关键：dictConfig 缺省是 True，会把**已存在**的 logger 全部关掉。
        # 本模块是在 app.main 导入后才跑的，那时 14 个 app.* logger 都已经建好了，
        # 少了这一行就会得到"日志一条都没有"这个最诡异的结局。
        "disable_existing_loggers": False,
        "formatters": {"kb": {"format": LOG_FORMAT, "datefmt": LOG_DATE_FORMAT}},
        "filters": {"request_id": {"()": "app.core.logging_conf.RequestIdFilter"}},
        "handlers": handlers,
        "root": {"level": level_name, "handlers": root_handlers},
        "loggers": {
            "uvicorn": {**shared, "level": level_name},
            "uvicorn.error": {**shared, "level": level_name},
            # 访问日志由 RequestIdMiddleware 打（带耗时和 request_id），所以压掉 uvicorn 那份，
            # 否则每个请求两行、还只有它那行没有耗时。
            "uvicorn.access": {**shared, "level": "INFO" if not settings.LOG_ACCESS_LINES else "WARNING"},
            # 父 logger **必须留在 WARNING**：SQLAlchemy 自己也是这么初始化的
            # （util/log.py: rootlogger.setLevel(WARNING)）。把它设成 INFO 的实测后果是
            # sqlalchemy.orm.mapper.Mapper 把每个 relationship 的建立过程全打一遍，
            # 一次登录刷出几百行噪声，真正有用的那几条反而看不见。
            # echo 的开关只在 sqlalchemy.engine 这一层给（SQL_ECHO=True 时用户就是要看 SQL）。
            "sqlalchemy": {**shared, "level": "WARNING"},
            "sqlalchemy.engine": {**shared, "level": "INFO" if settings.SQL_ECHO else "WARNING"},
            "sqlalchemy.orm": {**shared, "level": "WARNING"},
            "sqlalchemy.pool": {**shared, "level": "WARNING"},
            "sqlalchemy.dialects": {**shared, "level": "WARNING"},
            # 这几个第三方默认太吵：每次 embedding/视觉请求都会打一整条 HTTP 摘要
            "httpx": {**shared, "level": "WARNING"},
            "httpcore": {**shared, "level": "WARNING"},
            "openai": {**shared, "level": "WARNING"},
            "asyncio": {**shared, "level": "WARNING"},
        },
    }

    summary = {
        "signature": (level_name, str(file_path), bool(settings.LOG_TO_FILE), bool(settings.LOG_TO_CONSOLE),
                      bool(settings.LOG_ACCESS_LINES)),
        "level": level_name,
        "to_console": bool(settings.LOG_TO_CONSOLE),
        "to_file": bool(settings.LOG_TO_FILE),
        "file": str(file_path),
        "file_ok": file_ok,
        "access_lines": bool(settings.LOG_ACCESS_LINES),
        "max_bytes": int(settings.LOG_MAX_BYTES),
        "backup_count": int(settings.LOG_BACKUP_COUNT),
        "handlers": sorted(root_handlers),
    }
    return config, summary


def setup_logging(force: bool = False) -> dict[str, Any]:
    """配置一次（幂等）。配置没变就直接返回上次的摘要，避免 uvicorn --reload 反复重建 handler

    Args:
        force: True = 即使配置没变也重建（测试和 preflight 用）
    """
    global _summary
    config, summary = _build_config()
    if not force and _summary and _summary.get("signature") == summary["signature"]:
        return status()
    logging.config.dictConfig(config)
    # SQLAlchemy 的 echo 会在 engine 上**挂自己的 stdout handler**（且在 import 期就挂好了），
    # dictConfig 只清得掉我列出来的 logger，这里再兜一次，保证同一行 SQL 只打一遍。
    for name in ("sqlalchemy", "sqlalchemy.engine", "sqlalchemy.dialects", "sqlalchemy.pool"):
        logger = logging.getLogger(name)
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
        logger.propagate = True
    _summary = summary
    logging.getLogger("app.logging").debug(
        "日志已配置：level=%s handlers=%s file=%s(%s) 访问日志=%s",
        summary["level"], summary["handlers"], summary["file"], "可写" if summary["file_ok"] else "不可用",
        summary["access_lines"],
    )
    return status()


def status() -> dict[str, Any]:
    """给 preflight / 排错用（刻意不接进 /health：日志不是业务依赖，不该影响就绪状态）"""
    if _summary:
        return dict(_summary)
    return {"configured": False, "level": _level_name(settings.LOG_LEVEL), "note": "还没调用 setup_logging()"}


def is_configured() -> bool:
    return bool(_summary)


class RequestIdMiddleware:
    """给每个 HTTP 请求绑一个 request_id，回写响应头，并打一条带耗时的访问日志

    复用客户端传来的 `X-Request-ID`（前端/网关已经生成过就跟着它走），
    这样"用户在手机上点发送"到"后端解析文档"是同一个 id，报障时才串得起来。
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        incoming = (headers.get(REQUEST_ID_HEADER.lower()) or "").strip()
        request_id = (incoming[:64] or new_request_id())
        scope.setdefault("state", {})["request_id"] = request_id
        method = scope.get("method", "-")
        path = scope.get("path", "-")

        state = {"status": 0, "length": 0}

        async def send_wrapper(message: dict) -> None:
            if message["type"] == "http.response.start":
                state["status"] = int(message.get("status") or 0)
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            elif message["type"] == "http.response.body":
                state["length"] += len(message.get("body") or b"")
            await send(message)

        token = bind_request_id(request_id)
        started = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            reset_request_id(token)
            self._log_access(method, path, state, time.perf_counter() - started)

    def _log_access(self, method: str, path: str, state: dict, seconds: float) -> None:
        if not settings.LOG_ACCESS_LINES:
            return
        elapsed_ms = seconds * 1000
        status_code = state["status"]
        if not status_code:  # 响应头都没发出去（客户端提前断开 / 未捕获异常）
            _access_logger.warning('%s %s → 未完成（客户端断开或未捕获异常）%.1fms', method, path, elapsed_ms)
            return
        # 5xx 抬到 WARNING：在只看一屏的 journald / docker logs 里它该比正常请求显眼
        level = logging.WARNING if status_code >= 500 else logging.INFO
        _access_logger.log(
            level, '%s %s → %s %.1fms %dB', method, path, status_code, elapsed_ms, state["length"]
        )


__all__ = [
    "LOG_FORMAT",
    "REQUEST_ID",
    "REQUEST_ID_HEADER",
    "RequestIdFilter",
    "RequestIdMiddleware",
    "bind_request_id",
    "get_request_id",
    "is_configured",
    "new_request_id",
    "setup_logging",
    "status",
]
