"""Settings 默认值、必填项与端点白名单。"""

from __future__ import annotations

import pytest

from src.config import Settings, validate_endpoint_allowed
from src.engine.scheduler import EXPIRY_WINDOW_SECONDS, SECONDARY_EXPIRY_WINDOW_SECONDS
from tests.helpers.constants import SECRET


def test_settings_defaults_and_admin_set():
    s = Settings(_env_file=None, APP_SECRET=SECRET, ADMIN_USERNAMES="alice, bob ,")
    assert s.admin_set == frozenset({"alice", "bob"})
    assert s.default_model == "glm-5.2"
    assert s.quota_expiry_window_seconds == EXPIRY_WINDOW_SECONDS
    assert s.quota_expiry_secondary_window_seconds == SECONDARY_EXPIRY_WINDOW_SECONDS
    assert s.is_admin("alice") and not s.is_admin("carol")

def test_settings_requires_app_secret():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None)

def test_allowed_endpoints_whitelist():
    s = Settings(_env_file=None, APP_SECRET=SECRET)
    assert validate_endpoint_allowed("https://copilot.tencent.com", s)
    assert validate_endpoint_allowed("https://www.codebuddy.ai", s)
    assert not validate_endpoint_allowed("https://evil.example", s)

def test_db_path_joins_data_dir():
    s = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR="/tmp/cbd")
    assert s.db_path == "/tmp/cbd/coding2api.sqlite3"
