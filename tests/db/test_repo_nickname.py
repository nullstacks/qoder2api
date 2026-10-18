"""`CredentialRepository.nickname_of`：实时流按需补凭证昵称（单条查询）。

与 `/api/stats/events` 同口径：昵称取自全局共享池，端点只在 admin/operator
时才调用；这里固定仓储层的边界（未设置 / 已删除 / 有昵称）。
"""

from __future__ import annotations


def test_nickname_of_returns_configured_nickname(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="trae",
                                    credential_data={"accessToken": "a"},
                                    nickname="alice@example.com")
    assert credentials.nickname_of(credential_id) == "alice@example.com"


def test_nickname_of_returns_none_when_unset(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="trae", credential_data={"accessToken": "a"})
    assert credentials.nickname_of(credential_id) is None


def test_nickname_of_returns_none_when_empty_string(repo):
    """空串与「无昵称」等价（前端回落到 ID 前缀，不显示空白）。"""
    credentials, _db = repo
    credential_id = credentials.add(provider="trae",
                                    credential_data={"accessToken": "a"}, nickname="")
    assert credentials.nickname_of(credential_id) is None


def test_nickname_of_returns_none_for_deleted_credential(repo):
    credentials, _db = repo
    credential_id = credentials.add(provider="trae",
                                    credential_data={"accessToken": "a"},
                                    nickname="bob@example.com")
    credentials.delete(credential_id)
    assert credentials.nickname_of(credential_id) is None
