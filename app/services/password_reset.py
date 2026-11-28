"""「忘记密码」这条车道：申请 → 送达验证码 → 校验 → 设新密码（TODO.md §6.1）

2026-11-28 从「长令牌 + 邮件链接」改成「6 位数字验证码」（用户要求：本机自测直接抄日志里的码），
五条设计决定：

1. **短码必须 bcrypt，不能 sha256**。10^6 种可能的东西用无盐快哈希存 = 库被人拖走
   就等明文。所以 `app/db/crud/reset_crud.py` 用 `get_password_hash`/`verify_password`，
   代价是不能按哈希查行（每次哈希结果不同）—— 改成"按 account 找用户 → 取他那条待用的码
   → 逐字段 verify"。
2. **在线爆破由 attempts 挡住**：库里安全了不等于接口安全。6 位数字一百万种，
   不限尝试次数就是给人送福利。`RESET_CODE_MAX_ATTEMPTS`（默认 5）到顶即作废该条，
   命中 5 次的概率约百万分之五，且申请本身还有账号冷却 + IP 频率两层。
   ⚠️ 响应里**永远不说"还剩几次机会"**：那种提示只在账号存在时出现，等于枚举机。
3. **账号存在与否，响应逐字相同**（防枚举）。"没这个账号"、"码错了"、"码过期了"
   三种失败共用 `INVALID_CODE_MSG`。耗时差异（存在→多两次写库 + 一次 bcrypt）没做定长填充，
   本地/内网接受的取舍，公网暴露前先补填充或在反代限流（docs/DEPLOYMENT.md §6）。
4. **送达通道可插拔且默认不出网**：`console`（打进服务端日志 = 本机自测就靠它，
   终端/`logs/app.log` 里直接抄码）、`file`（落 `var/reset/`，用完整即删）、`smtp`（真发邮件）。
   console/file 都意味着"能看日志/能读 var/ 的人可以改密码"，所以 `./start.sh check`
   会把当前通道和这句话打出来，多人环境必须换 smtp。
5. **改密成功必须吊销该用户全部 access token**（`tokens` 表）+ 作废该账号其余未用的码。
   鉴权那条链路每次都在库里验 token 是否还在（app/api/deps.py 写死的规矩，永不进缓存），
   所以删行 = 全端立刻下线，不需要新机制。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import cache
from app.core.cache import build_key
from app.core.config import settings
from app.core.exceptions import ParamError, RateLimitError
from app.core.security import get_password_hash, verify_password
from app.db.crud import reset_crud, token_crud, user_crud
from app.db.models import PasswordResetCode, User

logger = logging.getLogger("app.services.password_reset")

# 三种失败路径（没这个账号 / 码不对 / 已过期或已作废）**共用这一句**，不给调用方区分的机会
INVALID_CODE_MSG = "验证码无效或已过期，请重新获取"


def masked_account(account: str) -> str:
    """日志里绝不落完整邮箱/用户名（脱敏红线，和 /health 不给连接串同一条规矩）

    邮箱留首字符和域名（`z***@example.com`）足够判断"是不是我以为那个账号"，
    纯用户名则只留长度 —— 用户名在本项目里是登录凭据的一半。
    ⚠️ 这条对**验证码**同样成立：日志里可以出现码（那是 console 车道的送达方式本身），
    但绝不可以出现"用户名 + 码"之外的凭据组合（比如把邮箱和密码一起打出来）。
    """
    text = str(account or "").strip()
    if "@" in text:
        name, _, domain = text.partition("@")
        return f"{name[:1]}***@{domain}"
    return f"{text[:1]}***({len(text)})"


def code_ttl() -> timedelta:
    return timedelta(minutes=max(int(settings.RESET_CODE_TTL_MINUTES), 1))


def _file_target(user_id) -> Path:
    """文件名用 user_id 的 sha256 前 16 位，不直接用用户名/邮箱（目录列表本身也是信息）"""
    digest = hashlib.sha256(str(user_id).encode("utf-8")).hexdigest()[:16]
    return Path(settings.RESET_FILE_DIR or "var/reset") / f"{digest}.txt"


async def _deliver_console(user: User, code: str, expires_at: datetime) -> tuple[bool, str]:
    """把验证码打进服务端日志 —— 本机/单人部署的"模拟发送"，不需要任何外部服务"""
    logger.warning(
        "密码重置验证码（RESET_DELIVERY=console，仅适合单人本地部署）账号=%s 验证码=%s 有效至=%s",
        user.username, code, expires_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )
    return True, "已写入服务端日志"


async def _deliver_file(user: User, code: str, expires_at: datetime) -> tuple[bool, str]:
    target = _file_target(user.id)

    def _write() -> str:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            f"账号: {user.username}\n验证码: {code}\n"
            f"申请时间: {reset_crud.utc_now().isoformat(timespec='seconds')}\n"
            f"过期时间: {expires_at.isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
        # 权限收到最紧：这份文件等于一次改密码的机会
        try:
            target.chmod(0o600)
        except OSError:
            pass
        return str(target)

    try:
        path = await asyncio.to_thread(_write)
    except OSError as exc:
        return False, f"写文件失败: {type(exc).__name__}: {exc}"
    logger.info("密码重置验证码已落文件 user=%s path=%s", user.username, path)
    return True, path


def _send_email_blocking(to_address: str, code: str, expires_at: datetime) -> None:
    """同步发信（在 to_thread 里跑）。SMTP 是阻塞 socket，直接 await 会把事件循环卡住"""
    message = EmailMessage()
    message["Subject"] = "AI 知识库：密码重置验证码"
    message["From"] = settings.SMTP_FROM or settings.SMTP_USER
    message["To"] = to_address
    message.set_content(
        f"你的密码重置验证码是：{code}\n\n"
        f"{settings.RESET_CODE_LENGTH} 位数字，"
        f"{settings.RESET_CODE_TTL_MINUTES} 分钟内有效，最多可尝试 "
        f"{settings.RESET_CODE_MAX_ATTEMPTS} 次；输错次数用完请重新申请。\n"
        "不是你本人操作的话，请忽略本邮件（不改密码不会有任何影响）。\n"
    )
    # SMTP_SSL 的语义按配置注释走：True=隐式 TLS（465），False=明文连上再 STARTTLS（587）。
    # 写成"总是 SMTP + starttls"会让 465 端口握手失败，而且报的是很难读的 SSL 错误
    if settings.SMTP_SSL:
        client = smtplib.SMTP_SSL(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
    else:
        client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=15)
        try:
            client.starttls()
        except smtplib.SMTPNotSupportedError:
            pass      # 服务器不支持 STARTTLS 时按原连接继续（内网自建服务常见），不静默改行为
    try:
        if settings.SMTP_USER and settings.SMTP_PASSWORD:
            client.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
        client.send_message(message)
    finally:
        try:
            client.quit()
        except OSError:
            pass


async def _deliver_smtp(user: User, code: str, expires_at: datetime) -> tuple[bool, str]:
    if not settings.smtp_ready:
        return False, "SMTP_HOST/SMTP_FROM 没配齐"
    if not (user.email or "").strip():
        return False, "该账号没有邮箱"
    try:
        await asyncio.to_thread(_send_email_blocking, user.email, code, expires_at)
    except Exception as exc:  # noqa: BLE001  发信的失败种类太多（DNS/认证/TLS/超时），统一算送达失败
        return False, f"{type(exc).__name__}: {str(exc)[:160]}"
    return True, f"已发送到 {masked_account(user.email)}"


async def deliver(user: User, code: str, expires_at: datetime) -> tuple[bool, str]:
    """按配置的通道把验证码送出去，返回 (成功?, 给日志看的细节)。绝不返回给接口响应"""
    channel = settings.RESET_DELIVERY
    if channel == "file":
        return await _deliver_file(user, code, expires_at)
    if channel == "smtp":
        return await _deliver_smtp(user, code, expires_at)
    return await _deliver_console(user, code, expires_at)


def _uniform_msg() -> str:
    return (
        f"如果该账号存在，验证码已发出（{settings.RESET_CODE_LENGTH} 位数字、"
        f"{settings.RESET_CODE_TTL_MINUTES} 分钟内有效，最多可试 {settings.RESET_CODE_MAX_ATTEMPTS} 次）"
    )


async def send_code(
    account: str,
    db: AsyncSession,
    user: User | None = None,
    client_ip: str | None = None,
) -> str:
    """申请一次验证码。返回给用户看的那句话（**与账号是否存在无关**）

    `user` 由路由的依赖 `ResetAccount` 查好传进来（省一次 SELECT）；
    留成可选参数是为了让服务层能被单独测/单独调（脚本里传 None 就自己查表）。

    限流三层，故意不合并：
      * 同账号冷却 —— **查库**，Redis 关掉/被清也照样生效，这是兜底的那层；
      * 同 IP 窗口 —— 走 Redis 计数器，没有 Redis 就是 None（fail-open）；
      * 尝试次数 —— 在 `verify_and_reset` 里按条计数，防在线爆破 10^6。
    """
    if user is None:
        user = await user_crud.get_user_by_account(account, db)
    if user is None or user.deleted_at is not None or not user.is_active:
        # 没命中也要打一条：不然"用户说没收到"时无法区分"账号写错了"和"通道坏了"
        logger.info("忘记密码：账号未命中（不存在/已注销/已禁用）account=%s ip=%s", masked_account(account), client_ip)
        return _uniform_msg()

    latest = await reset_crud.latest_for_user(user.id, db)
    created = reset_crud._as_utc(latest.created_at) if latest is not None else None
    cooldown = max(int(settings.RESET_REQUEST_COOLDOWN_SECONDS), 0)
    if created is not None and cooldown:
        elapsed = (reset_crud.utc_now() - created).total_seconds()
        if elapsed < cooldown:
            wait = int(cooldown - elapsed) + 1
            logger.info("忘记密码：命中账号冷却 user=%s 还需等 %ss", user.username, wait)
            raise RateLimitError(f"申请太频繁，请 {wait} 秒后再试")

    if client_ip:
        key = build_key("ratelimit", "reset-ip", client_ip)
        count = await cache.incr(key, max(int(settings.RESET_REQUEST_IP_WINDOW_SECONDS), 1))
        if count is not None and count > settings.RESET_REQUEST_IP_LIMIT:
            logger.warning("忘记密码：IP 频率超限 ip=%s count=%s", client_ip, count)
            raise RateLimitError("申请太频繁，请稍后再试")

    # 先作废该账号手里其它未用的码：只让**最新那条**有效（否则有效期被最早那次拉长）
    stale = await reset_crud.invalidate_user_codes(user.id, db)
    if stale:
        logger.info("忘记密码：作废旧验证码 %d 条 user=%s", stale, user.username)

    raw_code = reset_crud.new_raw_code(settings.RESET_CODE_LENGTH)
    expires_at = reset_crud.utc_now() + code_ttl()
    record = await reset_crud.create_code(
        user.id, raw_code, expires_at, db, channel=settings.RESET_DELIVERY, client_ip=client_ip
    )
    if record is None:
        logger.warning("忘记密码：验证码落库失败 user=%s", user.username)
        return _uniform_msg()

    ok, detail = await deliver(user, raw_code, expires_at)
    if not ok:
        # 送不出去的码留在库里只是风险面，直接作废，并且**照样回同一句话**（不泄露账号状态）
        await reset_crud.mark_used(record, db)
        logger.warning(
            "密码重置送达失败，验证码已作废 user=%s 通道=%s 原因=%s", user.username, settings.RESET_DELIVERY, detail
        )
    else:
        logger.info("密码重置验证码已发出 user=%s 通道=%s 细节=%s", user.username, settings.RESET_DELIVERY, detail)
    return _uniform_msg()


async def verify_and_reset(
    account: str,
    code: str,
    new_password: str,
    db: AsyncSession,
    user: User | None = None,
) -> tuple[User, int]:
    """验码 + 设新密码，返回 (用户, 被吊销的登录 token 数)；任何无效一律 ParamError(同一句话)

    顺序是刻意的：**先作废这条码，再改密码**。
    反过来（先改密码、后置 used_at）一旦中间那步抛异常，就会留下
    "密码已经换了、码还能再用一次"这种最难复现的状态；现在最坏是"码白烧了，重新申请"。

    错误尝试也要**落库计数**（register_attempt），不能只在内存里数：进程重启就归零的话，
    攻击者只要每 5 次重启一次服务就能无限试。
    """
    if user is None:
        user = await user_crud.get_user_by_account(account, db)
    if user is None:
        logger.info("忘记密码：校验时账号未命中 account=%s", masked_account(account))
        raise ParamError(INVALID_CODE_MSG)

    record: PasswordResetCode | None = await reset_crud.latest_active_for_user(user.id, db)
    if record is None or not reset_crud.is_usable(record):
        logger.info("忘记密码：没有可用的验证码（没申请过/已用掉/已过期/次数用完）user=%s", user.username)
        raise ParamError(INVALID_CODE_MSG)

    if not reset_crud.check_code(str(code or "").strip(), record.code_hash):
        usable = await reset_crud.register_attempt(record, db, settings.RESET_CODE_MAX_ATTEMPTS)
        logger.warning(
            "忘记密码：验证码不匹配 user=%s 已尝试=%s 状态=%s",
            user.username, record.attempts, "仍可用" if usable else "已作废（次数用完）",
        )
        raise ParamError(INVALID_CODE_MSG)      # 响应里刻意不说还剩几次（见模块注释第 2 条）

    if user.deleted_at is not None or not user.is_active:
        await reset_crud.mark_used(record, db)
        logger.info("忘记密码：码有效但账号已不可用 user_id=%s", record.user_id)
        raise ParamError(INVALID_CODE_MSG)

    await reset_crud.mark_used(record, db)

    if verify_password(new_password, user.hashed_password):
        # 到了这一步，调用方手里已经有一条**有效**的验证码，给明确提示不再算泄露
        raise ParamError("新密码不能和当前密码相同，请换一个")

    await user_crud.set_password(user, get_password_hash(new_password), db)
    others = await reset_crud.invalidate_user_codes(user.id, db)
    revoked = await token_crud.revoke_all_user_tokens(user.id, db)
    await user_cache_invalidate(user.id)
    _cleanup_file_channel(user.id)
    logger.info("密码已重置 user=%s 吊销登录token=%d 作废其它验证码=%d", user.username, revoked, others)
    return user, revoked


async def user_cache_invalidate(user_id) -> int:
    """单独包一层：让 verify_and_reset 里的失效动作看得出顺序，测试也能精准打掉这一个调用"""
    from app.services import user_cache

    return await user_cache.invalidate(user_id)


def _cleanup_file_channel(user_id) -> None:
    """file 车道用完即删：那是一份**明文可改密码**的文件，留着没有意义"""
    if settings.RESET_DELIVERY != "file":
        return
    try:
        _file_target(user_id).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("重置文件清理失败（请手工清 %s）: %s", settings.RESET_FILE_DIR, exc)


__all__ = [
    "INVALID_CODE_MSG",
    "code_ttl",
    "deliver",
    "masked_account",
    "send_code",
    "verify_and_reset",
]
