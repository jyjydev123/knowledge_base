"""Redis 旁路缓存（cache-aside）：客户端、降级与三条纪律

三条铁律（违反的代价是"数据看起来是对的，但一直在骗人"，所以写进代码也写进注释）：

  1. **缓存只是加速器，永远不是数据源**。连不上 / 超时 / 键不存在 / 值不是合法 JSON，
     统统当"没缓存"处理并回源数据库，**绝不把 Redis 异常抛给路由**。
     Redis 挂了就登不上系统，这种设计不能接受。
  2. **写操作固定顺序 = 先落库，再删键**（delete-after-write），不是"先更新缓存再落库"，
     也不是"落库后更新缓存"。缓存里没有业务真相，删键失败最坏是旧值多活一个 TTL；
     反过来先动缓存就会读到写过期的东西。
  3. **键一律带前缀和版本号**（`{prefix}:user:v1:{id}`）。字段结构变了不用清库，
     把版本号 +1 即可，旧键自然过期；同一台 Redis 上跑别的应用也不会互相踩。

降级必须**可见**：第一次失败时记下原因并打一条 warning（只打一次，别刷屏），
`/health` 里能看到"缓存现在到底有没有生效"。静默退回数据库是最坏的做法 ——
所有人都会以为加速在做，直到某天容量出问题。
"""

from __future__ import annotations

import json
import logging
import random
import time
from typing import Any

from app.core.config import settings

logger = logging.getLogger("app.core.cache")

# 模块级单例。Redis 客户端自己带连接池，每条请求 new 一个等于每次都要重新 TCP + 握手，
# 那才是真的把加速器变成减速器。
# 熔断：连续失败之后这么多秒内**不再尝试连接**，直接走数据库。
# 为什么必须有：Redis 没起而 REDIS_ENABLED 还是 True 时，每条需要登录的请求都要
# 等一次 socket_connect_timeout（默认 0.5s）才 fail-open 回源 —— 加速器当场变成减速器，
# 而且监控上看不出来（每个请求都"成功"了）。本机没装 Redis 的人会第一时间撞上这个。
CIRCUIT_COOLDOWN_SECONDS = 10.0

_client: Any = None
# enabled  = 客户端建起来了（不代表连得上）
# last_ok  = **上一次真操作成功过**（get/set/ping 任一次成功）。这两个必须分开：
#            redis.asyncio 的客户端是懒连接的，构造成功≠能用，把它报成"缓存已生效"
#            会让运维在 /health 里看到一条假绿，而实际每一次请求都在超时回源。
_state: dict[str, Any] = {
    "enabled": False, "last_ok": False, "reason": "", "hits": 0, "misses": 0, "errors": 0,
    "cooldown_until": 0.0,      # 熔断到期时间（monotonic 秒），0 = 不在熔断里
    "trips": 0,                 # 拉过几次闸：/health 里看得见，用来区分"偶尔抽风"和"根本没装"
}
_warned = False


def _mark_ok() -> None:
    """一次真操作成功：把"缓存到底在不在干活"拨回 true，并把警告重新武装一次"""
    global _warned
    _state["last_ok"] = True
    _state["reason"] = ""
    _state["cooldown_until"] = 0.0
    _warned = False


def _mark_disabled(reason: str, *, error: bool = False) -> None:
    """记录"这条车道为什么没生效"，并且只警告一次（同一原因刷屏没有信息量）"""
    global _warned
    _state["last_ok"] = False
    _state["reason"] = reason
    if error:
        # 拉闸：接下来 10 秒的缓存请求全部短路，省掉每次一个 socket 超时。
        # 不需要人工恢复 —— 到点自动再试一次，成功就合闸。
        now = time.monotonic()
        if _state["cooldown_until"] <= now:
            # 只在"合闸 → 拉闸"那一刻计数，否则一次故障期被刷成一堆 trips，看不出故障次数
            _state["trips"] += 1
        _state["cooldown_until"] = now + CIRCUIT_COOLDOWN_SECONDS
        _state["errors"] += 1
    if not _warned:
        _warned = True
        logger.warning("Redis 缓存未生效（fail-open 回源数据库）: %s", reason)


