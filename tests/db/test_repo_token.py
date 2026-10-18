"""凭证仓储：token 到期时间解析（JWT/显式）与持久化。"""

from __future__ import annotations

import json
import time

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository
from src.provider.codebuddy.client import CodeBuddyCredential
from tests.helpers.chat import b64url as _b64
from tests.helpers.chat import jwt_token as _jwt
from tests.helpers.constants import SECRET


def jwt_expiry_of(token: str) -> int:
    from src.provider.token_expiry import jwt_expiry

    return jwt_expiry(token)

def test_jwt_expiry_decodes_exp():
    """JWT payload 的 exp 是可用的到期来源；非 JWT / 坏结构一律 0。"""
    from src.provider.token_expiry import jwt_expiry

    assert jwt_expiry(_jwt(1794389367)) == 1794389367
    # 毫秒时间戳归一
    assert jwt_expiry(_jwt(1794389367000)) == 1794389367
    # 非 JWT、段数不足、payload 非 base64、payload 非对象、exp 非法
    assert jwt_expiry("not-a-jwt") == 0
    assert jwt_expiry("a.b") == 0
    assert jwt_expiry("h.!!!.s") == 0
    # payload 是合法 base64 但不是 JSON 对象（数组）→ 0
    assert jwt_expiry(f"h.{_b64([1, 2])}.s") == 0
    assert jwt_expiry(_jwt(0)) == 0
    assert jwt_expiry(_jwt(True)) == 0
    assert jwt_expiry(_jwt("soon")) == 0      # type: ignore[arg-type]
    assert jwt_expiry(12345) == 0             # type: ignore[arg-type]
    # 缺 payload 段（"a..c"）也要挡住，不能把空串当 JSON 解
    assert jwt_expiry("a..c") == 0

def test_jwt_times_decodes_iat_and_exp():
    """`(iat, exp)` 一起取：进度条要 iat 当满量程，缺它就只能退化（见 display）。"""
    from src.provider.token_expiry import jwt_times

    token = _jwt(1794389367, iat=1793189367)
    assert jwt_times(token) == (1793189367, 1794389367)
    # iat 缺失 / 非法 → 0（而 exp 照常给），两者互不拖累
    assert jwt_times(_jwt(1794389367)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat=0)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat=True)) == (0, 1794389367)
    assert jwt_times(_jwt(1794389367, iat="x")) == (0, 1794389367)  # type: ignore[arg-type]
    # 毫秒时间戳归一（两个 claim 各自归一）
    assert jwt_times(_jwt(1794389367000, iat=1793189367000)) == (1793189367, 1794389367)
    # 非 JWT / 坏结构 → (0, 0)
    assert jwt_times("not-a-jwt") == (0, 0)
    assert jwt_times("a..c") == (0, 0)
    assert jwt_times(f"h.{_b64([1, 2])}.s") == (0, 0)
    assert jwt_times(12345) == (0, 0)         # type: ignore[arg-type]

def test_credential_expiry_prefers_explicit_then_jwt():
    """显式 expires_at 优先；缺失才回落 JWT exp；两者都没有 → 0（未知）。"""
    from src.provider.token_expiry import credential_expiry

    token = _jwt(1794389367)
    assert credential_expiry({"expires_at": 100}) == 100
    assert credential_expiry({"expiresAt": 200}) == 200
    # `expiration`：华为云系（CodeArts STS 临时凭证）的字段名。漏掉它时该渠道
    # `token_expires_at` 恒为 0 → 管理台无到期预览、到期预警对它完全失效。
    assert credential_expiry({"expiration": 300}) == 300
    assert credential_expiry({"expiration": 1794389367000}) == 1794389367   # 毫秒
    assert credential_expiry({"expires_at": 100, "expiration": 300}) == 100  # 前者优先
    assert credential_expiry({"expiration": "soon"}) == 0                   # 非数字
    # 显式值为 0 / 非数字 → 视作缺失，回落 JWT
    assert credential_expiry({"expires_at": 0, "bearer_token": token}) == 1794389367
    assert credential_expiry({"expires_at": True, "accessToken": token}) == 1794389367
    assert credential_expiry({"access_token": token}) == 1794389367
    assert credential_expiry({"token": token}) == 1794389367
    # 无任何 token / 键值类型不对 → 0，绝不猜本地 TTL
    assert credential_expiry({}) == 0
    assert credential_expiry({"bearer_token": None}) == 0
    assert credential_expiry({"bearer_token": "plain-token"}) == 0
    assert credential_expiry([]) == 0         # type: ignore[arg-type]

