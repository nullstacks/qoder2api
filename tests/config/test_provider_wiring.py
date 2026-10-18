"""Qoder / CodeArts 接线：Settings 字段、端点白名单与热更最小间隔。"""

from __future__ import annotations

import pytest

from src.config import validate_codearts_endpoint_allowed, validate_qoder_endpoint_allowed
from src.runtime_settings import HOT_BY_KEY, RuntimeSettings
from tests.helpers.auth import settings_with as _settings
from tests.helpers.stores import MemoryStore as _MemoryStore

# --------------------------------------------------------------- Settings 字段

def test_qoder_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.qoder_api_endpoint == "https://openapi.qoder.com.cn"
    assert settings.qoder_gateway_endpoint == "https://gateway.qoder.com.cn"
    # 白名单去空白并去掉结尾斜杠；国内版两个域名都在
    assert settings.qoder_allowed == (
        "https://openapi.qoder.com.cn",
        "https://gateway.qoder.com.cn",
        "https://openapi.qoder.sh",
        "https://api1.qoder.sh",
    )

def test_codearts_defaults_and_allowed_parsing():
    settings = _settings()
    assert settings.codearts_api_endpoint == (
        "https://snap-access.cn-north-4.myhuaweicloud.com")
    assert settings.codearts_allowed == (
        "https://snap-access.cn-north-4.myhuaweicloud.com",
        "https://sts.cn-north-4.myhuaweicloud.com",
        "https://opengw.developer.huaweicloud.com",
        "https://codearts.huaweicloud.com",
    )

def test_allowed_parsing_ignores_blank_entries():
    settings = _settings(qoder_allowed_endpoints=" https://a.example.com/ , , ")
    assert settings.qoder_allowed == ("https://a.example.com",)
    assert _settings(codearts_allowed_endpoints="").codearts_allowed == ()

# --------------------------------------------------------------- 端点白名单

def test_qoder_endpoint_whitelist():
    settings = _settings()
    assert validate_qoder_endpoint_allowed("https://openapi.qoder.com.cn", settings)
    # 尾斜杠归一后同等通过
    assert validate_qoder_endpoint_allowed("https://openapi.qoder.com.cn/", settings)
    assert validate_qoder_endpoint_allowed("https://api1.qoder.sh", settings)
    assert not validate_qoder_endpoint_allowed("https://evil.example.com", settings)

def test_codearts_endpoint_whitelist():
    settings = _settings()
    base = "https://snap-access.cn-north-4.myhuaweicloud.com"
    assert validate_codearts_endpoint_allowed(base, settings)
    assert validate_codearts_endpoint_allowed(base + "/", settings)
    assert validate_codearts_endpoint_allowed(
        "https://sts.cn-north-4.myhuaweicloud.com", settings)
    assert not validate_codearts_endpoint_allowed("https://evil.example.com", settings)

# --------------------------------------------------------------- 热更最小间隔

def test_qoder_codearts_min_intervals_are_hot():
    for key in ("qoder_chat_min_interval", "codearts_chat_min_interval"):
        assert key in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    # 默认 5s，独立于其它渠道
    assert runtime.qoder_chat_min_interval == 5
    assert runtime.codearts_chat_min_interval == 5
    runtime.set("qoder_chat_min_interval", 0)
    runtime.set("codearts_chat_min_interval", 2.5)
    assert runtime.qoder_chat_min_interval == 0
    assert runtime.codearts_chat_min_interval == 2.5

def test_codearts_max_concurrency_is_hot():
    """CodeArts 每账号并发上限可热更：默认 3（对齐上游硬限），0 = 关闭上限。"""
    assert "codearts_max_concurrency" in HOT_BY_KEY
    runtime = RuntimeSettings(_settings(), _MemoryStore())
    assert runtime.codearts_max_concurrency == 3
    runtime.set("codearts_max_concurrency", 0)
    assert runtime.codearts_max_concurrency == 0
    runtime.set("codearts_max_concurrency", 8)
    assert runtime.codearts_max_concurrency == 8

