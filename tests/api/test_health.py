"""/healthz 池计数。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.main import build_app
from tests.helpers.auth import isolated_settings as _settings


def test_healthz_reports_pool_counts(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok" and body["service"] == "coding2api"
    assert body["version"]
    counts = body["credentials"]
    # 默认装配会种子 zen / kilo 两条无凭证虚拟凭证（可选中 → ready）
    assert counts == {"total": 2, "ready": 2, "cooling": 0, "paused": 0, "disabled": 0}
    # 老端点保留
    assert client.get("/health").json() == {"status": "ok"}


def test_every_response_carries_request_id(client):
    """请求 ID 挂在最外层：普通响应与「被拒的请求」都必须带 X-Request-ID。"""
    import re

    response = client.get("/health")
    assert re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", response.headers["X-Request-ID"])
    # 入站合法 ID 沿用（便于与客户端日志对齐）
    echoed = client.get("/health", headers={"X-Request-ID": "trace-abc"})
    assert echoed.headers["X-Request-ID"] == "trace-abc"
    # 入站非法 ID 丢弃、改用自生成
    bad = client.get("/health", headers={"X-Request-ID": "bad id"})
    assert bad.headers["X-Request-ID"] != "bad id"
    # 请求体超限的 413 由内层 ASGI 中间件自造，最外层仍要补上头
    too_large = client.post("/api/auth/login", content=b"x" * (8 * 1024 + 1))
    assert too_large.status_code == 413
    assert too_large.headers.get("X-Request-ID")

def test_healthz_counts_partition_the_pool(tmp_path):
    """四类计数互斥且合计 = total；口径与调度器 is_selectable 一致。"""
    import sqlite3

    settings = _settings(tmp_path)
    app = build_app(settings)
    repo = app.state.credentials
    repo.add(provider="trae", credential_data={"accessToken": "a"})
    paused = repo.add(provider="trae", credential_data={"accessToken": "b"})
    cooling = repo.add(provider="trae", credential_data={"accessToken": "c"})
    broken = repo.add(provider="trae", credential_data={"accessToken": "d"})
    repo.set_enabled(paused, False)
    # 硬禁用/冷却没有公开 setter（生产路径由 save_error 驱动），直接改库构造状态
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE credentials SET disabled = 1 WHERE id = ?", (broken,))
    conn.execute("UPDATE credentials SET cooling_until = ? WHERE id = ?",
                 (2 ** 31 - 1, cooling))
    conn.commit()
    conn.close()
    with TestClient(app) as http:
        counts = http.get("/healthz").json()["credentials"]
    # 4 条 trae + 种子的一条 zen、一条 kilo（都 ready）
    assert counts == {"total": 6, "ready": 3, "cooling": 1, "paused": 1,
                      "disabled": 1}


def test_healthz_returns_503_when_no_ready_credential(tmp_path):
    """就绪语义：ready==0 回 503（进程活着但没一个可用凭证）。

    计数仍要随 503 一起返回，监控才能读到「为什么不可用」。`/health`
    保持 200（存活探针不看池子）。
    """
    import sqlite3

    settings = _settings(tmp_path)
    app = build_app(settings)
    # 把种子（zen / kilo）连同新加的凭证全部禁用 → ready=0
    repo = app.state.credentials
    repo.add(provider="trae", credential_data={"accessToken": "a"})
    conn = sqlite3.connect(settings.db_path)
    conn.execute("UPDATE credentials SET enabled = 0")
    conn.commit()
    conn.close()
    with TestClient(app) as http:
        response = http.get("/healthz")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "unavailable"
        assert body["credentials"]["ready"] == 0
        assert body["credentials"]["total"] == body["credentials"]["paused"] == 3
        # 存活探针不受池子影响
        assert http.get("/health").status_code == 200


def test_healthz_503_when_pool_is_empty(tmp_path):
    """空池（连种子都被删光）同样不可用 → 503，计数全 0。"""
    import sqlite3

    settings = _settings(tmp_path)
    app = build_app(settings)
    conn = sqlite3.connect(settings.db_path)
    conn.execute("DELETE FROM credentials")
    conn.commit()
    conn.close()
    with TestClient(app) as http:
        response = http.get("/healthz")
    assert response.status_code == 503
    assert response.json()["credentials"] == {
        "total": 0, "ready": 0, "cooling": 0, "paused": 0, "disabled": 0}
