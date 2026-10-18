"""兼容出口（Anthropic / Responses）测试共享装配助手。

`_app` 的装配体（建 Settings → build_app → 注入 codebuddy 凭证）在两个文件里
各写一份逐字节等价的实现，收敛到这里；默认 provider 因文件而异，由各文件自己
补上（`provider or _Provider()`）。
"""

from __future__ import annotations

from src.config import Settings
from src.main import build_app
from tests.helpers.constants import SECRET

__all__ = ["build_compat_app"]


def build_compat_app(tmp_path, provider):
    """建带 admin 引导配置的 app，注入 `provider` 与一个 codebuddy 凭证。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    application = build_app(settings, providers={"codebuddy": provider})
    application.state.credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"})
    return application