def build_key(*parts: Any) -> str:
    """拼一个带前缀的键：kb:user:v1:{id}

    分隔符固定 `:`，各段内部的空格删掉、冒号换成 `-` —— 段里留冒号会把一个段
    撑成好几个段（`build_key("user:v1")` 变出两段），换掉之后既不撞键也好读。
    """
    cleaned = [str(part).strip().replace(" ", "").replace(":", "-") for part in parts if str(part).strip()]
    return ":".join([settings.CACHE_KEY_PREFIX, *cleaned])


def ttl_with_jitter(seconds: int) -> int:
    """TTL 加 ±10% 抖动，避免同一批键同时到期把数据库打一下

    为什么值得多写四行：缓存里的用户身份是按登录浪潮一批批写进去的，
    TTL 一模一样的话，每隔 N 分钟会出现一波集体穿透（缓存雪崩的入门款）。
    """
    base = max(int(seconds), 1)
    spread = max(int(base * 0.1), 1)
    # 最后再兜一次底：base=1 时 1 + (-1) = 0，而 TTL=0 在 Redis 里是"不设过期"，
    # 那等于把这条车道变成内存泄漏。抖动只能打散时间，不该改变"必须会过期"这个承诺。
    return max(1, base + random.randint(-spread, spread))  # noqa: S311 - 只为打散过期时间


async def get_client():
    """拿到可用的 Redis 客户端；拿不到就返回 None（调用方必须能处理"没有缓存"）"""
    global _client, _warned

    if not settings.REDIS_ENABLED:
        _mark_disabled("REDIS_ENABLED=False，缓存车道被显式关闭")
        return None
    if _client is not None:
        # 熔断期内连试都不试：调用方拿到的语义就是"没有缓存"，立刻回源数据库
        if _state["cooldown_until"] > time.monotonic():
            return None
        # 客户端已就绪（上一轮建好的，或测试里注入的假客户端）时也要把就绪位拨对：
        # 否则 status() 会报"缓存没配"，而实际情况只是这条车道还没成功操作过一次。
        _state['enabled'] = True
        return _client

    try:
        import redis.asyncio as aioredis  # 延迟 import：没装 redis 时整个应用照样能起
    except ImportError:
        _mark_disabled("未安装 redis 包（uv add redis）")
        return None

    try:
        _client = aioredis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,                 # 取出来就是 str，省掉每处 .decode()
            socket_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            socket_connect_timeout=settings.REDIS_SOCKET_TIMEOUT_SECONDS,
            max_connections=settings.REDIS_MAX_CONNECTIONS,
            health_check_interval=30,
        )
    except Exception as exc:  # noqa: BLE001 - URL 写错之类的问题不该带崩应用
        _client = None
        _mark_disabled(f"Redis 客户端构造失败: {type(exc).__name__}: {exc}", error=True)
        return None

    # 注意这里**不**把 last_ok 设成 true：客户端是懒连接的，这一步只说明"配好了"
    _state["enabled"] = True
    _state["cooldown_until"] = 0.0
    logger.info(
        "Redis 客户端已就绪（%s，TTL=%ss）—— 是否真连得上看 /health 的 cache 一节",
        _transport(settings.REDIS_URL),
        settings.USER_CACHE_TTL_SECONDS,
    )
    return _client


async def get_json(key: str) -> dict | None:
    """读一个 JSON 值。任何异常/坏值都当成"没命中"，绝不抛"""
    client = await get_client()
    if client is None:
        return None
    try:
        raw = await client.get(key)
    except Exception as exc:  # noqa: BLE001
        _mark_disabled(f"读取失败（{type(exc).__name__}: {exc}）", error=True)
        return None
    if raw is None:
        _state["misses"] += 1
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        # 键被别人写过非 JSON 内容：删掉它，别把一个坏数据长期当成命中
        _state["misses"] += 1
        await delete(key)
        return None
    if not isinstance(value, dict):
        _state["misses"] += 1
        await delete(key)
        return None
    _mark_ok()
    _state["hits"] += 1
    return value


