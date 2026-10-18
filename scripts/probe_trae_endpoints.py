#!/usr/bin/env python3
"""临时诊断：TRAE 多端点与 1M Max 逐项实测（E5 前置验证）。

只读：不改库、不改 src/、不写配置。只发少量真实请求（每次 max_tokens 很小），
用来回答三件事：
    1. `/api/ide/v1/get_detail_param` 的原始 config 里有没有 1M / Max 相关字段，
       `custom_model_1M*` 到底是不是真模型；
    2. `/api/ide/v1/chat`、`/api/ide/v2/llm_raw_chat`、`/api/remote/v1/chat_sessions`
       三个候选端点各自的真实状态码与事件形态；
    3. 每个端点能否产出可用正文（只认 `output` 帧里有非空 response）。

用法：
    uv run python scripts/probe_trae_endpoints.py --models          # 只看模型表
    uv run python scripts/probe_trae_endpoints.py --endpoints       # 再打三个端点
    uv run python scripts/probe_trae_endpoints.py --all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.engine.sse import iter_frames  # noqa: E402
from src.provider.trae.client import TraeClient, prepare_body, solo_headers  # noqa: E402
from src.provider.trae.credential import TraeCredential  # noqa: E402
from src.provider.trae.events import EP_MODELS, FUNCTION  # noqa: E402

# 测试用的模型：优先找一个普通模型与一个 1M 模型各打一次
PROBE_MODELS = ("glm-5.2", "custom_model_1M", "custom_model_1M_text")


def load_credential(db_path: str, secret: str) -> tuple[str, TraeCredential]:
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    rows = db.connect().execute(
        "SELECT id, nickname, data_enc FROM credentials "
        "WHERE provider='trae' AND enabled=1 ORDER BY health DESC, created_at",
    ).fetchall()
    for row in rows:
        data = json.loads(cipher.decrypt(row["data_enc"]).decode("utf-8"))
        cred = TraeCredential.from_dict(data)
        if cred.access_token and cred.uid:
            print(f"凭证：{row['nickname']} ({row['id']})\n")
            return row["id"], cred
    raise SystemExit("没有可用的 trae 凭证（缺 access_token / uid）")


async def dump_models(client: TraeClient, credential: TraeCredential) -> None:
    """拉原始模型表：打印全部 config 键名与 1M 相关条目。"""
    body = {
        "function": FUNCTION, "config_names": None, "need_prompt": False,
        "current_config_info": None, "poly_prompt": True,
        "mode_type": None, "agent_type": None,
    }
    data = await client._post_json(
        f"{client.agent_host}{EP_MODELS}", body, solo_headers(credential, stream=False),
    )
    configs = data.get("config_info_list") or []
    print(f"=== 模型表：{len(configs)} 条 ===")
    keys: set[str] = set()
    for config in configs:
        if isinstance(config, dict):
            keys.update(config.keys())
    print(f"config 顶层键（并集）：{sorted(keys)}\n")

    print("--- 名字含 1M / max / 长上下文的条目 ---")
    for config in configs:
        if not isinstance(config, dict):
            continue
        name = str(config.get("config_name") or "")
        blob = json.dumps(config, ensure_ascii=False)
        if any(tag in name.lower() for tag in ("1m", "max", "text")) or any(
            tag in blob.lower() for tag in ("max_mode", "context_window_size")
        ):
            print(f"  {name}: {blob[:600]}\n")

    print("--- 可用模型名（前 40）---")
    names = [str(c.get("config_name")) for c in configs if isinstance(c, dict)]
    print(", ".join(names[:40]))
    print()


# --------------------------------------------------------------- 端点探测


async def probe_llm_utils_chat(client: TraeClient, credential: TraeCredential,
                               model: str) -> dict[str, Any]:
    """基线：当前生产端点 /api/agent/v3/llm_utils_chat。"""
    body = prepare_body({
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "max_tokens": 16,
    }, model)
    return await _post_stream(
        client, credential, "/api/agent/v3/llm_utils_chat", body,
    )


async def probe_ide_chat(client: TraeClient, credential: TraeCredential,
                         model: str) -> dict[str, Any]:
    """候选 1：/api/ide/v1/chat（user_input + chat_history 形态）。"""
    session = uuid.uuid4().hex
    body = {
        "user_input": "只回复两个字：收到",
        "intent_name": "general_qa_intent",
        "variables": json.dumps({
            "language": "zh", "locale": "zh-CN",
            "input": "只回复两个字：收到", "version_code": "1.0.0",
            "workspace_path": "", "system_type": "windows",
        }, ensure_ascii=False),
        "context_resolvers": [
            {"type": "project-labels"},
            {"type": "terminal_context"},
        ],
        "chat_history": [],
        "session_id": session,
        "conversation_id": session,
        "model_name": model,
        "last_llm_response_info": {"turn": 0, "is_error": False, "response": ""},
        "max_output_tokens": 16,
    }
    return await _post_stream(client, credential, "/api/ide/v1/chat", body)


async def probe_raw_chat(client: TraeClient, credential: TraeCredential,
                         model: str) -> dict[str, Any]:
    """候选 2：/api/ide/v2/llm_raw_chat（六字段，tools 折进 system prompt）。"""
    session = uuid.uuid4().hex
    body = {
        "config_name": model,
        "conversation_id": session,
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
        "model_name": model,
        "session_id": session,
        "stream": True,
    }
    return await _post_stream(client, credential, "/api/ide/v2/llm_raw_chat", body)


async def probe_remote(client: TraeClient, credential: TraeCredential,
                       model: str) -> dict[str, Any]:
    """候选 3：/api/remote/v1/chat_sessions（两步式：建会话 → 拉事件）。"""
    result: dict[str, Any] = {"endpoint": "remote", "steps": []}
    create_body = {
        "mode": "work",
        "environment_id": "default",
        "auto_create_project": False,
        "origin": "web",
        "env": "remote",
        "initial_message": {
            "chat_session_id": "",
            "content": "只回复两个字：收到",
            "query": "只回复两个字：收到",
            "model_name": model,
            "agent_type": "solo_work_remote",
            "model_selection_strategy": "auto",
            "common_params": {},
        },
    }
    url = f"{client.agent_host}/api/remote/v1/chat_sessions"
    headers = solo_headers(credential, stream=False)
    headers["Origin"] = "https://solo.trae.cn"
    headers["Referer"] = "https://solo.trae.cn/"
    headers["X-Trae-Client-Type"] = "web"
    try:
        response = await client._short().post(url, json=create_body, headers=headers)
    except Exception as error:  # noqa: BLE001
        result["steps"].append({"step": "create", "error": type(error).__name__})
        return result
    raw = (await response.aread()).decode("utf-8", "replace")
    result["steps"].append({
        "step": "create", "status": response.status_code, "body": raw[:600],
    })
    if response.status_code >= 400:
        result["ok"] = False
        return result
    try:
        created = json.loads(raw)
    except ValueError:
        result["ok"] = False
        return result
    session_id = created.get("chat_session_id")
    message_id = created.get("message_id")
    result["steps"].append({"ids": {"session": session_id, "message": message_id}})
    if not session_id or not message_id:
        result["ok"] = False
        return result

    events_url = (
        f"{client.agent_host}/api/remote/v1/chat_sessions/{session_id}/events"
        f"?reply_to_message_id={message_id}"
    )
    headers = solo_headers(credential, stream=True)
    headers["Origin"] = "https://solo.trae.cn"
    headers["Referer"] = "https://solo.trae.cn/"
    text = ""
    names: list[str] = []
    try:
        async with client._stream().stream("GET", events_url, headers=headers) as stream:
            result["steps"].append({"step": "events", "status": stream.status_code})
            if stream.status_code >= 400:
                result["ok"] = False
                return result
            async for frame in iter_frames(stream.aiter_bytes()):
                if frame.event:
                    names.append(frame.event.strip())
                text += _extract_text(frame.data)
    except Exception as error:  # noqa: BLE001
        result["steps"].append({"step": "events", "error": type(error).__name__})
        result["ok"] = False
        return result
    result["event_names"] = sorted(set(names))[:20]
    result["text"] = text[:200]
    result["ok"] = bool(text.strip())
    return result


def _extract_text(data: str) -> str:
    """从一帧 data 里尽力取正文（兼容 output.response / content 两种形态）。"""
    if not data or data.strip() in ("[DONE]",):
        return ""
    try:
        payload = json.loads(data)
    except ValueError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("response", "content", "text", "delta"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            return value["content"]
    return ""


async def _post_stream(client: TraeClient, credential: TraeCredential, path: str,
                       body: dict[str, Any]) -> dict[str, Any]:
    """POST 一个聊天端点，收齐事件名与正文，返回结论（永不抛）。"""
    result: dict[str, Any] = {"endpoint": path}
    url = f"{client.agent_host}{path}"
    names: list[str] = []
    text = ""
    try:
        async with client._stream().stream(
            "POST", url, json=body, headers=solo_headers(credential),
        ) as response:
            result["status"] = response.status_code
            if response.status_code >= 400:
                raw = await response.aread()
                result["body"] = raw.decode("utf-8", "replace")[:600]
                result["ok"] = False
                return result
            async for frame in iter_frames(response.aiter_bytes()):
                if frame.event:
                    names.append(frame.event.strip())
                text += _extract_text(frame.data)
    except Exception as error:  # noqa: BLE001
        result["error"] = f"{type(error).__name__}: {error}"
        result["ok"] = False
        return result
    result["event_names"] = sorted(set(names))[:20]
    result["frames"] = len(names)
    result["text"] = text[:200]
    result["ok"] = bool(text.strip())
    return result


PROBES = (
    ("llm_utils_chat（现行）", probe_llm_utils_chat),
    ("ide/v1/chat", probe_ide_chat),
    ("ide/v2/llm_raw_chat", probe_raw_chat),
    ("remote/v1/chat_sessions", probe_remote),
)


async def run_endpoints(client: TraeClient, credential: TraeCredential,
                        models: tuple[str, ...]) -> None:
    print("=== 端点逐一实测（每个模型 × 每个端点各一次）===")
    for model in models:
        print(f"\n### 模型 {model}")
        for label, probe in PROBES:
            outcome = await probe(client, credential, model)
            verdict = "✅ 有正文" if outcome.get("ok") else "❌ 无正文"
            print(f"  [{label}] {verdict}")
            for key, value in outcome.items():
                if key in ("endpoint", "ok"):
                    continue
                print(f"      {key}: {value}")
    print()


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", action="store_true", help="只拉模型表")
    parser.add_argument("--endpoints", action="store_true", help="只打端点")
    parser.add_argument("--all", action="store_true", help="两者都做")
    parser.add_argument("--model", action="append", default=None,
                        help="指定模型（可重复），默认内置三个")
    args = parser.parse_args()
    if not (args.models or args.endpoints or args.all):
        args.all = True

    settings = load_settings()
    db_path = Path(settings.data_dir) / "coding2api.sqlite3"
    _cid, credential = load_credential(str(db_path), settings.app_secret)
    client = TraeClient()
    try:
        if args.models or args.all:
            await dump_models(client, credential)
        if args.endpoints or args.all:
            await run_endpoints(client, credential,
                                tuple(args.model or PROBE_MODELS))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