def test_credential_token_times_borrows_iat_from_co_issued_refresh_token():
    """access token 不是 JWT 时，借**同时签发**的 refresh_token JWT 的 `iat`。

    CodeArts 的 access token 是 STS AK/SK（取不到 iat），但签发它的那次响应同时
    轮转出一张 refresh_token JWT，`iat` 就是临时凭证的签发时刻（实测
    `iat + 7200 == expiration`）。只借 iat：它自己的 `exp` 是「登录 +30 天」，
    拿来做 access token 的到期会把 2h 的临时凭证画成 30 天。
    """
    from src.provider.token_expiry import credential_token_times

    issued = 1_700_000_000
    refresh_token = _jwt(issued + 30 * 86400, iat=issued)
    # CodeArts 形态：到期来自 `expiration`，签发借 refresh_token 的 iat
    assert credential_token_times({"expiration": issued + 7200,
                                   "refresh_token": refresh_token}) == (issued, issued + 7200)
    # access token 本身是 JWT 时优先用它自己的 iat（借 iat 只在兜底时发生）
    assert credential_token_times({
        "expiration": issued + 7200, "refresh_token": refresh_token,
        "bearer_token": _jwt(issued + 7200, iat=issued - 5),
    }) == (issued - 5, issued + 7200)
    # refresh_token 不是 JWT / 没有 iat → 签发时间仍旧 0（绝不拿回填时刻冒充）
    assert credential_token_times({"expiration": 1_700_007_200,
                                   "refresh_token": "plain"}) == (0, 1_700_007_200)
    assert credential_token_times({"expiration": 1_700_007_200, "refresh_token": ""}) == (
        0, 1_700_007_200)

def test_credential_token_times_pairs_issued_with_expiry():
    """签发时间只来自 JWT `iat`：上游不单独回传，拿不到就是 0（绝不拿回填时刻冒充）。"""
    from src.provider.token_expiry import credential_token_times

    token = _jwt(1794389367, iat=1793189367)
    # 显式 expires_at 优先；iat 仍从 JWT 取（两者不同源，各取所长）
    assert credential_token_times(
        {"expires_at": 100, "bearer_token": token}) == (1793189367, 100)
    assert credential_token_times(
        {"expiresAt": 200, "accessToken": token}) == (1793189367, 200)
    # 无显式值 → 到期回落 JWT exp
    assert credential_token_times({"bearer_token": token}) == (1793189367, 1794389367)
    # 无 iat 的 JWT：到期照给，签发时间未知 → 0
    assert credential_token_times({"bearer_token": _jwt(42)}) == (0, 42)
    # 非 JWT 的明文 token / 没有 token 字段 → 全 0（不猜）
    assert credential_token_times({"bearer_token": "plain"}) == (0, 0)
    assert credential_token_times({"expires_at": 7}) == (0, 7)
    assert credential_token_times({"bearer_token": None}) == (0, 0)
    assert credential_token_times([]) == (0, 0)   # type: ignore[arg-type]
    # 高优先键是明文、低优先键才是 JWT → 继续找，别丢签发时间
    assert credential_token_times(
        {"bearer_token": "plain", "token": token}) == (1793189367, 1794389367)
    # 高优先键是坏 JWT 也一样跳过
    assert credential_token_times(
        {"bearer_token": _jwt(0), "access_token": token}) == (1793189367, 1794389367)

def test_codebuddy_token_expiry_falls_back_to_jwt():
    """实测 CB 的 token 响应不带到期字段：必须回落到 JWT exp，否则预刷新永不触发。"""
    token = _jwt(int(time.time()) + 50 * 86400)
    credential = CodeBuddyCredential.from_dict(
        {"bearer_token": token, "refresh_token": "r", "auth_source": "oauth"})
    assert credential.expires_at == 0                       # JSON 里确实没有显式值
    assert credential.token_expires_at() == jwt_expiry_of(token)
    # 预刷新：进入 24h 窗口才 True，早于此 False（修复前恒 False）
    assert credential.needs_refresh(24 * 3600) is False
    assert credential.needs_refresh(50 * 86400) is True
    # 显式 expires_at 仍然优先于 JWT
    explicit = CodeBuddyCredential.from_dict(
        {"bearer_token": token, "refresh_token": "r", "expires_at": 12345})
    assert explicit.token_expires_at() == 12345

def test_codebuddy_token_expiry_unknown_is_not_expired():
    """拿不到到期时间 → 过期未知，不刷新（none 分支），绝不猜本地 TTL。"""
    credential = CodeBuddyCredential.from_dict(
        {"bearer_token": "plain", "refresh_token": "r", "auth_source": "oauth"})
    assert credential.token_expires_at() == 0
    assert credential.needs_refresh(0) is False
    # bearer-only 手动凭证永不刷新（AGENTS.md），即便 token 里带 exp
    manual = CodeBuddyCredential.from_dict({"bearer_token": _jwt(1), "refresh_token": "r"})
    assert manual.needs_refresh(999_999_999) is False

