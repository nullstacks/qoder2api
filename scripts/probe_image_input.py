#!/usr/bin/env python3
"""只读实测：各渠道上游是否接受图片内容块（扫描报告 §4.15 的证据脚本）。

发一次最小对话（64×64 纯红 PNG，问「什么颜色」），不改库、不改 src/。
`--text-only` 做文本对照，用来区分「渠道/额度不可用」与「图片被拒」。

用法：
    uv run python scripts/probe_image_input.py --provider codebuddy
    uv run python scripts/probe_image_input.py --provider all
    uv run python scripts/probe_image_input.py --provider trae --text-only

结论（2026-10-15，七渠道真实凭证）：出站均已是 OpenAI `image_url` 内容块原样
透传，上游全部接受图片，故 provider 出站零改动；详见
`docs/competitor-scan-2026-10-14.md` §4.15 的实测表。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.conn import Database  # noqa: E402
from src.db.crypto import CredentialCipher  # noqa: E402
from src.provider.base import EventKind  # noqa: E402


def make_png(width: int = 64, height: int = 64, rgb: tuple[int, int, int] = (220, 20, 20)) -> bytes:
    """生成纯色 PNG（不依赖 Pillow）。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


PNG = make_png()
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


def image_payload(model: str, text_only: bool = False) -> dict:
    if text_only:
        content: object = "这张图片是什么颜色？只回答一个词。"
    else:
        content = [
            {"type": "text", "text": "这张图片是什么颜色？只回答一个词。"},
            {"type": "image_url", "image_url": {"url": DATA_URL}},
        ]
    return {
        "model": model,
        "stream": True,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": content}],
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
    reasoning = ""
    error = ""
    finish = ""
    usage = None
    async for event in stream:
        if event.kind is EventKind.CONTENT:
            content += event.content or ""
        elif event.kind is EventKind.REASONING:
            reasoning += event.content or ""
        elif event.kind is EventKind.ERROR:
            error = f"code={event.error_code} kind={event.error_kind} {event.error_message}"
        elif event.kind is EventKind.FINISH:
            finish = event.finish_reason or ""
        elif event.kind is EventKind.USAGE and event.usage:
            usage = {"in": event.usage.input_tokens, "out": event.usage.output_tokens}
    return {"content": content, "reasoning_len": len(reasoning),
            "error": error, "finish": finish, "usage": usage}


async def probe(provider: str, model: str, db_path: str, secret: str,
                text_only: bool = False) -> None:
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
    proxies_raw = settings.provider_proxies
    from src.provider.proxy import parse_provider_proxies
    proxies = parse_provider_proxies(proxies_raw, (
        "trae", "codebuddy", "codebuddy_intl", "zen", "kilo", "qoder", "codearts"))

    creds = load_credentials(db_path, secret, provider)
    if provider in ("kilo", "zen") and not creds:
        creds = [("virtual", {})]
    if not creds:
        print(f"[{provider}] 无可用凭证，跳过")
        return

    payload = image_payload(model, text_only=text_only)
    for credential_id, data in creds[:1]:
        print(f"\n=== {provider} / {model} / cred {credential_id} ===")
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
            await client.aclose()
        except Exception as error:  # noqa: BLE001
            print(f"异常 {type(error).__name__}: {str(error)[:400]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True)
    parser.add_argument("--model", default=None)
    parser.add_argument("--text-only", action="store_true",
                        help="对照：只发文本，判断失败是图片导致还是渠道本身不可用")
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
                          text_only=args.text_only))


if __name__ == "__main__":
    main()
