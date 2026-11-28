"""日志这一层的测试（app/core/logging_conf.py）

最该被钉住的是第一条：`logger.info` 必须真的能落地。
改这层之前的现象是"后端什么都没打"——不是没执行，是 root 上没 handler、
`logging.lastResort` 只放行 WARNING+，所以 INFO 被静默丢掉。
没有这条用例，下次谁再删掉 setup_logging() 是看不出来的。
"""

import logging
import re

import pytest

from app.core import logging_conf
from app.core.config import settings


@pytest.fixture(autouse=True)
def restore_global_logging():
    """setup_logging 改的是全局 root，必须还原，否则后面的用例会被我的 handler 污染"""
    root = logging.getLogger()
    saved_root = (root.level, list(root.handlers), list(root.filters))
    watched = ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "sqlalchemy.engine", "httpx")
    saved = {name: (list(logging.getLogger(name).handlers), logging.getLogger(name).propagate,
                    logging.getLogger(name).level) for name in watched}
    saved_summary = dict(logging_conf._summary)
    # configure() 会直接改 settings（为了走真实分支），这里统一还原，别把 WARNING 漏给后面的用例
    log_keys = ("LOG_LEVEL", "LOG_TO_CONSOLE", "LOG_TO_FILE", "LOG_DIR", "LOG_FILE_NAME",
                "LOG_MAX_BYTES", "LOG_BACKUP_COUNT", "LOG_ACCESS_LINES")
    saved_settings = {key: getattr(settings, key) for key in log_keys}
    yield
    root.setLevel(saved_root[0])
    root.handlers[:] = saved_root[1]
    root.filters[:] = saved_root[2]
    for name, (handlers, propagate, level) in saved.items():
        logger = logging.getLogger(name)
        logger.handlers[:] = handlers
        logger.propagate = propagate
        logger.setLevel(level)
    logging_conf._summary.clear()
    logging_conf._summary.update(saved_summary)
    for key, value in saved_settings.items():
        setattr(settings, key, value)


def configure(tmp_path, *, to_file=True, to_console=False, level="INFO", max_bytes=4 * 1024 * 1024):
    """按临时目录配一次，返回 (摘要, 日志文件路径)

    maxBytes 默认给足 4MB：一条 traceback 就有十几 KB，设小了会在断言之前
    就滚到 app.log.1 去（我先踩过一次，症状是"文件里什么都没有"）。
    滚动本身另有专门的用例验。
    """
    settings.LOG_LEVEL = level
    settings.LOG_TO_FILE = to_file
    settings.LOG_TO_CONSOLE = to_console
    settings.LOG_DIR = str(tmp_path / "logs")
    settings.LOG_FILE_NAME = "app.log"
    settings.LOG_MAX_BYTES = max_bytes
    settings.LOG_BACKUP_COUNT = 2
    settings.LOG_ACCESS_LINES = True
    return logging_conf.setup_logging(force=True), tmp_path / "logs" / "app.log"


def test_info_reaches_the_file(tmp_path):
    """这条就是整个文件存在的理由：INFO 必须能被看见（以前是被 lastResort 丢掉的）"""
    summary, path = configure(tmp_path)
    assert summary["file_ok"] is True
    logging.getLogger("app.document").info("文档入库完成 耗时 解析=1.20s 切块=0.02s 写库=0.30s")
    text = path.read_text(encoding="utf-8")
    assert "文档入库完成" in text
    assert "INFO" in text
    assert "[rid-none]" not in text  # 没绑请求上下文时是占位符 "-"


def test_line_format_contains_request_id(tmp_path):
    summary, path = configure(tmp_path)
    token = logging_conf.bind_request_id("abc123def456")
    try:
        logging.getLogger("app.services.vector").info("向量化完成 chunks=64")
    finally:
        logging_conf.reset_request_id(token)
    text = path.read_text(encoding="utf-8")
    assert "[abc123def456]" in text
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\s+INFO\s+app\.services\.vector", text)


def test_setup_is_idempotent(tmp_path):
    """重复调用不能叠 handler（uvicorn --reload / 多次 import app.main 都会重复走到这里）"""
    configure(tmp_path)
    before = len(logging.getLogger().handlers)
    logging_conf.setup_logging()
    logging_conf.setup_logging(force=True)
    assert len(logging.getLogger().handlers) == before


def test_third_party_loggers_do_not_double_print(tmp_path):
    """uvicorn / SQLAlchemy 自带的 handler 必须摘掉并交给 root，否则同一行打两遍、两种时间戳格式"""
    configure(tmp_path, to_console=True)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        assert logger.handlers == [], name
        assert logger.propagate is True, name


