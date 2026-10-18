"""API 测试目录的共享夹具。"""

from __future__ import annotations

import pytest

from tests.helpers.auth import isolated_settings


@pytest.fixture()
def settings(tmp_path):
    """隔离的 `Settings`（显式传值，不依赖本地 `.env`）。

    `test_balance.py` / `test_admin_model_catalog.py` 曾各写一份逐字节等价的实现。
    """
    return isolated_settings(tmp_path)
