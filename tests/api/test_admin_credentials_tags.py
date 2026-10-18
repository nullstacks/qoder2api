"""凭证标签：解析/规范化纯函数 + 端点往返 + 老库升级。"""

from __future__ import annotations

import pytest

from src.tags import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    normalize_tags,
    parse_tags,
    split_tags,
    tags_value,
)


def _add(app, provider="codebuddy", **kwargs):
    return app.state.credentials.add(provider=provider,
                                     credential_data={"bearer_token": "t"}, **kwargs)


def _row(client, credential_id):
    rows = client.get("/api/credentials").json()["credentials"]
    return next(row for row in rows if row["id"] == credential_id)


# ---------------------------------------------------------------- 纯函数

def test_split_tags_handles_full_width_separators_and_whitespace():
    assert split_tags(None) == []
    assert split_tags("") == []
    assert split_tags(" 生产 , 备用 ") == ["生产", "备用"]
    # 中文输入法下的全角逗号/分号都当分隔符
    assert split_tags("生产，备用；测试") == ["生产", "备用", "测试"]


def test_parse_tags_accepts_list_and_string_and_dedupes_case_insensitively():
    assert parse_tags(None) == []
    assert parse_tags(["生产", "备用"]) == ["生产", "备用"]
    assert parse_tags("生产,备用") == ["生产", "备用"]
    # 去重大小写不敏感，但保留首次写法
    assert parse_tags(["Prod", "prod", "PROD"]) == ["Prod"]
    # 空段丢弃
    assert parse_tags(["", " ", "生产"]) == ["生产"]


def test_parse_tags_rejects_non_string_items_and_wrong_container():
    with pytest.raises(ValueError, match="list or a comma-separated string"):
        parse_tags(123)
    with pytest.raises(ValueError, match="items must be strings"):
        parse_tags([1])


def test_parse_tags_rejects_items_containing_a_separator():
    """列表项含分隔符会被读回时拆成两条（读写不等价），写入前拦住。"""
    with pytest.raises(ValueError, match="不能包含逗号或分号"):
        parse_tags(["生产,备用"])
    with pytest.raises(ValueError, match="不能包含逗号或分号"):
        parse_tags(["生产，备用"])   # 全角逗号同样会被 split_tags 拆开


def test_parse_tags_enforces_count_and_length_limits():
    with pytest.raises(ValueError, match=f"最多 {MAX_TAGS} 条"):
        parse_tags([f"t{i}" for i in range(MAX_TAGS + 1)])
    with pytest.raises(ValueError, match="标签过长"):
        parse_tags(["x" * (MAX_TAG_LENGTH + 1)])


def test_normalize_tags_joins_with_comma():
    assert normalize_tags(["生产", "备用"]) == "生产,备用"
    assert normalize_tags(None) == ""


def test_tags_value_filters_out_of_range_dirty_data():
    """读路径容错：手工改库塞进的超长/超量标签只丢弃，不抛错。"""
    assert tags_value(None) == []
    stored = ",".join(["x" * (MAX_TAG_LENGTH + 1), "生产", "prod", "PROD"])
    assert tags_value(stored) == ["生产", "prod"]
    assert tags_value(",".join(f"t{i}" for i in range(MAX_TAGS + 5))) == \
        [f"t{i}" for i in range(MAX_TAGS)]


# ---------------------------------------------------------------- 存取与端点

def test_add_persists_tags_and_list_returns_them(admin_client):
    app, client = admin_client
    credential_id = _add(app, tags=["生产", "备用"])
    assert _row(client, credential_id)["tags"] == ["生产", "备用"]
    # 不带标签的凭证回空列表（不是 null），前端不必判空
    other = _add(app, provider="trae")
    assert _row(client, other)["tags"] == []


def test_set_tags_round_trip_and_audit(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    response = client.put(f"/api/credentials/{credential_id}/tags",
                          json={"tags": ["生产", "prod", "生产"]})
    assert response.status_code == 200
    assert response.json()["tags"] == ["生产", "prod"]
    assert _row(client, credential_id)["tags"] == ["生产", "prod"]

    events = app.state.audit.query(action="credential.tags")
    assert len(events) == 1
    assert events[0]["target"] == credential_id
    assert events[0]["detail"] == "生产,prod"

    # 空列表 = 清空，审计写明「（清空）」
    cleared = client.put(f"/api/credentials/{credential_id}/tags", json={"tags": []})
    assert cleared.json()["tags"] == []
    assert _row(client, credential_id)["tags"] == []
    # 两条审计落在同一秒，默认排序的 tiebreak 是随机 id，不能用 [-1] 取「最后一条」
    details = [event["detail"] for event in app.state.audit.query(action="credential.tags")]
    assert "（清空）" in details


def test_set_tags_validation_and_missing_credential(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    assert client.put(f"/api/credentials/{credential_id}/tags",
                      json={"tags": [1]}).status_code == 400
    assert client.put(f"/api/credentials/{credential_id}/tags",
                      json={"tags": ["x" * (MAX_TAG_LENGTH + 1)]}).status_code == 400
    assert client.put("/api/credentials/ghost/tags",
                      json={"tags": ["生产"]}).status_code == 400


def test_import_credential_accepts_optional_tags(admin_client):
    app, client = admin_client
    created = client.post("/api/credentials", json={
        "provider": "codebuddy",
        "credential": {"bearer_token": "t"},
        "nickname": "带标签导入",
        "tags": "生产,备用",
    }).json()
    assert _row(client, created["id"])["tags"] == ["生产", "备用"]
    # 非法标签 → 400，不落库
    bad = client.post("/api/credentials", json={
        "provider": "codebuddy",
        "credential": {"bearer_token": "t"},
        "tags": 123,
    })
    assert bad.status_code == 400


def test_set_tags_requires_operator(tmp_path):
    """viewer 无权改标签（与其余凭证写操作同一门槛）。"""
    from fastapi.testclient import TestClient

    from src.auth.session import create_session_token
    from src.config import Settings
    from src.main import build_app
    from tests.helpers.constants import SECRET

    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        credential_id = _add(app)
        client.cookies.set("coding2api_session", create_session_token("viewer", SECRET))
        assert client.put(f"/api/credentials/{credential_id}/tags",
                          json={"tags": ["生产"]}).status_code == 401