def test_codearts_max_concurrency_rejects_negative():
    from src.runtime_settings import InvalidSetting

    runtime = RuntimeSettings(_settings(), _MemoryStore())
    with pytest.raises(InvalidSetting):
        runtime.set("codearts_max_concurrency", -1)

def test_main_wires_codearts_concurrency_cap(admin_client):
    """装配层必须把上限接到 CodeArts pacer，且读数实时跟随热更覆盖。"""
    app, _client = admin_client
    pacer = app.state.services.registry["codearts"].pacer
    assert pacer.allow_concurrent is True
    assert pacer.max_concurrency == 3        # 对齐上游每账号硬限
    assert pacer.window_seconds == 60        # 窗口口径：每账号每 60s 最多 3 次启动
    app.state.services.settings.set("codearts_max_concurrency", 0)
    assert pacer.max_concurrency == 0        # 热更 0 = 关闭上限
    app.state.services.settings.set("codearts_request_window_seconds", 0)
    assert pacer.window_seconds == 0.0       # 热更 0 = 关闭窗口口径

def test_qoder_codearts_min_intervals_reject_negative():
    from src.runtime_settings import InvalidSetting

    runtime = RuntimeSettings(_settings(), _MemoryStore())
    with pytest.raises(InvalidSetting):
        runtime.set("qoder_chat_min_interval", -1)
    with pytest.raises(InvalidSetting):
        runtime.set("codearts_chat_min_interval", -1)

# ----------------------------------------------------- main.py 端点装配校验

def test_qoder_endpoint_resolvers_accept_defaults_and_reject_evil():
    from src.main import _qoder_gateway, _qoder_host

    settings = _settings()
    assert _qoder_host(settings) == "https://openapi.qoder.com.cn"
    assert _qoder_gateway(settings) == "https://gateway.qoder.com.cn"

    bad_host = _settings(qoder_api_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="QODER_ALLOWED_ENDPOINTS"):
        _qoder_host(bad_host)

    bad_gateway = _settings(qoder_gateway_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="QODER_ALLOWED_ENDPOINTS"):
        _qoder_gateway(bad_gateway)

def test_codearts_endpoint_resolver_accepts_default_and_rejects_evil():
    from src.main import _codearts_endpoint

    assert _codearts_endpoint(_settings()) == (
        "https://snap-access.cn-north-4.myhuaweicloud.com")
    bad = _settings(codearts_api_endpoint="https://evil.example.com")
    with pytest.raises(ValueError, match="CODEARTS_ALLOWED_ENDPOINTS"):
        _codearts_endpoint(bad)

# --------------------------------------------------------- poll 轨道装配

def test_upstream_auth_registers_qoder_and_codearts_tracks():
    """四渠道都能 start/poll：Qoder 与 CodeArts 各挂一个 poll 轨道对象。"""
    from src.main import _upstream_auth
    from src.provider.codearts import CodeArtsProvider
    from src.provider.codearts.client import CodeArtsClient
    from src.provider.codearts.oauth import CodeArtsOAuth
    from src.provider.qoder import QoderProvider
    from src.provider.qoder.auth import QoderOAuth
    from src.provider.qoder.client import QoderClient

    registry = {
        "qoder": QoderProvider(client=QoderClient(
            host="https://openapi.qoder.com.cn",
            gateway="https://gateway.qoder.com.cn")),
        "codearts": CodeArtsProvider(client=CodeArtsClient(
            endpoint="https://snap-access.cn-north-4.myhuaweicloud.com")),
    }
    flows = _upstream_auth(registry, _settings())
    assert isinstance(flows["qoder"], QoderOAuth)
    assert isinstance(flows["codearts"], CodeArtsOAuth)
    # 登录补身份复用 provider 的签名客户端：登录时 token 响应不带用户名，
    # 不补则凭证昵称为空、统计明细的凭证列空白
    assert flows["codearts"]._identity_client is registry["codearts"].client
    # 未注册的渠道不产生轨道（防御：registry 缺 key 时不应抛）
    assert _upstream_auth({}, _settings()) == {}
