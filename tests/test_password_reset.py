"""「忘记密码」这条车道的接口与编排测试（app/services/password_reset.py，验证码版）

四条最该守住的性质，用例都围绕它们：
  1. 账号存在与否 / 码错 / 码过期 —— **响应逐字相同**（防枚举，匿名接口唯一的安全属性）；
  2. 库里只有 bcrypt 哈希（6 位数字用 sha256 等于明文）、一次性、过期即废；
  3. **尝试次数上限真的生效**：到顶之后连正确的码也不能用（防在线爆破 10^6）；
  4. 改密成功必须吊销全部 access token（旧设备立刻 401）+ 作废该账号其它未用的码。
"""

import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.db.crud import reset_crud
from app.db.crud.reset_crud import utc_now
from app.db.models import PasswordResetCode, User
from app.services import password_reset

ENVELOPE_KEYS = {"status", "msg", "data"}
NEW_PASSWORD = "brand-new-pass-2026"


def assert_envelope(resp, status: int):
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body.keys()) == ENVELOPE_KEYS, body
    assert body["status"] == status, body
    assert isinstance(body["msg"], str) and body["msg"]
    return body


@pytest.fixture(autouse=True)
def file_channel(tmp_path, monkeypatch):
    """默认走 file 通道：测试不需要真发信，也不用读日志，验证码直接从临时文件里捞

    选 file 而不是 console 是有原因的：console 车道要求"读日志"，
    而测试进程里 root handler 归 pytest 管，验证码一旦被别的用例吃掉就会随机失败。
    """
    monkeypatch.setattr(settings, "RESET_DELIVERY", "file")
    monkeypatch.setattr(settings, "RESET_FILE_DIR", str(tmp_path / "reset"))
    monkeypatch.setattr(settings, "RESET_CODE_LENGTH", 6)
    monkeypatch.setattr(settings, "RESET_CODE_TTL_MINUTES", 10)
    monkeypatch.setattr(settings, "RESET_CODE_MAX_ATTEMPTS", 5)
    monkeypatch.setattr(settings, "RESET_REQUEST_COOLDOWN_SECONDS", 600)


def ask(client, account):
    return client.post("/api/v1/user/forgot_password", json={"account": account})


def verify(client, account, code, new_password=NEW_PASSWORD, **extra):
    return client.post(
        "/api/v1/user/reset_password/verify",
        json={"account": account, "code": code, "new_password": new_password, **extra},
    )


def extract_code(text: str) -> str:
    match = re.search(r"验证码:\s*(\d{4,8})", text)
    assert match, text
    return match.group(1)


def issued_code(directory: Path) -> str:
    """从 file 车道落地的文件里把验证码抠出来（顺带证明明文确实只进通道不进响应）"""
    files = sorted(Path(directory).glob("*.txt"))
    assert files, "file 车道没有落下任何验证码文件"
    return extract_code(files[0].read_text(encoding="utf-8"))


async def rows(session):
    result = await session.execute(select(PasswordResetCode))
    return list(result.scalars().all())


# ---------------- 申请 ----------------
def test_forgot_password_known_account_issues_code(client, register):
    register()
    body = assert_envelope(ask(client, "zhangsan"), 200)
    assert "验证码" in body["msg"]
    assert body["data"] is None
    code = issued_code(Path(settings.RESET_FILE_DIR))
    assert len(code) == 6 and code.isdigit()


def test_unknown_account_response_is_identical(client, register):
    """防枚举的核心断言：两个响应的 msg 必须逐字相同（不是"都算成功"这么宽松）"""
    register()
    known = assert_envelope(ask(client, "zhangsan"), 200)
    body = assert_envelope(ask(client, "nobody-here@example.com"), 200)
    assert body["msg"] == known["msg"]


def test_forgot_password_never_returns_code(client, register):
    """响应体里绝不能出现验证码本身：出现就等于"POST 一个账号即可改密码" """
    register()
    resp = ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))
    assert code not in resp.text
    assert resp.headers.get("X-Request-ID")  # 响应头有 trace 用的 id（前端报障贴这一个就够）


def test_disabled_account_gets_same_msg_and_no_code(client, register, run, monkeypatch):
    register()
    monkeypatch.setattr(settings, "RESET_REQUEST_COOLDOWN_SECONDS", 0)
    ask(client, "zhangsan")  # 先落一份，稍后确认"禁用之后不会再落新的"
    for path in Path(settings.RESET_FILE_DIR).glob("*.txt"):
        path.unlink()

    async def disable(session):
        user = (await session.execute(select(User).where(User.username == "zhangsan"))).scalar_one()
        user.is_active = False
        await session.commit()

    run(disable)
    body = assert_envelope(ask(client, "zhangsan"), 200)
    assert "验证码已发出" in body["msg"]
    assert not list(Path(settings.RESET_FILE_DIR).glob("*.txt"))


