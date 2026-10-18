#!/usr/bin/env python3
"""只读实测：各渠道上游是否接受 Anthropic 风格的 `cache_control` 标记。

`compat/anthropic/request.py` 把 Anthropic 请求映射成 chat 载荷时，`cache_control`
目前被白名单静默丢弃（见 `docs/competitor-comparison.md` §3.1）。要不要透传，
取决于上游是「忽略未知字段」还是「严格校验后 400」——后者会让透传直接把请求
打成 4xx（还会冷却凭证），不能靠猜。

本脚本发一次最小对话，正文是**内容块数组**，块上带
`cache_control: {"type": "ephemeral"}`，并额外给工具定义带同一个标记；对比
`--plain`（同形状但不带 cache_control）判断失败是标记导致还是渠道本身不可用。
不改库、不改 src/。

用法：
    uv run python scripts/probe_cache_control.py --provider codebuddy
    uv run python scripts/probe_cache_control.py --provider all
    uv run python scripts/probe_cache_control.py --provider trae --plain

结论（2026-10-17，真实凭证）：见 `docs/competitor-comparison.md` §3.1 与
`TECHNICAL.md` §11.4 的实测记录。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.provider.base import EventKind  # noqa: E402

CACHE_CONTROL = {"type": "ephemeral"}
TOOL_NAME = "ping"


def cache_payload(model: str, plain: bool = False) -> dict:
    """覆盖出站映射会产生的**全部** cache_control 落点。

    这些形状与 `compat/anthropic/request.py` 透传后产出的 chat 载荷一一对应：
    1. system 消息内容块数组
    2. user 消息内容块数组
    3. assistant `tool_calls` 顶层（Anthropic `tool_use` 块的标记）
    4. tool 消息内容块数组（Anthropic `tool_result` 块）
    5. 工具定义顶层
    """
    marker = {} if plain else {"cache_control": CACHE_CONTROL}
    tool = {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": "测试用空工具",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    if not plain:
        tool["cache_control"] = CACHE_CONTROL
    tool_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": TOOL_NAME, "arguments": ""},
    }
    if not plain:
        tool_call["cache_control"] = CACHE_CONTROL
    return {
        "model": model,
        "stream": True,
        "max_tokens": 512,
        "messages": [
            {"role": "system",
             "content": [{"type": "text", "text": "你是回声机。", **marker}]},
            {"role": "user",
             "content": [{"type": "text", "text": "先调用 ping 工具。", **marker}]},
            {"role": "assistant",
             "content": [{"type": "text", "text": "好的。", **marker}],
             "tool_calls": [tool_call],
             **marker},
            {"role": "tool", "tool_call_id": "call_1",
             "content": [{"type": "text", "text": "", **marker}], **marker},
            {"role": "user",
             "content": [{"type": "text", "text": "只回答一个词：pong", **marker}]},
        ],
        "tools": [tool],
    }


def load_credentials(db_path: str, secret: str, provider: str) -> list[tuple[str, dict]]:
    db = Database(db_path)
    cipher = CredentialCipher(secret)
    rows = db.connect().execute(
        "SELECT id, nickname, data_enc FROM credentials WHERE provider = ? "
        "AND enabled = 1 AND disabled = 0 ORDER BY created_at", (provider,)).fetchall()
    out: list[tuple[str, dict]] = []
    for row in rows:
        data = json.loads(cipher.decrypt(row["data_enc"]).decode("utf-8"))
        out.append((row["id"], data))
    return out


async def consume(stream) -> dict:
    content = ""
    error = ""
    finish = ""
    async for event in stream:
        if event.kind is EventKind.CONTENT:
            content += event.content or ""
        elif event.kind is EventKind.ERROR:
            error = f"code={event.error_code} kind={event.error_kind} {event.error_message}"
        elif event.kind is EventKind.FINISH:
            finish = event.finish_reason or ""
    return {"content": content[:80], "error": error, "finish": finish}


async def probe(provider: str, model: str, db_path: str, secret: str,
                plain: bool = False) -> None:
    from src.provider.codearts.client import CodeArtsClient
    from src.provider.codearts.credential import CodeArtsCredential
    from src.provider.codebuddy.client import CodeBuddyClient
    from src.provider.codebuddy.client import parse_credential as cb_parse
    from src.provider.kilo.client import KiloClient
    from src.provider.qoder.client import QoderClient
    from src.provider.qoder.credential import parse_credential as qd_parse
    from src.provider.trae.client import TraeClient
    from src.provider.trae.credential import parse_credential as tr_parse
    from src.provider.zen.client import ZenClient

    settings = load_settings()
    from src.provider.proxy import parse_provider_proxies
    proxies = parse_provider_proxies(settings.provider_proxies, (
        "trae", "codebuddy", "codebuddy_intl", "zen", "kilo", "qoder", "codearts"))

    creds = load_credentials(db_path, secret, provider)
    if provider in ("kilo", "zen") and not creds:
        creds = [("virtual", {})]
    if not creds:
        print(f"[{provider}] 无可用凭证，跳过")
        return

    payload = cache_payload(model, plain=plain)
    for credential_id, data in creds[:1]:
        print(f"\n=== {provider} / {model} / cred {credential_id} "
              f"/ {'plain' if plain else 'cache_control'} ===")
        client = None
        try:
            if provider == "codebuddy":
                client = CodeBuddyClient(proxy=proxies.get("codebuddy"))
                result = await consume(client.stream_chat(
                    cb_parse(data), payload, model))
            elif provider == "codebuddy_intl":
                client = CodeBuddyClient(
                    endpoint="https://www.codebuddy.ai",
                    require_leading_system=True,
                    proxy=proxies.get("codebuddy_intl"))
                result = await consume(client.stream_chat(
                    cb_parse(data), payload, model))
            elif provider == "trae":
                client = TraeClient(proxy=proxies.get("trae"))
                result = await consume(client.stream_chat(
                    tr_parse(data), payload, model))
            elif provider == "qoder":
                client = QoderClient(proxy=proxies.get("qoder"))
                result = await consume(client.stream_chat(
                    qd_parse(data), payload, model))
            elif provider == "codearts":
                client = CodeArtsClient(proxy=proxies.get("codearts"))
                result = await consume(client.stream_chat(
                    CodeArtsCredential.from_dict(data), payload, model))
            elif provider == "kilo":
                client = KiloClient(proxy=proxies.get("kilo"))
                result = await consume(client.stream_chat(payload, model))
            elif provider == "zen":
                client = ZenClient(proxy=proxies.get("zen"))
                result = await consume(client.stream_chat(payload, model))
            else:
                print(f"未知渠道 {provider}")
                return
            print(json.dumps(result, ensure_ascii=False, indent=2))
        except Exception as error:  # noqa: BLE001
            print(f"异常 {type(error).__name__}: {str(error)[:400]}")
        finally:
            if client is not None:
                await client.aclose()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--plain", action="store_true",
                        help="对照：同形状但不带 cache_control")
    parser.add_argument("--db", default=os.environ.get("DB_PATH", "data/coding2api.sqlite3"))
    args = parser.parse_args()
    settings = load_settings()

    defaults = {
        "codebuddy": "deepseek-v4.1-flash",
        "codebuddy_intl": "deepseek-v4.1-flash",
        "trae": "glm-5.2",
        "qoder": "qmodel",
        "codearts": "Qwen3-VL-235B",
        "kilo": "dots-studio/dots-3-note-preview:free",
        "zen": "space-bunny-free",
    }
    providers = list(defaults) if args.provider == "all" else [args.provider]
    for provider in providers:
        model = args.model or defaults[provider]
        asyncio.run(probe(provider, model, args.db, settings.app_secret,
                          plain=args.plain))


if __name__ == "__main__":
    main()
