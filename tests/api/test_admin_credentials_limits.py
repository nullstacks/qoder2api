"""凭证级限额护栏端点：PUT /api/credentials/{id}/limits。"""

from __future__ import annotations


def _add(app, provider="codebuddy"):
    return app.state.credentials.add(provider=provider,
                                     credential_data={"bearer_token": "t"})


def _row(client, credential_id):
    rows = client.get("/api/credentials").json()["credentials"]
    return next(row for row in rows if row["id"] == credential_id)


def test_list_exposes_limits_field(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    assert _row(client, credential_id)["limits"] is None      # 缺行 = 不受限

    client.put(f"/api/credentials/{credential_id}/limits",
               json={"daily_tokens": 1000})
    assert _row(client, credential_id)["limits"]["daily_tokens"] == 1000


def test_set_limits_round_trip_and_audit(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    response = client.put(
        f"/api/credentials/{credential_id}/limits",
        json={"keep_credits": 50, "daily_tokens": 1000, "daily_credits": 3.5,
              "per_model_tokens": {"glm-5": 100, "kimi": 0, "": 5}})
    assert response.status_code == 200
    limits = response.json()["limits"]
    assert limits["keep_credits"] == 50.0
    assert limits["daily_tokens"] == 1000
    assert limits["per_model_tokens"] == {"glm-5": 100}   # 空名/0 项丢弃
    assert limits["updated_by"] == "root"

    # 管理台改完立即生效（快照失效）
    assert app.state.services.limit_guard.blocked(
        app.state.credentials.candidates(["codebuddy"])[0], "glm-5") is False

    # 审计落一条
    events = app.state.audit.query(action="credential.limits")
    assert len(events) == 1
    assert events[0]["target"] == credential_id
    assert "每日 token 1000" in events[0]["detail"]


def test_clear_limits_with_empty_payload(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    client.put(f"/api/credentials/{credential_id}/limits",
               json={"daily_tokens": 100})
    cleared = client.put(f"/api/credentials/{credential_id}/limits", json={})
    assert cleared.json()["limits"] is None
    assert app.state.services.limits.get(credential_id) is None


def test_set_limits_validation_and_missing_credential(admin_client):
    app, client = admin_client
    credential_id = _add(app)
    # 非数值 / 非对象 / 非有限值 → 400
    assert client.put(f"/api/credentials/{credential_id}/limits",
                      json={"daily_tokens": "many"}).status_code == 400
    assert client.put(f"/api/credentials/{credential_id}/limits",
                      json={"keep_credits": True}).status_code == 400
    assert client.put(f"/api/credentials/{credential_id}/limits",
                      json={"per_model_tokens": [1]}).status_code == 400
    assert client.put(f"/api/credentials/{credential_id}/limits",
                      json={"per_model_tokens": {"m": "x"}}).status_code == 400
    # 凭证不存在 → 400
    assert client.put("/api/credentials/ghost/limits",
                      json={"daily_tokens": 1}).status_code == 400


def test_optional_limit_rejects_non_finite():
    """非有限值（NaN/Inf）被拒：它们会让 `> 0` 判定与聚合失去意义。"""
    import math

    import pytest

    from src.api.admin_credentials import _optional_limit
    from src.compat.openai.request import InvalidRequest

    with pytest.raises(InvalidRequest):
        _optional_limit({"x": math.inf}, "x")
    with pytest.raises(InvalidRequest):
        _optional_limit({"x": math.nan}, "x", integer=True)


def test_set_limits_requires_operator(tmp_path):
    """viewer 无权改限额（与其余凭证写操作同一门槛）。"""
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
        # viewer 用户不存在 → 401；补一个真实 viewer 用户再验证 403
        assert client.put(f"/api/credentials/{credential_id}/limits",
                          json={"daily_tokens": 1}).status_code == 401
