"""路由层公共依赖：数据库 Session、当前登录用户"""

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Annotated, Literal

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ForbiddenError, NotFoundError, ParamError, UnauthorizedError
from app.core.security import decode_access_token
from app.db.crud import kb_crud, token_crud, user_crud
from app.db.models import User
from app.db.psql_config import AsyncSessionLocal, get_db
from app.services import user_cache
from app.services.kb_access import KbAccess, resolve

# auto_error=False: 没带 Authorization 时由我们自己返回统一的 {"status":401,...}
bearer_scheme = HTTPBearer(auto_error=False, description="JWT，格式: Bearer <access_token>")

# 把 Session 类型固定下来，路由里写 `db: DB` 即可
DB = Annotated[AsyncSession, Depends(get_db)]


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DB,
) -> User:
    """从 Authorization 头解析 JWT，取出当前登录用户

    校验链路: 有 token -> token 能解析 -> 用户还在 -> token 没被登出/过期

    两条和缓存有关的硬规矩（改这里之前先读）：
      1. **users 那一行走 Redis 旁路缓存**（cache-aside）。命中时返回的是脱离 Session 的
         transient 实例 —— 只读字段安全。**要改这个用户的路由必须重新回源**
         （`user_cache.load_for_write`：update / reset_password / delete），
         否则会出现"属性改了、commit 什么都没写、接口还返回 200"这种最难查的 bug。
      2. **token 是否被吊销永远查库，绝不进缓存**。
         不然"登出/改密后旧 token 立刻失效"会被 TTL 变成"几分钟内还能用"，
         那是拿安全属性换性能。
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("未登录，请先携带 Authorization: Bearer <token>")

    token = credentials.credentials
    payload = decode_access_token(token)
    if not payload or not payload.get("sub"):
        raise UnauthorizedError("登录凭证无效或已过期，请重新登录")

    user = await user_cache.get_user_cached(payload["sub"], db)
    if user is None or user.deleted_at is not None:
        raise UnauthorizedError("账号不存在或已注销，请重新登录")
    if not user.is_active:
        raise ForbiddenError("账号已被禁用，请联系管理员")

    # 这一步**不能缓存**（见上面第 2 条）：登出、改密、被踢下线只体现在 tokens 表里
    if not await token_crud.is_token_active(token, db):
        raise UnauthorizedError("登录已失效（已登出或被踢下线），请重新登录")

    return user


def get_session_factory():
    """返回"还能再开一个 Session"的工厂，给流式接口收尾落库用

    SSE 的生成器活得比请求函数久：请求级 Session（get_db）可能已经随依赖栈退出被还回连接池，
    而且让一条数据库连接陪跑几十秒的模型流是在白占池子。所以流式车道要的是工厂不是 Session。
    测试用 dependency_overrides 把它换成临时库的 sessionmaker（见 tests/conftest.py）。
    """
    return AsyncSessionLocal


async def get_current_token(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """取请求头里的原始 JWT 字符串（退出登录时要按它删掉库里的记录）"""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("未登录，请先携带 Authorization: Bearer <token>")
    return credentials.credentials


async def get_optional_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    db: DB,
) -> User | None:
    """可选登录：没带 token 返回 None，带了就必须是合法的"""
    if credentials is None or not credentials.credentials:
        return None

    payload = decode_access_token(credentials.credentials)
    if not payload or not payload.get("sub"):
        raise UnauthorizedError("登录凭证无效或已过期，请重新登录")

    user = await user_cache.get_user_cached(payload["sub"], db)
    if user is None or user.deleted_at is not None or not user.is_active:
        return None
    # 之前漏了这一步：已吊销的 token 不该再算登录
    if not await token_crud.is_token_active(credentials.credentials, db):
        return None
    return user


async def require_admin_dep(user: User = Depends(get_current_user)) -> User:
    """管理员校验：直接看 users.role

    第一个管理员从哪来：注册时用户名命中 settings.ADMIN_USERNAMES 就写 role=admin，
    之后正常权限都走 role 字段，不再依赖配置。
    """
    if not user.is_admin:
        raise ForbiddenError("需要管理员权限")
    return user


AccessMode = Literal["read", "write", "manage"]


def require_kb_access(mode: AccessMode = "read"):
    """知识库权限依赖注入工厂：mode ∈ {"read", "write", "manage"}

    kb_id 从路径参数或 query 参数里取（两种写法的路由都能用同一个依赖）。
    判定规则全部委托 app/services/kb_access.py，这里只负责取参数 + 抛统一异常：
        没带 kb_id                       → 400
        库不存在 / 已软删 / 连 read 都没有 → 404（不暴露别人的库是否存在）
        能 read 但不够 write/manage       → 403，msg 说清需要什么权限
    """
    async def _dep(
        request: Request,
        current_user: User = Depends(get_current_user),
        db: AsyncSession = Depends(get_db),
    ) -> KbAccess:
        kb_id = request.path_params.get("kb_id") or request.query_params.get("kb_id")
        if not kb_id:
            raise ParamError("缺少 kb_id 参数")

        kb = await kb_crud.get_kb(kb_id, db)
        if kb is None:
            raise NotFoundError("知识库不存在")

        access = await resolve(kb, current_user.id, db)
        if not access.can_read:
            raise NotFoundError("知识库不存在")
        if mode == "write":
            access.require_write()
        elif mode == "manage":
            access.require_manage()
        return access

    return _dep


KBReader = Annotated[KbAccess, Depends(require_kb_access("read"))]
KBWriter = Annotated[KbAccess, Depends(require_kb_access("write"))]
KBManager = Annotated[KbAccess, Depends(require_kb_access("manage"))]

async def lookup_reset_account(
    request: Request,
    db: DB,
) -> User | None:
    """「忘记密码」申请车道：按 account（用户名或邮箱）找账号，**找不到就返回 None**

    为什么单独立一个依赖，而不是在路由里现查：
      1. 这条车道没有登录态可言（用户正因为登不进去才用它），套不了 `get_current_user`；
      2. "存在与否"和"能不能发链接"是两件事。路由拿到 None 之后**仍然回 200 + 同一句话**，
         陌生人无法从响应里区分"没这个账号"和"有账号"—— 防枚举是这条车道唯一的安全属性，
         所以判断结果必须回到路由手里，而不是在依赖里直接 raise NotFoundError；
      3. is_active / 已注销这类判断**不在这里做**（那是业务规则，归 password_reset 服务层）。

    account 从 body 里读（`request.json()`，Starlette 会缓存，和路由自己的 Pydantic
    模型不冲突）：不复用 UserLogin —— 那个模型必填 password，语义完全不对。
    """
    account = await _read_account_field(request)
    if not account:
        return None
    return await user_crud.get_user_by_account(account, db)


async def _read_account_field(request: Request) -> str:
    payload = await _safe_json(request)
    value = payload.get("account") or payload.get("username") or payload.get("email")
    return str(value).strip() if value else ""


async def _safe_json(request: Request) -> dict:
    """请求体不是合法 JSON / 不是对象时返回空字典，而不是抛 400

    为什么不能抛：抛了就是"格式不对"和"账号不存在"两种响应，
    陌生人可以拿这个差异枚举账号。这条车道宁可后面因为字段缺失回一句统一提示。
    """
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001 - 非法 JSON 是外部输入，不是故障
        return {}
    return data if isinstance(data, dict) else {}


CurrentUser = Annotated[User, Depends(get_current_user)]
AdminUser = Annotated[User, Depends(require_admin_dep)]
CurrentToken = Annotated[str, Depends(get_current_token)]
SessionFactory = Annotated[Callable[[], AbstractAsyncContextManager[AsyncSession]], Depends(get_session_factory)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
# 「忘记密码」两条匿名车道（申请 / 验码）都先按 account 找账号。
# None = 查不到，**由服务层/路由决定回统一文案**（这里绝不能 raise，否则就是账号枚举机）
ResetAccount = Annotated[User | None, Depends(lookup_reset_account)]
