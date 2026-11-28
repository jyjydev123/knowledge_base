"""应用入口

在这里统一注册路由、跨域/请求日志中间件，以及最重要的「统一返回格式」异常处理器：
    业务异常 / 参数校验失败 / 404 / 500 都会被包成
    {"status": <和HTTP一致的码>, "msg": <提示语>, "data": <数据或null>}

日志由 app/core/logging_conf.py 统一接管（终端 + logs/app.log 滚动 + request_id），
500 那一路除了打 traceback 还会把 request_id 放进 data.trace_id —— 用户报障只需要一个字符串。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1.aichatrouter import router as aichat_router
from app.api.v1.documentrouter import router as document_router
from app.api.v1.kbrouter import router as kb_router
from app.api.v1.searchrouter import router as search_router
from app.api.v1.userrouter import router as user_router
from app.core import cache, logging_conf, startup
from app.core.config import settings
from app.core.exceptions import BusinessError
from app.schemas.common import ApiResponse

# 必须在建 app 之前：否则 lifespan 里 startup 的日志、以及导入期就已经产生的 app.* 日志
# 会走"没有 handler"的老路（被 lastResort 丢掉）。函数是幂等的，重复调用不会叠 handler。
logging_conf.setup_logging()

logger = logging.getLogger("app.main")


async def prepare_schema() -> dict:
    """启动期检查（单独抽出来，测试才能不打真全局引擎就验到三种策略）"""
    return await startup.ensure_schema_on_startup()


@asynccontextmanager
async def lifespan(_: FastAPI):
    # 内存库/未配库会在 startup 里短路，所以测试车道（with TestClient）不会因此多连一次库
    await prepare_schema()
    yield
    # 关掉 Redis 连接池。不关的话 uvicorn --reload 时每轮重启都留一批半开连接，
    # 攒到 max_connections 就再也没法建缓存（症状是"跑了一会儿缓存突然全失效"）。
    await cache.aclose()


app = FastAPI(
    lifespan=lifespan,
    title=settings.APP_NAME,
    description=settings.APP_DESCRIPTION,
    version=settings.APP_VERSION,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# ---------------- 请求上下文（request_id + 访问日志）----------------
# 用纯 ASGI 类而不是 BaseHTTPMiddleware：后者会把响应体包进 anyio 流，
# 对 SSE 长流式响应有缓冲/取消语义的坑（本项目最贵的链路就是流式问答）。
# add_middleware 是"后加的在外层"，所以 request_id 比 CORS 更早绑定，所有响应都带得上头。
app.add_middleware(logging_conf.RequestIdMiddleware)

# ---------------- 跨域 ----------------
_origins = settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,  # "*" 时浏览器不允许同时携带 cookie
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------- 统一返回格式 ----------------
def _envelope(status: int, msg: str, data=None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"status": status, "msg": msg, "data": data},
        headers=headers,
    )


@app.exception_handler(BusinessError)
async def business_error_handler(request: Request, exc: BusinessError):
    """业务异常 -> {"status": 4xx, "msg": "...", "data": null}"""
    return _envelope(exc.status, exc.msg, exc.data)


def _clean_errors(errors: list[dict]) -> list[dict]:
    """pydantic 的 errors() 里 ctx 可能带异常对象，不能直接 json 序列化"""
    cleaned = []
    for err in errors:
        cleaned.append(
            {
                "field": ".".join(str(part) for part in err.get("loc", [])[1:]) or "body",
                "location": str(err.get("loc", [""])[0] or "body"),
                "msg": str(err.get("msg", "")),
                "type": str(err.get("type", "")),
            }
        )
    return cleaned


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    """参数校验失败 -> 422，msg 里给出第一个错误提示，data 里放明细"""
    errors = _clean_errors(exc.errors())
    first = errors[0] if errors else {"field": "body", "msg": "请求参数不合法"}
    msg = f"参数校验失败: {first['field']} {first['msg']}".strip()
    return _envelope(422, msg, {"errors": errors})


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """404 路由不存在 / 405 方法不对等框架异常，同样用统一格式返回"""
    return _envelope(exc.status_code, str(exc.detail), None)


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception):
    """兜底：数据库挂了之类的意外错误也不要吐 HTML 500

    原来这一路是**黑洞**：异常被吞掉换成一句"服务器繁忙"，连 traceback 都没落，
    事后完全查不到是哪一行。现在先把堆栈打进日志（带 request_id），
    再把同一个 id 放进 data.trace_id 回给前端 —— 用户报障只需要贴那 12 个字符。
    响应体依旧不含异常原文（DEBUG 才给），密码/连接串不会被带出去。
    """
    # 这一路必须自己兜住 trace_id：**Exception 处理器跑在 Starlette 的 ServerErrorMiddleware 里，
    # 位置在 RequestIdMiddleware 的外层**（异常冒泡出来时中间件的 finally 已经 reset 过 ContextVar，
    # 而且 500 的响应也是由它直接 send 的，不经过我的 send_wrapper）。
    # 实测两个后果：data.trace_id 变成 null、日志里那行 traceback 的 rid 是 "-" —— 正是这次
    # 要修的东西，所以这里从 scope["state"] 取回 id、重新绑定后再打日志，并把头也补上。
    request_id = getattr(request.state, "request_id", None) or logging_conf.get_request_id()
    if request_id == logging_conf.REQUEST_ID_PLACEHOLDER or not request_id:
        request_id = logging_conf.new_request_id()
    token = logging_conf.bind_request_id(request_id)
    try:
        logger.exception("未处理异常 %s %s", request.method, request.url.path)
    finally:
        logging_conf.reset_request_id(token)
    data = {"trace_id": request_id}
    headers = {logging_conf.REQUEST_ID_HEADER: request_id}
    if settings.DEBUG:
        # DEBUG 下带上异常原文（本地排障靠它）；生产 APP_DEBUG=False 只会回一句通用提示
        return _envelope(500, f"服务器内部错误: {type(exc).__name__}: {exc}", data, headers)
    return _envelope(500, "服务器繁忙，请稍后重试", data, headers)


# ---------------- 路由（以后新增模块就再多 import 一个 router 多 include 一次） ----------------
app.include_router(user_router, prefix=settings.API_V1_PREFIX)
app.include_router(document_router, prefix=settings.API_V1_PREFIX)
app.include_router(kb_router, prefix=settings.API_V1_PREFIX)
app.include_router(aichat_router, prefix=settings.API_V1_PREFIX)
app.include_router(search_router, prefix=settings.API_V1_PREFIX)


@app.get("/", response_model=ApiResponse[dict], summary="根路径", tags=["Meta"])
async def root():
    return ApiResponse.ok({"docs": "/docs", "api": settings.API_V1_PREFIX}, msg="AI知识库系统")


@app.get(
    "/health",
    response_model=ApiResponse[dict],
    summary="健康检查（含数据库探活）",
    tags=["Meta"],
)
async def health():
    """真的连一次库，而不是只报版本号

    状态码必须是 503：Docker healthcheck / k8s readiness / CI 只看状态码，不看 body。
    响应体只给类别（dialect、缺哪几张表），**不给连接串和异常原文** ——
    里面含用户名密码和内网地址，`/health` 通常是无需鉴权就能访问的。
    """
    code, data = await startup.health_payload()
    error = (data.get("database") or {}).get("error")
    if code == 200:
        return ApiResponse.ok(data, msg="服务正常")
    if (data.get("database") or {}).get("ephemeral"):
        return _envelope(code, "服务正常（内存库：测试/临时车道，不代表持久化状态）", data)
    if error == startup.ERROR_MISSING_TABLES:
        return _envelope(code, "数据库可连但表不完整：请先运行 python scripts/init_db.py", data)
    if error == startup.ERROR_NO_METADATA:
        return _envelope(code, "ORM 元数据为空，检查 app/db/models.py 是否被导入", data)
    return _envelope(code, "数据库不可用：请检查 DATABASE_URL 与 PostgreSQL 是否在运行", data)