def test_cooldown_blocks_second_request(client, register):
    register()
    assert ask(client, "zhangsan").status_code == 200
    body = assert_envelope(ask(client, "zhangsan"), 429)
    assert "秒后再试" in body["msg"]


def test_cooldown_check_is_db_based_even_without_redis(client, register, run):
    """限流的兜底必须在数据库里：REDIS_ENABLED=False 时同账号冷却依然生效

    这条用例存在的原因：Redis 是可选加速器（本机甚至没装），
    如果冷却只靠 Redis 计数器，没 Redis 就等于**任何人都能无限申请**。
    """
    register()
    assert settings.REDIS_ENABLED is False
    assert ask(client, "zhangsan").status_code == 200

    async def count(session):
        return len(await rows(session))

    assert run(count) == 1
    assert ask(client, "zhangsan").status_code == 429

    async def latest(session):
        return (await session.execute(select(PasswordResetCode))).scalars().first()

    record = run(latest)
    assert record is not None and reset_crud.is_usable(record)


def test_code_is_stored_as_bcrypt_not_sha256(client, register, run):
    """短码存 sha256 等于没存（10^6 全枚举只要几十毫秒），所以必须是带盐慢哈希"""
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))

    async def fetch(session):
        return await rows(session)

    stored = run(fetch)
    assert len(stored) == 1
    assert stored[0].code_hash != code              # 原文绝不入库
    assert stored[0].code_hash.startswith("$2")     # bcrypt 前缀
    assert len(stored[0].code_hash) == 60
    assert code not in stored[0].code_hash


def test_two_requests_hash_the_same_code_differently(client, register, run):
    """同一个码两次哈希结果不同 = 真的带盐（拿库的人没法靠比对哈希串反推）"""
    assert reset_crud.hash_code("123456") != reset_crud.hash_code("123456")
    assert reset_crud.check_code("123456", reset_crud.hash_code("123456")) is True
    assert reset_crud.check_code("654321", reset_crud.hash_code("123456")) is False


# ---------------- 校验（设新密码）----------------
def test_verify_resets_password_and_revokes_tokens(client, login, user_headers):
    # user_headers 已经注册 + 登录过 zhangsan，这里不能再 register()（会撞 409，噪声还看不出问题）
    headers = user_headers
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))

    body = assert_envelope(verify(client, "zhangsan", code), 200)
    assert "已下线" in body["msg"]

    # 旧 JWT 必须当场失效（tokens 表里没有它了）
    assert client.get("/api/v1/user/info", headers=headers).status_code == 401
    # 旧密码登不进去，新密码能
    assert login("zhangsan").status_code == 401
    assert login("zhangsan", NEW_PASSWORD).status_code == 200


def test_verify_needs_no_authorization(client, register):
    """匿名接口：不带头也要能改（用户正因为没登录态才用它）"""
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))
    assert verify(client, "zhangsan", code).status_code == 200


def test_verify_rejects_replay(client, register):
    """一次性：用过的码再来一次必须失败，而且文案与"码错"完全相同"""
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))
    assert verify(client, "zhangsan", code).status_code == 200
    body = assert_envelope(verify(client, "zhangsan", code, new_password="another-pass-99"), 400)
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_unknown_account_and_wrong_code_share_one_message(client, register):
    """三种失败（没这个账号 / 码错 / 没申请过）必须是同一句话，否则就是枚举探针"""
    register()
    ask(client, "zhangsan")
    wrong = assert_envelope(verify(client, "zhangsan", "000000"), 400)
    nobody = assert_envelope(verify(client, "nobody-here", "123456"), 400)
    no_request = assert_envelope(verify(client, "lisi-absent", "123456"), 400)
    assert wrong["msg"] == nobody["msg"] == no_request["msg"] == password_reset.INVALID_CODE_MSG