async def set_json(key: str, value: dict, ttl_seconds: int | None = None) -> bool:
    """写一个 JSON 值（带 TTL）。失败只记不抛 —— 写缓存失败不影响这次请求的正确性"""
    client = await get_client()
    if client is None:
        return False
    ttl = ttl_with_jitter(settings.USER_CACHE_TTL_SECONDS if ttl_seconds is None else ttl_seconds)
    try:
        await client.set(key, json.dumps(value, ensure_ascii=False, default=str), ex=ttl)
    except Exception as exc:  # noqa: BLE001
        _mark_disabled(f"写入失败（{type(exc).__name__}: {exc}）", error=True)
        return False
    _mark_ok()
    return True


async def delete(*keys: str) -> int:
    """删键（铁律 2 的落点：业务写库成功之后调用它）。返回真正删掉的个数"""
    targets = [key for key in keys if key]
    if not targets:
        return 0
    client = await get_client()
    if client is None:
        return 0
    try:
        return int(await client.delete(*targets) or 0)
    except Exception as exc:  # noqa: BLE001
        # 删失败必须**打日志**：它的后果是"旧值最多再活一个 TTL"，
        # 不吭声的话就变成"改了资料但刷新看不到"，而且没人知道是缓存的问题
        logger.warning("删除缓存键失败（旧值会存活到 TTL 到期）: %s %s", targets, exc)
        _state["errors"] += 1
        return 0


def status() -> dict[str, Any]:
    """给 /health 用：缓存这条车道现在到底什么状态（如实报，不粉饰）"""
    return {
        "configured": settings.REDIS_ENABLED,
        "client_ready": bool(_state["enabled"]),
        "last_ok": bool(_state["last_ok"]),
        "circuit_open": _state["cooldown_until"] > time.monotonic(),
        "circuit_trips": _state["trips"],
        "active": bool(_state["enabled"] and _state["last_ok"]),
        # 只报传输方式，**不报 host/端口/密码**：/health 通常无需鉴权就能访问，
        # 而项目对健康检查的既有规矩就是"不给连接信息"（见 tests/test_startup_health.py）
        "transport": _transport(settings.REDIS_URL),
        "reason": _state["reason"],
        "last_error_count": _state["errors"],
        "hits": _state["hits"],
        "misses": _state["misses"],
    }


def _transport(url: str) -> str:
    """把连接串收缩成一个不含任何地址信息的安全标签

    redis:// → "redis"、rediss:// → "rediss(tls)"、unix:// → "unix"、
    别的写法一律 "unknown"。**永远不返回 host/端口/密码/db 号** ——
    这既是为了 /health 不泄露内网拓扑，也是为了日志里不会漂着密码。
    """
    text = str(url or "")
    if text.startswith("rediss://"):
        return "rediss(tls)"
    if text.startswith("redis://"):
        return "redis"
    if text.startswith("unix://"):
        return "unix"
    return "unknown"


async def ping() -> tuple[bool, str]:
    """主动探一次（/health 用）。没启用/连不上都返回 (False, 原因)，不抛"""
    if not settings.REDIS_ENABLED:
        return False, "REDIS_ENABLED=False"
    client = await get_client()
    if client is None:
        return False, _state["reason"] or "客户端不可用"
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        _mark_disabled(f"ping 失败（{type(exc).__name__}: {exc}）", error=True)
        return False, f"{type(exc).__name__}: {exc}"
    _mark_ok()
    return True, "可用"


async def aclose() -> None:
    """进程退出时关掉连接池（lifespan 里调用）"""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa: BLE001 - 关闭阶段的异常没有可处理的对象
            pass
        _client = None


def reset_state() -> None:
    """换配置 / 跑测试后用一次，避免旧单例带着上一条连接的池子和计数器继续跑"""
    global _client, _warned
    _client = None
    _warned = False
    _state.update({
        "enabled": False, "last_ok": False, "reason": "", "hits": 0, "misses": 0, "errors": 0,
        "cooldown_until": 0.0, "trips": 0,
    })


__all__ = [
    "aclose",
    "build_key",
    "delete",
    "get_client",
    "get_json",
    "ping",
    "reset_state",
    "set_json",
    "status",
    "ttl_with_jitter",
]
