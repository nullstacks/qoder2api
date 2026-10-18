"""上下文压缩接线（按实际服务渠道取窗口）。

`src/engine/compress.py` 是纯函数；本模块构造「按渠道查输入上限 → 压缩」的
闭包，注入执行引擎（`ExecutorDeps.context_compress`）。压缩点在**选号之后、
发上游之前**：同名模型挂多渠道时各自上限不同（qoder 180K vs codebuddy 1M），
路由前取最小值会让大窗口渠道的会话被小窗口渠道的阈值反复裁剪——估算器口径
偏高时每轮裁剪结果不稳定，上游前缀缓存只命中 system 段（实测命中率从 96%+
跌到 7-16%）。按实际落到的渠道取窗口后，未超限的会话零裁剪，前缀单调增长、
缓存可正常命中。

目录里查不到输入上限时**不压缩**：宁可不裁剪，也不拿猜出来的数字去砍用户
上下文；因此本功能对未知模型是零副作用的。

**声明值 ≠ 实际可用量**：上游目录里的 `max_input_tokens` 是模型自报的声明值，
个别渠道严重低估（TRAE 声明 200K/232K，实测可吃约 1M token）。压缩预算用
`_CHANNEL_WINDOW_FLOOR` 抬到实测下限，但 `/v1/models` 对外宣告仍用声明值——
客户端的保守认知与我们的实际裁剪阈值分开，既不虚假承诺，也不主动制造有损压缩。
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ..engine.compress import compress_messages

# 渠道实际窗口下限（只用于**压缩预算**，不影响 `/v1/models` 对外宣告）。
#
# 上游模型目录里的 `max_input_tokens` 是「声明值」，可能与实际可接受量差很远。
# TRAE 实测：声明 232768/200000，实际 1,450,000 字符（约 1,035,544 token）仍能
# 正确读到开头内容，1,470,000 字符才报 4022/4026（针在开头、问题在结尾、
# 中间填随机 ASCII，答对暗号即证明窗口横跨整段；复现见
# scripts/probe_trae_context_limit.py）。按声明值算，压缩预算只有
# 217K token，会在用户发 30 万 token 时把上游明明能吃的内容静默压掉。
#
# 只抬压缩预算、不改宣告值：客户端看到的仍是保守上限，不会主动发超限请求；
# 我们自己也不再制造有损压缩。取值 1,048,576（2^20）与实测硬边界吻合，且经
# `safety_ratio` 后实际预算约 992K，低于实测通过点。
_CHANNEL_WINDOW_FLOOR: dict[str, int] = {"trae": 1_048_576}


def context_window_for(cache: dict[str, dict[str, Any]],
                       aliases: dict[str, dict[str, str]],
                       provider_id: str, model: str) -> int | None:
    """单渠道的模型输入上限：按归一名或别名原始 id 在该渠道目录取值。

    `model` 可带 `@provider` 后缀（防御：executor 传入的已是去掉后缀的
    归一名，这里再剥一层不伤直调方）。查不到 / 值非法（≤0、非 int、bool）
    视为未知，返回 None。

    查到后按 `_CHANNEL_WINDOW_FLOOR` 抬到渠道实测下限：该表只描述「压缩时
    敢用多大预算」，不影响对外宣告。
    """
    name = (model or "").strip()
    if "@" in name:
        name = name.rsplit("@", 1)[0].strip()
    key = name.lower()
    if not key:
        return None
    table = cache.get(provider_id) or {}
    candidates = {key}
    raw = (aliases.get(provider_id) or {}).get(key)
    if raw:
        candidates.add(raw.lower())
    for candidate in candidates:
        entry = table.get(candidate)
        value = getattr(entry, "max_input_tokens", None)
        # bool 是 int 子类，显式排除；<=0 视为未上报
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return max(value, _CHANNEL_WINDOW_FLOOR.get(provider_id, 0))
    return None


def build_context_compressor(
        cache: dict[str, dict[str, Any]],
        aliases: dict[str, dict[str, str]],
        settings: Any) -> Callable[[str, str, dict], dict]:
    """构造 `(provider_id, model, payload) → payload` 的压缩闭包。

    `settings` 传运行态覆盖层（`RuntimeSettings`）：开关与四个参数每轮
    请求现读，管理台热更即生效。装配侧（main.py）把它注入
    `ExecutorDeps.context_compress`；executor 在选号后按实际渠道调用。
    """
    def compress(provider_id: str, model: str, payload: dict) -> dict:
        if not settings.context_compress_enabled:
            return payload
        window = context_window_for(cache, aliases, provider_id, model)
        if window is None:
            return payload
        messages = payload.get("messages")
        if not isinstance(messages, list):
            return payload
        compressed = compress_messages(
            messages,
            max_input_tokens=window,
            reserve_for_output=int(settings.context_compress_reserve_tokens),
            min_keep_messages=int(settings.context_compress_min_keep_messages),
            safety_ratio=float(settings.context_compress_safety_ratio),
        )
        if compressed is messages:
            return payload
        # 不原地改 payload：同一请求可能在轮换/回退里再压一次（另一个渠道、
        # 另一个窗口），必须始终从原文出发，且 affinity 指纹读的是原文
        return {**payload, "messages": compressed}
    return compress
