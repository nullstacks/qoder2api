"""RBAC 鉴权。"""

from __future__ import annotations

import pytest

from src.auth.rbac import ForbiddenError, Principal, require_admin


def test_require_admin():
    assert require_admin(Principal("root", True)).username == "root"
    with pytest.raises(ForbiddenError):
        require_admin(Principal("u", False))


def test_principal_reconciles_is_admin_and_role():
    """is_admin 与 role 互为补全，消除「双真相源」。"""
    legacy = Principal("u", True)                      # 只传 is_admin
    assert legacy.role == "admin" and legacy.is_admin is True
    from_role = Principal("u", role="admin")           # 只传 role
    assert from_role.is_admin is True
    operator = Principal("u", role="operator")
    assert operator.is_admin is False and operator.is_operator is True
    viewer = Principal("u", role="viewer")
    assert viewer.is_admin is False and viewer.is_operator is False
