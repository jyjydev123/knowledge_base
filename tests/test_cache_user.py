"""Redis 旁路缓存的测试：假客户端、fail-open、失效顺序，以及"缓存里不该有什么"

测试车道**永远不碰真 Redis**（conftest 里 REDIS_ENABLED=False），
所有行为都靠注入一个假客户端来验 —— 这条车道一旦需要"装了 redis 才能测"，
它就会在 CI 和别的机器上变成没人跑过的死代码，那比没有缓存更危险。
"""

import asyncio
import json
import time

import pytest

from app.core import cache
from app.core.config import settings
from app.db.crud import user_crud
from app.services import user_cache


class FakeRedis:
    """只实现我们真正用到的那几个命令，行为尽量贴近 redis-py 的异步客户端"""

    def __init__(self, *, fail=False):
        self.store: dict[str, str] = {}
        self.fail = fail
        self.ttls: dict[str, int] = {}
        self.closed = False

    async def get(self, key):
        if self.fail:
            raise ConnectionError("connection refused")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("connection refused")
        self.store[key] = value
        self.ttls[key] = ex or 0
        return True

    async def delete(self, *keys):
        if self.fail:
            raise ConnectionError("connection refused")
        return sum(1 for key in keys if self.store.pop(key, None) is not None)

    async def ping(self):
        if self.fail:
            raise ConnectionError("connection refused")
        return True

    async def aclose(self):
        self.closed = True


@pytest.fixture()
def fake(monkeypatch):
    """开缓存车道 + 注入假客户端，返回那个假客户端好断言键和 TTL"""
    monkeypatch.setattr(settings, "REDIS_ENABLED", True)
    client = FakeRedis()
    cache.reset_state()
    monkeypatch.setattr(cache, "_client", client)
    yield client
    cache.reset_state()


# --------------------------------------------------------------------------
# 键与 TTL
# --------------------------------------------------------------------------


def test_键带前缀和版本():
    assert cache.build_key("user", "v1", "abc") == "kb:user:v1:abc"
    # 段里混进冒号会被换成 `-`：不然一个段会撑成好几个段，键的含义就漂了
    assert cache.build_key("user:v1:abc") == "kb:user-v1-abc"
    assert cache.build_key("user", "  ", "x") == "kb:user:x"


def test_ttl有抖动不至于同时集体过期():
    values = {cache.ttl_with_jitter(300) for _ in range(200)}
    assert len(values) > 5, "全都一样说明没抖，同一批键会在同一秒集体穿透"
    assert all(270 <= value <= 330 for value in values)
    # 下界必须是 1：Redis 里 TTL=0 的语义是"永不过期"，抖动绝不能踩到这个值
    assert all(cache.ttl_with_jitter(tiny) >= 1 for tiny in (0, -5, 1, 2, 10))


def test_transport不泄露任何地址信息():
    assert cache._transport("redis://user:pw@10.0.0.8:6379/3") == "redis"
    assert cache._transport("rediss://host") == "rediss(tls)"
    assert cache._transport("unix:///run/redis.sock") == "unix"
    assert cache._transport("") == "unknown"
    assert "10.0.0.8" not in json.dumps(cache.status()) and "pw" not in json.dumps(cache.status())


# --------------------------------------------------------------------------
# 读写与 fail-open
# --------------------------------------------------------------------------


def test_未命中返回None并记一笔(fake):
    assert asyncio.run(cache.get_json("kb:user:v1:x")) is None
    assert cache.status()["misses"] == 1


def test_命中时计数加一(fake):
    asyncio.run(cache.set_json("kb:a", {"id": "1"}))
    assert asyncio.run(cache.get_json("kb:a")) == {"id": "1"}
    state = cache.status()
    assert (state["hits"], state["misses"]) == (1, 0)
    assert state["active"] is True and state["last_ok"] is True
    assert 270 <= fake.ttls["kb:a"] <= 330, "写入必须带 TTL，永不过期的键就是内存泄漏"