@pytest.mark.parametrize("bad_code", ["0000", "000000", "1234567", "999999"])
def test_verify_rejects_codes_that_were_never_issued(client, register, bad_code):
    """形状合法但没发过的码：走完 bcrypt 比对再拒，不能因为"查不到匹配"就给出不同提示"""
    register()
    ask(client, "zhangsan")
    body = assert_envelope(verify(client, "zhangsan", bad_code), 400)
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_attempts_limit_burns_the_code_even_if_it_then_matches(client, register, monkeypatch):
    """在线爆破的闸门：试错到上限之后，**连正确的码也不能用**

    没有这条，6 位数字（100 万种）就是可以慢慢试的；库里存得再好也没用。
    """
    monkeypatch.setattr(settings, "RESET_CODE_MAX_ATTEMPTS", 3)
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))

    for _ in range(3):
        assert verify(client, "zhangsan", "000000").status_code == 400

    body = assert_envelope(verify(client, "zhangsan", code), 400)   # 码是对的，但已经被烧掉
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_attempt_counter_is_persisted_not_in_memory(client, register, run):
    """计数必须落库：只在内存里数，进程一重启就归零，等于没限"""
    register()
    ask(client, "zhangsan")
    verify(client, "zhangsan", "000000")

    async def fetch(session):
        return await rows(session)

    stored = run(fetch)
    assert len(stored) == 1 and stored[0].attempts == 1


def test_expired_code_rejected(client, register, run):
    """过期即废（用直接改库的方式造过期，比把 TTL 配成负数更诚实）"""
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))

    async def expire(session):
        record = (await session.execute(select(PasswordResetCode))).scalars().first()
        record.expires_at = utc_now() - timedelta(minutes=1)
        await session.commit()

    run(expire)
    body = assert_envelope(verify(client, "zhangsan", code), 400)
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_old_code_is_invalid_after_rerequest(client, register, run, monkeypatch):
    """重新申请只让**最新那条**有效：旧码留在库里等于把有效期拉长到最早那次申请"""
    monkeypatch.setattr(settings, "RESET_REQUEST_COOLDOWN_SECONDS", 0)
    register()
    ask(client, "zhangsan")
    first = issued_code(Path(settings.RESET_FILE_DIR))

    async def count(session):
        return len(await rows(session))

    assert run(count) == 1
    ask(client, "zhangsan")
    assert run(count) == 2                       # 两条都在（旧的被标 used_at，不删行，留排障线索）

    body = assert_envelope(verify(client, "zhangsan", first), 400)
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_other_codes_burned_after_success(client, register, run, monkeypatch):
    """改密成功后该账号其它未用的码一律作废（用户可能手里攥着好几条）"""
    monkeypatch.setattr(settings, "RESET_REQUEST_COOLDOWN_SECONDS", 0)
    register()
    ask(client, "zhangsan")
    first = issued_code(Path(settings.RESET_FILE_DIR))
    ask(client, "zhangsan")
    second = issued_code(Path(settings.RESET_FILE_DIR))
    assert first != second

    assert verify(client, "zhangsan", second).status_code == 200

    async def usable(session):
        return [r for r in await rows(session) if reset_crud.is_usable(r)]

    assert run(usable) == []


def test_same_as_current_password_rejected(client, register):
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))
    body = assert_envelope(verify(client, "zhangsan", code, new_password="abc123456"), 400)
    assert "不能和当前密码相同" in body["msg"]
    # 这一步已经把码烧掉（宁可让用户重新申请，也不能留有效的码）
    assert verify(client, "zhangsan", code).status_code == 400


def test_wrong_password_still_logs_in(client, register):
    """失败的校验绝不能碰密码：密码只在码对之后才被改"""
    register()
    ask(client, "zhangsan")
    assert verify(client, "zhangsan", "000000").status_code == 400
    assert client.post("/api/v1/user/login", json={"account": "zhangsan", "password": "abc123456"}).status_code == 200


# ---------------- 参数校验（形状层，不碰业务）----------------
def test_short_password_rejected_by_schema(client, register):
    register()
    ask(client, "zhangsan")
    assert_envelope(verify(client, "zhangsan", "123456", new_password="abc"), 422)


@pytest.mark.parametrize("bad", ["12ab56", "abcdef", "1 2 3 4 5 6"])
def test_non_digit_code_rejected_by_schema(client, register, bad):
    """非数字直接 422，连库都不进：省一次 bcrypt，也不给"这是不是有效账号"的侧信道"""
    register()
    ask(client, "zhangsan")
    assert verify(client, "zhangsan", bad).status_code == 422


def test_verify_requires_account(client, register):
    """短码不自带身份（长令牌才自带），所以 account 是必填 —— 缺了就是 422"""
    register()
    ask(client, "zhangsan")
    resp = client.post(
        "/api/v1/user/reset_password/verify", json={"code": "123456", "new_password": NEW_PASSWORD}
    )
    assert resp.status_code == 422