def test_file_handler_failure_falls_back(tmp_path):
    """日志目录被一个同名文件占住 → 只退回终端，绝不抛异常把应用带崩"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我不是目录", encoding="utf-8")
    settings.LOG_LEVEL = "INFO"
    settings.LOG_TO_FILE = True
    settings.LOG_TO_CONSOLE = True
    settings.LOG_DIR = str(blocker)
    summary = logging_conf.setup_logging(force=True)
    assert summary["file_ok"] is False
    assert "console" in summary["handlers"]
    assert logging.getLogger().handlers  # root 仍然有出口


def test_uvicorn_cli_level_still_wins(tmp_path):
    """`./start.sh api --log-level debug` 不能被 LOG_LEVEL=INFO 静默吃掉

    uvicorn 在我们之前先按命令行参数配了自己的 logger，这里取更响的那个。
    （反方向不成立：LOG_LEVEL=DEBUG 而命令行没写时，也是 DEBUG。）
    """
    uvicorn = logging.getLogger("uvicorn")
    uvicorn.setLevel(logging.DEBUG)
    probe = logging.NullHandler()   # 有 handler = uvicorn 自己配过日志（真实运行时的样子）
    uvicorn.addHandler(probe)
    try:
        summary, _ = configure(tmp_path, level="INFO")
        assert summary["level"] == "DEBUG"
    finally:
        uvicorn.removeHandler(probe)
    # 反过来说：我们配置过之后（handler 已清空）就不再"继承"，LOG_LEVEL 说了算
    again, _ = configure(tmp_path, level="WARNING")
    assert again["level"] == "WARNING"


def test_rotating_file_rolls_over(tmp_path):
    """到量就滚，且只留 backupCount 份（否则日志早晚把磁盘吃掉）"""
    _, path = configure(tmp_path, max_bytes=512)
    for index in range(60):
        logging.getLogger("app.document").info("填充行 %03d %s", index, "x" * 40)
    rolled = sorted(item.name for item in path.parent.glob("app.log*"))
    assert rolled == ["app.log", "app.log.1", "app.log.2"], rolled


def test_status_reports_configuration(tmp_path):
    summary, path = configure(tmp_path, level="WARNING")
    assert summary["level"] == "WARNING"
    assert summary["file"].endswith("app.log")
    assert logging_conf.is_configured() is True


def test_request_id_filter_fills_missing_field():
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, "hello", None, None)
    token = logging_conf.bind_request_id("fixed-id")
    try:
        assert logging_conf.RequestIdFilter().filter(record) is True
        assert record.request_id == "fixed-id"
    finally:
        logging_conf.reset_request_id(token)


def test_new_request_id_is_unique_and_short():
    ids = {logging_conf.new_request_id() for _ in range(500)}
    assert len(ids) == 500
    assert all(len(value) == 12 for value in ids)


# ---------------- 中间件（走真 app）----------------
def test_middleware_generates_request_id(client):
    response = client.get("/health")
    rid = response.headers.get("X-Request-ID")
    assert rid and re.fullmatch(r"[0-9a-f]{12}", rid), rid


def test_middleware_reuses_incoming_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "frontend-trace-42"})
    assert response.headers.get("X-Request-ID") == "frontend-trace-42"


def test_access_log_line_has_method_path_status_duration(client, caplog):
    with caplog.at_level(logging.INFO, logger="app.request"):
        client.get("/health")
    lines = [record.getMessage() for record in caplog.records if record.name == "app.request"]
    assert lines, "访问日志一条都没有"
    assert re.search(r"GET /health → 200 \d+(\.\d+)?ms", lines[-1]), lines


def test_unhandled_error_logs_traceback_and_returns_trace_id(client, tmp_path):
    """500 那一路以前是黑洞：只回"服务器繁忙"，连 traceback 都不落

    现在必须同时满足三件事：响应里有 trace_id、traceback 落在文件里、
    那一行日志带的就是同一个 trace_id（否则贴工单也串不起来）。

    用**文件**断言而不是 caplog：RequestIdFilter 挂在我们的 handler 上，
    caplog 自己那个 handler 没挂，`%(request_id)s` 走不到它那儿；
    而且 setup_logging 会重建 root 的 handler 列表，caplog 的会被摘掉。
    """
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user
    from app.main import app

    _, path = configure(tmp_path, to_console=False)

    def boom():
        raise RuntimeError("upstream 炸了 secret=sk-should-not-leak")

    app.dependency_overrides[get_current_user] = boom
    try:
        # raise_server_exceptions=False：Starlette 的 ServerErrorMiddleware 在**发完响应之后
        # 总会把异常再抛一次**（让服务器自己记日志），TestClient 默认会把它冒到用例里。
        with TestClient(app, raise_server_exceptions=False) as tc:
            response = tc.get("/api/v1/user/info")
    finally:
        app.dependency_overrides.pop(get_current_user, None)

    assert response.status_code == 500, response.text
    body = response.json()
    assert body["status"] == 500 and body["msg"]
    trace_id = body["data"]["trace_id"]
    assert trace_id and re.fullmatch(r"[0-9a-f]{12}", trace_id), body
    assert response.headers.get("X-Request-ID") == trace_id

    text = path.read_text(encoding="utf-8")
    assert "Traceback (most recent call last)" in text
    assert "sk-should-not-leak" in text  # 堆栈要完整（含异常原文），那是给我们看的
    assert re.search(rf"ERROR\s+app\.main \[{trace_id}\] 未处理异常 GET /api/v1/user/info", text), text
    # DEBUG=True（本地默认）才会把异常原文放进响应体；线那条见下一个用例
    assert "RuntimeError" in body["msg"]


def test_500_body_does_not_leak_exception_in_production(client, monkeypatch):
    """APP_DEBUG=False 时对外只有一句通用提示，但 trace_id 照样给

    为什么单独一条用例：上面那条断言的是 DEBUG 车道（本地默认开着，异常原文进响应体）。
    真到线上，异常原文里可能带 SQL、路径、甚至连接串 —— 只能进日志，不能进响应。
    """
    from fastapi.testclient import TestClient

    from app.api.deps import get_current_user
    from app.main import app

    def boom():
        raise RuntimeError("upstream 炸了 secret=sk-should-not-leak")

    monkeypatch.setattr(settings, "DEBUG", False)
    app.dependency_overrides[get_current_user] = boom
    try:
        with TestClient(app, raise_server_exceptions=False) as tc:
            response = tc.get("/api/v1/user/info")
    finally:
        app.dependency_overrides.pop(get_current_user, None)
    assert response.status_code == 500
    body = response.json()
    assert body["msg"] == "服务器繁忙，请稍后重试"
    assert "sk-should-not-leak" not in response.text
    assert body["data"]["trace_id"] == response.headers.get("X-Request-ID")