def test_坏值当未命中并顺手删掉(fake):
    """键里存了非 JSON / 不是 dict 的东西：不能当命中，也不能抛 —— 自愈比报错有用"""
    fake.store["kb:bad"] = "not json at all"
    assert asyncio.run(cache.get_json("kb:bad")) is None
    assert "kb:bad" not in fake.store

    fake.store["kb:list"] = json.dumps([1, 2, 3])
    assert asyncio.run(cache.get_json("kb:list")) is None


def test_redis挂了绝对不能把请求带崩(fake):
    """fail-open 是这套设计的前提：Redis 故障不能让登录/问答变成 500"""
    fake.fail = True
    assert asyncio.run(cache.get_json("kb:any")) is None
    assert asyncio.run(cache.set_json("kb:any", {"a": 1})) is False
    assert asyncio.run(cache.delete("kb:any")) == 0
    ok, detail = asyncio.run(cache.ping())
    assert ok is False and "ConnectionError" in detail
    assert cache.status()["active"] is False and cache.status()["last_ok"] is False


def test_没开缓存时一切照常但明确说原因(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_ENABLED", False)
    cache.reset_state()
    assert asyncio.run(cache.get_json("kb:x")) is None
    assert asyncio.run(cache.set_json("kb:x", {"a": 1})) is False
    ok, detail = asyncio.run(cache.ping())
    assert ok is False and "REDIS_ENABLED" in detail


def test_客户端建好不等于缓存生效(fake):
    """这条就是当初那个假绿 bug 的回归测试：
    redis.asyncio 是懒连接，构造成功就报 active=true 会让运维看不见"其实每次都连不上"。"""
    asyncio.run(cache.get_client())  # 真实链路上每个操作都会先经过这里
    state = cache.status()
    assert state["client_ready"] is True, "客户端就绪和能不能连是两件事"
    assert state["last_ok"] is False and state["active"] is False, "没成功操作过就不能报成在生效"


# --------------------------------------------------------------------------
# 用户身份这条车道
# --------------------------------------------------------------------------


def test_只读车道_第二次不再查库(fake, run, monkeypatch):
    import app.services.user_cache as uc
    from tests.test_schema_models import _add_user

    user = run(lambda db: _add_user(db, "cachezhang", "cachezhang@example.com"))

    calls: list = []
    real_get = user_crud.get_user_by_id

    async def counting(user_id, db):
        calls.append(user_id)
        return await real_get(user_id, db)

    monkeypatch.setattr(uc.user_crud, "get_user_by_id", counting)

    # 第一次：缓存里没有，必须回源（用真 Session）
    first = run(lambda db: uc.get_user_cached(user.id, db))
    # 第二次：传一个"碰一下就报错"的假 Session —— 能返回就说明它压根没用数据库
    second = asyncio.run(uc.get_user_cached(user.id, _Guard()))

    assert first is not None and second is not None
    assert len(calls) == 1, "第二次必须命中缓存，不然这层缓存等于没做"
    assert second.username == "cachezhang"

    key = uc.cache_key(user.id)
    snapshot = json.loads(fake.store[key])
    assert snapshot["username"] == "cachezhang"
    # 密码哈希**绝不进缓存**：任何外部存储都不该有它（改密/注销因此必须回源）
    assert "hashed_password" not in snapshot
    assert set(snapshot) == {
        "id", "username", "email", "role", "is_active", "deleted", "created_at", "last_login_at"
    }


class _Guard:
    """假的 Session：任何属性访问都报错。用来证明"缓存命中时根本不碰数据库" """

    def __getattr__(self, name):
        raise AssertionError(f"缓存命中时不该用 Session，却访问了 {name}")


def test_写车道一定回源(fake, run, monkeypatch):
    """缓存里明明有值，load_for_write 也必须去查库 —— 改密/注销靠的就是这条"""
    from sqlalchemy import inspect as sa_inspect

    import app.services.user_cache as uc
    from tests.test_schema_models import _add_user

    user = run(lambda db: _add_user(db, "wcache", "wcache@example.com"))
    run(lambda db: user_cache.get_user_cached(user.id, db))
    assert fake.store, "前置条件：只读车道已经把键写进缓存了"

    calls: list = []
    real_get = user_crud.get_user_by_id

    async def counting(user_id, db):
        calls.append(user_id)
        return await real_get(user_id, db)

    monkeypatch.setattr(uc.user_crud, "get_user_by_id", counting)

    async def _load(db):
        fresh = await uc.load_for_write(user.id, db)
        # 受管对象这件事必须在 Session 还开着的时候验（出了 run 夹具它就 detach 了）
        return fresh, sa_inspect(fresh).session is not None

    fresh, managed = run(_load)
    assert len(calls) == 1, "写车道不许读缓存"
    assert fresh is not None and managed is True, "写车道必须返回受管对象，只读车道那个是 transient"


def test_失效就是删键(fake, run):
    from tests.test_schema_models import _add_user

    user = run(lambda db: _add_user(db, "invalid", "invalid@example.com"))
    key = user_cache.cache_key(user.id)
    asyncio.run(cache.set_json(key, {"id": str(user.id), "username": "invalid", "email": "e@x.co",
                                     "role": "user", "is_active": True, "deleted": False,
                                     "created_at": None, "last_login_at": None}))
    assert key in fake.store
    assert asyncio.run(user_cache.invalidate(user.id)) == 1
    assert key not in fake.store
    # 幂等：本来就没有也返回 0，不抛
    assert asyncio.run(user_cache.invalidate(user.id)) == 0
    assert asyncio.run(user_cache.invalidate("not-a-uuid")) == 0


def test_坏快照回源而不是信缓存(fake, run):
    """字段缺了/类型变了（比如改了 SCHEMA_VERSION 但旧键还在）→ 当未命中，回数据库"""
    from tests.test_schema_models import _add_user

    user = run(lambda db: _add_user(db, "broken", "broken@example.com"))
    key = user_cache.cache_key(user.id)
    fake.store[key] = json.dumps({"id": str(user.id)})  # 缺 username 等字段

    got = run(lambda db: user_cache.get_user_cached(user.id, db))
    assert got is not None and got.username == "broken", "解码失败必须回源，不能返回 None 把人踢下线"
    assert json.loads(fake.store[key])["username"] == "broken", "回源之后要顺手把坏键修正"


def test_连不上时不会每条请求都等一次超时(fake):
    """熔断（circuit breaker）。没装 Redis 又忘了关 REDIS_ENABLED 的人会撞上这个：
    不熔断的话每条需要登录的请求都要等一次 socket_connect_timeout 才 fail-open，
    缓存直接把服务变成减速器，而监控上看每个请求都是成功的。
    """
    fake.fail = True

    async def _hammer():
        for _ in range(200):
            await cache.get_json("kb:user:v1:x")

    started = time.monotonic()
    asyncio.run(_hammer())
    elapsed = time.monotonic() - started

    state = cache.status()
    assert state["circuit_open"] is True, "第一次失败就该拉闸"
    assert state["circuit_trips"] == 1, "同一次故障期只算一次，否则 trips 会被刷屏"
    assert state["last_error_count"] == 1, "200 次请求只真撞了一次远端"
    assert elapsed < 0.5, f"熔断没起作用，200 次尝试花了 {elapsed:.2f}s"

    # 恢复是**半开**的：冷却期一到就再试一次，成功就合闸。
    # 这里把冷却时间拨到过去而不是真等 10 秒 —— 测试要验的是状态机，不是墙钟。
    cache._state["cooldown_until"] = time.monotonic() - 1
    fake.fail = False
    assert asyncio.run(cache.set_json("kb:user:v1:back", {"id": "1"})) is True
    assert cache.status()["circuit_open"] is False, "成功一次必须立刻合闸"
    assert cache.status()["active"] is True