def test_malformed_json_does_not_leak(client, register):
    """body 不是合法 JSON 时不能崩 500，也不能给出"账号存在吗"的线索"""
    register()
    resp = client.post(
        "/api/v1/user/forgot_password",
        content=b"{not json",
        headers={"Content-Type": "application/json", **{"Authorization": "x"}},
    )
    assert resp.status_code in (200, 400, 422), resp.text


# ---------------- 通道与清理 ----------------
def test_reset_file_cleaned_up_after_success(client, register):
    register()
    directory = Path(settings.RESET_FILE_DIR)
    ask(client, "zhangsan")
    code = issued_code(directory)
    target = Path(sorted(directory.glob("*.txt"))[0])
    assert target.exists()
    verify(client, "zhangsan", code)
    assert not target.exists()  # 明文可改密码的文件不该留在盘上过夜


def test_deleted_user_code_rejected(client, register, run):
    """账号没了，之前申请的码必须失效（码不该比账号活得久）"""
    register()
    ask(client, "zhangsan")
    code = issued_code(Path(settings.RESET_FILE_DIR))

    async def wipe(session):
        user = (await session.execute(select(User).where(User.username == "zhangsan"))).scalar_one()
        user.deleted_at = utc_now()
        await session.commit()

    run(wipe)
    body = assert_envelope(verify(client, "zhangsan", code), 400)
    assert body["msg"] == password_reset.INVALID_CODE_MSG


def test_console_channel_logs_the_code(client, register, monkeypatch, caplog):
    """console 车道就是"模拟发送"：本机验收靠从日志里抄码，所以码必须真打进日志"""
    register()
    monkeypatch.setattr(settings, "RESET_DELIVERY", "console")
    with caplog.at_level(logging.INFO, logger="app.services.password_reset"):
        assert ask(client, "zhangsan").status_code == 200
    assert "验证码=" in caplog.text
    assert "console" in caplog.text          # 日志里要能看出是哪条通道送出去的
    assert "zhangsan" in caplog.text


# ---------------- 服务层单测（不走 HTTP）----------------
def test_masked_account_hides_local_part():
    assert password_reset.masked_account("zhangsan@example.com") == "z***@example.com"
    assert "zhangsan" not in password_reset.masked_account("zhangsan")


def test_new_raw_code_shape_and_randomness():
    codes = {reset_crud.new_raw_code(6) for _ in range(40)}
    assert all(len(c) == 6 and c.isdigit() for c in codes)
    assert len(codes) > 1          # 不是写死的常量


def test_code_ttl_never_zero_even_with_bad_config(monkeypatch):
    """TTL 配成 0/负数也不能签发"一签发就过期"的码：兜底成 1 分钟，并在 check 里点名"""
    monkeypatch.setattr(settings, "RESET_CODE_TTL_MINUTES", -5)
    assert password_reset.code_ttl() == timedelta(minutes=1)
    assert "RESET_CODE_TTL_MINUTES" in settings.reset_lane_block_reason


def test_block_reason_catches_unsafe_attempts(monkeypatch):
    monkeypatch.setattr(settings, "RESET_CODE_MAX_ATTEMPTS", 0)
    assert "尝试次数" in settings.reset_lane_block_reason


def test_block_reason_catches_short_code(monkeypatch):
    monkeypatch.setattr(settings, "RESET_CODE_LENGTH", 3)
    assert "RESET_CODE_LENGTH" in settings.reset_lane_block_reason


def test_smtp_lane_without_config_is_reported_not_crashing(client, register, monkeypatch):
    """smtp 没配齐时：送达失败 → 码作废 → 对用户仍是同一句话（不能把配置问题甩给用户）"""
    monkeypatch.setattr(settings, "RESET_DELIVERY", "smtp")
    monkeypatch.setattr(settings, "SMTP_HOST", "")
    register()
    body = assert_envelope(ask(client, "zhangsan"), 200)
    assert "验证码已发出" in body["msg"]
    assert not list(Path(settings.RESET_FILE_DIR).glob("*.txt"))


def test_service_layer_works_without_http(client, register, run):
    """服务层能被脚本/单测直接调（user 传 None 就自己查表），不依赖路由的依赖注入"""
    register()

    async def _send(session):
        return await password_reset.send_code("zhangsan", session)

    msg = run(_send)
    assert "验证码已发出" in msg
    code = issued_code(Path(settings.RESET_FILE_DIR))

    async def _reset(session):
        return await password_reset.verify_and_reset("zhangsan", code, NEW_PASSWORD, session)

    user, revoked = run(_reset)
    assert user.username == "zhangsan"
    assert revoked == 0     # 这个账号没登录过，没有 access token 可吊销
