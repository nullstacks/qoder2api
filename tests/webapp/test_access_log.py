"""请求级结构化日志（logfmt）：脱敏口径、路由模板、级别与热更开关。"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import Settings
from src.main import build_app
from src.request_context import set_request_id
from src.webapp.access_log import (
    MAX_PATH_LENGTH,
    RequestLogMiddleware,
    _route_label,
    _should_log,
)
from tests.helpers.constants import SECRET

LOGGER_NAME = "src.access"


@pytest.fixture(autouse=True)
def _reset_request_id():
    """contextvar 在同线程用例之间会残留。"""
    set_request_id(None)
    yield
    set_request_id(None)


def _lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records
            if record.name == LOGGER_NAME]


def _parse(line: str) -> dict[str, str]:
    """logfmt → dict（本模块的值不含空格，直接 split）。"""
    return dict(item.split("=", 1) for item in line.split(" "))


# ------------------------------------------------------------------ 纯函数

def test_should_log_only_business_paths():
    """只记业务请求：静态资源与探针交给 uvicorn 的 access log。"""
    for path in ("/api/credentials", "/v1/chat/completions", "/authorize",
                 "/api", "/v1"):
        assert _should_log(path) is True
    for path in ("/", "/assets/index-abc.js", "/health", "/healthz",
                 "/metrics", "/favicon.svg"):
        assert _should_log(path) is False


def test_route_label_prefers_template():
    """命中具体路由时用模板：不把资源 ID 写进日志。"""
    class _Route:
        path = "/api/credentials/{credential_id}"

    assert _route_label({"route": _Route()}) == "/api/credentials/{credential_id}"


def test_route_label_falls_back_to_sanitized_path():
    """未匹配到具体路由（404 / SPA catch-all）时回落原始路径并清洗。"""

    class _Spa:
        path = "/{path:path}"

    assert _route_label({"route": _Spa(), "path": "/api/nope"}) == "/api/nope"
    # 无 route 键（被外层中间件拒掉时可能没有）
    assert _route_label({"path": "/v1/typo"}) == "/v1/typo"
    # 控制字符与换行一律替换，防日志注入
    assert _route_label({"path": "/api/a\nb\tc"}) == "/api/a?b?c"
    # 超长路径截断
    label = _route_label({"path": "/api/" + "x" * 500})
    assert len(label) == MAX_PATH_LENGTH


def test_route_label_blank_path_returns_slash():
    assert _route_label({"path": ""}) == "/"


# ------------------------------------------------------------------ 中间件

def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/api/credentials/{credential_id}")
    async def credential(credential_id: str):
        return {"id": credential_id}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/boom")
    async def boom():
        raise RuntimeError("boom")

    return app


def test_middleware_logs_route_template_and_status(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    set_request_id("rid-42")
    with TestClient(_app()) as client:
        assert client.get("/api/credentials/cred_abc").status_code == 200
    lines = _lines(caplog)
    assert len(lines) == 1
    fields = _parse(lines[0])
    assert fields["event"] == "request"
    assert fields["request_id"] == "rid-42"
    assert fields["method"] == "GET"
    # 模板而不是实际路径：cred_abc 不能出现在日志里
    assert fields["route"] == "/api/credentials/{credential_id}"
    assert "cred_abc" not in lines[0]
    assert fields["status"] == "200"
    assert int(fields["duration_ms"]) >= 0


def test_middleware_never_logs_query_string(caplog):
    """query string 可能含客户端塞进去的 token，一律不记。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with TestClient(_app()) as client:
        client.get("/api/credentials/cred_1?api_key=sk-secret-value")
    text = "\n".join(_lines(caplog))
    assert "sk-secret-value" not in text
    assert "?" not in text.split("route=")[1].split(" ")[0]


def test_middleware_skips_non_business_paths(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with TestClient(_app()) as client:
        client.get("/health")
    assert _lines(caplog) == []


def test_middleware_5xx_is_warning_and_4xx_is_info(caplog):
    """5xx 是服务端问题值得 WARNING；4xx 是客户端用法问题，留 INFO 防淹没。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    app = _app()
    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/api/boom").status_code == 500
        assert client.get("/api/credentials/x").status_code == 200
    levels = {_parse(line)["route"]: record.levelno
              for line, record in zip(_lines(caplog),
                                      [r for r in caplog.records
                                       if r.name == LOGGER_NAME], strict=True)}
    assert levels["/api/boom"] == logging.WARNING


def test_middleware_logs_even_when_downstream_raises(caplog):
    """异常冒泡时也要留一行（否则「挂掉的请求」是唯一没日志的请求）。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    with TestClient(_app()) as client, pytest.raises(RuntimeError):
        client.get("/api/boom")
    lines = _lines(caplog)
    assert len(lines) == 1
    assert _parse(lines[0])["status"] == "500"


def test_middleware_ignores_non_http_scope():
    import asyncio

    seen: list[str] = []

    async def app(scope, receive, send):
        seen.append(scope["type"])

    middleware = RequestLogMiddleware(app)
    asyncio.run(middleware({"type": "lifespan"}, None, None))
    assert seen == ["lifespan"]


def test_middleware_enabled_callable_gates_logging(caplog):
    """开关是取值器：False 时一条都不记，改回 True 立即恢复。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    state = {"on": False}
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware, enabled=lambda: state["on"])

    @app.get("/api/ping")
    async def ping():
        return {"ok": True}

    with TestClient(app) as client:
        client.get("/api/ping")
        assert _lines(caplog) == []
        state["on"] = True
        client.get("/api/ping")
    assert len(_lines(caplog)) == 1


# --------------------------------------------------------------- 接入 build_app

def test_build_app_registers_request_log_middleware(tmp_path, caplog):
    """端到端：真实 build_app 装配后，业务请求打出带 request_id 的一行。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        response = client.get("/api/nav-badges",
                              headers={"X-Request-ID": "client-rid"})
    assert response.status_code in (200, 401, 403)
    lines = _lines(caplog)
    assert len(lines) == 1
    fields = _parse(lines[0])
    assert fields["request_id"] == "client-rid"
    assert fields["route"] == "/api/nav-badges"


def test_request_log_is_hot_updatable(tmp_path, caplog):
    """热更关掉后立即不再记，恢复默认后立即又记（无需重启）。"""
    caplog.set_level(logging.INFO, logger=LOGGER_NAME)
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    runtime = app.state.runtime_settings
    with TestClient(app) as client:
        runtime.set("request_log_enabled", False)
        client.get("/api/nav-badges")
        assert _lines(caplog) == []
        runtime.reset("request_log_enabled")
        client.get("/api/nav-badges")
    assert len(_lines(caplog)) == 1