def test_trae_token_expiry_summary_and_jwt_fallback():
    """TRAE 侧同一口径：`expires_at` 只存显式值，缺失时由派生方法回落 JWT exp。"""
    from src.provider.trae.credential import TraeCredential

    token = _jwt(int(time.time()) + 10 * 86400)
    plain = TraeCredential.from_dict({"accessToken": token, "expiresAt": 0})
    # 显式字段缺失/为 0 → 仍存 0（JWT 派生值不进字段，避免写回后变「权威」）
    assert plain.expires_at == 0
    assert plain.token_expires_at() > time.time() + 9 * 86400
    explicit = TraeCredential.from_dict({"accessToken": token, "expiresAt": 12345})
    assert explicit.expires_at == 12345
    # 刷新写回时 to_dict() 只带显式值，JWT 派生值不会残留在 JSON 里
    assert plain.to_dict()["expiresAt"] == 0
    # 完全拿不到到期时间：TRAE 保守刷新（既有契约）
    unknown = TraeCredential.from_dict({"accessToken": "plain-token"})
    assert unknown.expires_at == 0
    assert unknown.token_expires_at() == 0
    assert unknown.needs_refresh(3600) is True

def test_repo_persists_token_times_on_add_and_save(tmp_path):
    """add / save_credential_data 都写回 token_expires_at 与 token_issued_at。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    token = _jwt(now + 30 * 86400, iat=now)
    cid = credentials.add(provider="codebuddy",
                          credential_data={"bearer_token": token}, now=now)
    stored = db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (cid,)).fetchone()
    assert tuple(stored) == (now + 30 * 86400, now)
    # 刷新写回：两个时间一并推进（旧值被替换，不残留成「权威」到期）
    rotated = _jwt(now + 60 * 86400 + 100, iat=now + 100)
    credentials.save_credential_data(cid, {"bearer_token": rotated})
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (cid,)).fetchone()) == (now + 60 * 86400 + 100, now + 100)
    # 上游没给任何到期信息 → 写 0（未知），不得猜本地 TTL
    unknown = credentials.add(provider="codebuddy",
                              credential_data={"bearer_token": "plain"}, now=now)
    assert tuple(db.connect().execute(
        "SELECT token_expires_at, token_issued_at FROM credentials WHERE id = ?",
        (unknown,)).fetchone()) == (0, 0)
    db.close()

def test_list_all_derives_token_times_from_legacy_rows(tmp_path):
    """老库升级后两列为 NULL：列表读到时按需从密文派生。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    token = _jwt(now + 5 * 86400, iat=now)
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": token},
                          now=now)
    # 模拟升级前写入的行：两列都清空（迁移只加列，不回填）
    db.connect().execute(
        "UPDATE credentials SET token_expires_at = NULL, token_issued_at = NULL WHERE id = ?",
        (cid,))
    db.connect().commit()
    row = credentials.list_all(now=now)[0]
    assert row["token_expires_at"] == now + 5 * 86400
    assert row["token_issued_at"] == now
    # data_enc 绝不进响应
    assert "data_enc" not in row
    db.close()

def test_list_all_token_expiry_unknown_stays_zero(tmp_path):
    """到期时间未知写 0：只下发绝对值，剩余时间与预警由前端同一时钟现算。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    known = credentials.add(provider="codebuddy",
                            credential_data={"bearer_token": _jwt(now + 600)}, now=now)
    unknown = credentials.add(provider="codebuddy",
                              credential_data={"bearer_token": "plain"}, now=now)
    rows = {row["id"]: row for row in credentials.list_all(now=now)}
    assert rows[known]["token_expires_at"] == now + 600
    # 未知 = 0，展示层据此隐藏（绝不当成已过期）
    assert rows[unknown]["token_expires_at"] == 0
    assert rows[unknown]["token_issued_at"] == 0
    # 列表只给绝对值，不替前端算剩余时间（服务端算好的不会随 tick 更新）
    assert "token_remaining_seconds" not in rows[known]
    assert "token_expiring" not in rows[known]
    db.close()

def test_token_times_from_blob_tolerates_bad_ciphertext(tmp_path):
    """密文损坏 / 空值不得让列表整体崩掉（返回 (0, 0) = 未知）。"""
    from src.db.crypto import CredentialCipher
    from src.db.repo import _token_times_from_blob

    cipher = CredentialCipher(SECRET)
    assert _token_times_from_blob(None, cipher) == (0, 0)
    assert _token_times_from_blob(b"", cipher) == (0, 0)
    assert _token_times_from_blob(b"not-a-fernet-token", cipher) == (0, 0)
    # 解密成功但内容不是 JSON 对象
    assert _token_times_from_blob(cipher.encrypt(b"[1, 2]"), cipher) == (0, 0)
    assert _token_times_from_blob(cipher.encrypt(b"not json"), cipher) == (0, 0)
    # 正常载荷：两列一起给出
    payload = json.dumps({"bearer_token": _jwt(200, iat=100)}).encode("utf-8")
    assert _token_times_from_blob(cipher.encrypt(payload), cipher) == (100, 200)
