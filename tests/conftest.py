"""pytest 公共 fixture

测试不依赖真实的 PostgreSQL：每个用例一个临时文件 SQLite。
（主键在 Python 端就生成 uuid，见 app/db/models.py，所以换库不用改代码）
"""

import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAMES", "admin")
os.environ.setdefault("SQL_ECHO", "False")
# 测试车道**不往仓库的 logs/ 写文件**：跑一次全量会往里塞几万行噪声（和 data/ 里那些孤儿目录
# 一样的下场）。日志层的文件行为在 tests/test_logging_conf.py 里用 tmp_path 显式测。
os.environ.setdefault("LOG_TO_FILE", "False")
# 上传接口默认会丢一个后台解析任务，而测试里全局引擎是**没建表的内存库**：
# 任务跑了只会制造 "no such table" 噪声。索引逻辑用 process_document(session=...) 直接测。
os.environ.setdefault("DOCUMENT_INDEX_ON_UPLOAD", "False")
# 长期记忆的业务默认已经是 pgvector（唯一生产车道），但测试连的是临时 SQLite：
# 走 pgvector 只会在第一次读写时撞一次"连不上"然后降级成不记，断言全是空，
# 那等于把 68 条记忆用例变成"什么也没测"。所以测试车道显式钉在离线 self-test 后端 local。
# （store.py 里"pgvector 连不上不再自动退 local"这条规矩，正是为了让这种选择必须是显式的）
os.environ.setdefault("MEMORY_BACKEND", "local")
# 测试车道**永远不碰真 Redis**（本机可能装着 redis、也可能没装；CI 机器上更是随机）。
# 缓存的行为用假客户端在 tests/test_cache_user.py 里显式测，
# 而 /health 之类的用例必须保持确定性 —— 一个靠外部服务状态的测试比没写更糟。
os.environ.setdefault("REDIS_ENABLED", "False")

# 两个 key 必须**硬置空**，不能用 setdefault：Settings 读的是 `.env` 文件，
# 本机一旦填了真 key，"没配 key 就该明确报错"的用例就不再报错（DID NOT RAISE），
# 而短期记忆折叠那条会**真的联网调 DeepSeek**，摘要内容变成模型自由发挥 → 断言随机失败，
# 顺带烧配额。环境变量优先级高于 .env 文件，所以空串能把配置里的值压住。
# （需要"有 key"场景的用例自己在 monkeypatch 里造，不靠本机环境。）
os.environ["LLM_API_KEY"] = ""
os.environ["EMBEDDING_API_KEY"] = ""

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from app.api.deps import get_session_factory
from app.db import models  # noqa: F401,E402  建表前必须导入，否则表不在 metadata 里
from app.db.base import Base  # noqa: E402
from app.db.psql_config import get_db  # noqa: E402


def _create_tables(url: str) -> None:
    """用独立的事件循环建表，避免连接跨 loop 复用"""
    ddl_engine = create_async_engine(url, poolclass=NullPool)

    async def _run() -> None:
        async with ddl_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_run())
    asyncio.run(ddl_engine.dispose())


def _make_session_maker(engine):
    return async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


@pytest.fixture()
def db_url(tmp_path) -> str:
    """每个用例一个独立的库文件"""
    return f"sqlite+aiosqlite:///{tmp_path / 'kb_test.db'}"


@pytest.fixture()
def engine(db_url):
    _create_tables(db_url)
    eng = create_async_engine(db_url, poolclass=NullPool)

    # SQLite 默认不启用外键，级联删除/约束相关用例必须打开它
    @event.listens_for(eng.sync_engine, "connect")
    def _fk_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    yield eng
    asyncio.run(eng.dispose())


@pytest.fixture()
def run(engine):
    """在 ORM 上直接跑一段异步代码，用来造种子数据：

        doc = run(lambda session: make_document(session, kb_id=..., owner_id=...))
    """
    maker = _make_session_maker(engine)

    def _runner(fn):
        async def _inner():
            async with maker() as session:
                return await fn(session)

        return asyncio.run(_inner())

    return _runner


@pytest.fixture()
def client(engine):
    from app.main import app

    maker = _make_session_maker(engine)

    async def override_get_db():
        async with maker() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    # 流式车道收尾落库用的是「自己再开一个 Session」（app/api/deps.get_session_factory）。
    # 不覆盖它，SSE 测试就会去连那个没建表的内存库：报错信息还特别像业务 bug。
    app.dependency_overrides[get_session_factory] = lambda: maker
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


# ---------------- 注册 / 登录相关夹具 ----------------
@pytest.fixture()
def register(client):
    def _register(username: str = "zhangsan", email: str = "zhangsan@example.com", password: str = "abc123456"):
        return client.post(
            "/api/v1/user/register",
            json={"username": username, "email": email, "password": password},
        )

    return _register


@pytest.fixture()
def login(client):
    def _login(account: str, password: str = "abc123456"):
        return client.post("/api/v1/user/login", json={"account": account, "password": password})

    return _login


def _login_headers(client, username: str, password: str = "abc123456") -> dict:
    resp = client.post("/api/v1/user/login", json={"account": username, "password": password})
    return {"Authorization": f"Bearer {resp.json()['data']['access_token']}"}


@pytest.fixture()
def user_headers(client, register) -> dict:
    """普通用户 zhangsan 的登录态"""
    register(username="zhangsan", email="zhangsan@example.com")
    return _login_headers(client, "zhangsan")


@pytest.fixture()
def admin_headers(client, register) -> dict:
    """管理员登录态（用户名命中 ADMIN_USERNAMES，注册时 role 直接给 admin）"""
    register(username="admin", email="admin@example.com")
    return _login_headers(client, "admin")


@pytest.fixture()
def other_headers(client, register) -> dict:
    """另一个普通用户 lisi 的登录态"""
    register(username="lisi", email="lisi@example.com")
    return _login_headers(client, "lisi")


@pytest.fixture()
def logged_in(client, register) -> dict:
    """注册 + 登录 zhangsan，返回 {"headers":..., "user_id":...}，供需要种子数据的用例使用"""
    register(username="zhangsan", email="zhangsan@example.com")
    data = client.post("/api/v1/user/login", json={"account": "zhangsan", "password": "abc123456"}).json()["data"]
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "user_id": data["user"]["id"],
    }
