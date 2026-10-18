#!/usr/bin/env python3
"""临时诊断：TRAE 实际上下文上限 + max_mode 字段是否有效（E5 验证）。

背景（2026-10-18 实测，结论见 docs/trae-endpoint-verification-2026-10-18.md）：
- 模型表声明 `context_window_tokens.dev` 为 200K/232K，但实测可到 ~1M token；
- Trae2api-cn 的 `max_mode` 字段组加与不加，行为完全一致。

做法：针在开头、问题在结尾、中间填随机 ASCII。

    [暗号 <随机8位>] + <N 字符填充> + 「开头提到的暗号是什么？只回答暗号本身」

答对暗号 ⇒ 开头（针）与结尾（问题）必须同时在窗口内 ⇒ 窗口横跨整段填充。
这比只看「是否报错」更严格：静默截断也能被这一步抓出来。

用法：
    # 默认测几个定点，确认声明值与实际值的差距
    uv run python scripts/probe_trae_context_limit.py
    # 只测一个模型 / 一个长度
    uv run python scripts/probe_trae_context_limit.py --model glm-5.2 --chars 1450000

成本：单次长输入约 60 万–100 万 token。按 0.77 credits/1M 估算，
默认组合约 5–8 credits。**跑之前先确认凭证额度充足。**

只读：不改库、不改 src/。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import random
import string
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

# Trae2api-cn 用的那套「1M Max」字段。实测证明全部无效，保留只为对照。
MAX_MODE_FIELDS: dict[str, Any] = {
    "model_selection_strategy": "max",
    "model_auto_selection": {"strategy": "max"},
    "context_window_size": 1_000_000,
    "prompt_max_tokens": 936_000,
}

# 默认定点：声明 232768 的模型测到 1.45M 字符（≈1.03M token），
# 再加一个已知超限点确认错误码。
DEFAULT_CASES: tuple[tuple[str, int], ...] = (
    ("seed-code-pro-0430", 900_000),
    ("seed-code-pro-0430", 1_450_000),
    ("seed-code-pro-0430", 1_470_000),
    ("glm-5.2", 1_450_000),
)


def filler(chars: int) -> str:
    """确定性的随机 ASCII 填充（同 seed 保证可复现）。"""
    alphabet = string.ascii_letters + string.digits + " "
    rng = random.Random(20261020)
    chunk = "".join(rng.choices(alphabet, k=4096))
    return (chunk * ((chars // len(chunk)) + 1))[:chars]


def build_prompt(chars: int, needle: str) -> str:
    return (
        f"暗号是 {needle}，请记住它，稍后会问你。\n\n"
        f"{filler(chars)}\n\n"
        "上面这段长文本开头提到的暗号是什么？只回答暗号本身，不要解释。"
    )


def load_credential() -> TraeCredential:
    settings = load_settings()
    db = Database(str(Path(settings.data_dir) / "coding2api.sqlite3"))
    cipher = CredentialCipher(settings.app_secret)
    row = db.connect().execute(
        "SELECT data_enc FROM credentials WHERE provider='trae' AND enabled=1 "
        "ORDER BY health DESC LIMIT 1",
    ).fetchone()
    return TraeCredential.from_dict(json.loads(cipher.decrypt(row["data_enc"])))


def build_client() -> TraeClient:
    """长输入体积大：默认 write=10s 会在上传阶段 WriteTimeout。

    本脚本单独放宽写/连超时（read 仍 None，长流必须自己兜超时）。
    """
    import httpx

    return TraeClient(stream_client=httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=None, write=300.0, pool=30.0),
        follow_redirects=True,
    ))


async def ask(client: TraeClient, credential: TraeCredential, model: str,
              chars: int, extra: dict[str, Any] | None = None) -> tuple[bool, dict]:
    """发一次带暗号的长输入，返回 (是否答对暗号, 观测信息)。"""
    needle = uuid.uuid4().hex[:8]
    body = prepare_body({
        "messages": [{"role": "user", "content": build_prompt(chars, needle)}],
        "max_tokens": 32,
    }, model)
    if extra:
        body.update(extra)
    text, errors, usage = "", [], {}
    async with client._stream().stream(
        "POST", f"{client.agent_host}/api/agent/v3/llm_utils_chat",
        json=body, headers=solo_headers(credential),
    ) as response:
        if response.status_code >= 400:
            raw = await response.aread()
            return False, {"http": response.status_code,
                           "body": raw.decode("utf-8", "replace")[:200]}
        async for frame in iter_frames(response.aiter_bytes()):
            name = frame.event.strip()
            if name == "error":
                errors.append(frame.data[:160])
            elif name == "token_usage":
                usage = json.loads(frame.data)
            elif name == "output":
                with contextlib.suppress(ValueError):
                    text += json.loads(frame.data).get("response") or ""
    return needle in text, {
        "needle": needle,
        "text": text.strip()[:40],
        "prompt_tokens": usage.get("prompt_tokens"),
        "errors": errors,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=None, help="只测该模型（配 --chars）")
    parser.add_argument("--chars", type=int, default=None, help="只测该字符数")
    parser.add_argument("--compare-max-mode", action="store_true",
                        help="对每个点额外发一次带 max_mode 字段组的请求做对照")
    args = parser.parse_args()

    cases = DEFAULT_CASES
    if args.model and args.chars:
        cases = ((args.model, args.chars),)

    credential = load_credential()
    client = build_client()
    try:
        for model, chars in cases:
            ok, info = await ask(client, credential, model, chars)
            tag = "✅ 读到" if ok else "❌ 未读到"
            print(f"{model:22s} {chars:>9} chars -> {tag} "
                  f"prompt_tokens={info.get('prompt_tokens')} "
                  f"{info.get('errors') or ''}")
            if not args.compare_max_mode:
                continue
            ok_max, info_max = await ask(client, credential, model, chars,
                                         MAX_MODE_FIELDS)
            tag_max = "✅ 读到" if ok_max else "❌ 未读到"
            print(f"{'  └ max_mode 字段组':22s} {chars:>9} chars -> {tag_max} "
                  f"prompt_tokens={info_max.get('prompt_tokens')} "
                  f"{info_max.get('errors') or ''}")
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
