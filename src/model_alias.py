"""用户自定义模型别名（`MODEL_ALIASES`）：解析配置文本。

**为什么需要它**：本服务对外的模型 id 由模型目录自动归一出（`kimi-k3`、
`glm-5.2`，见 `api.model_merge`）。上游改了模型 id、或某个团队想要一个稳定的
对外名（`team-fast`）时，原先只能改客户端配置。别名把「对外名 → 目标模型
（可钉渠道）」做成一条热更配置，列表与请求解析共用同一份。

格式：`对外名=模型@渠道;对外名2=模型`。
`@渠道` 可省（不钉渠道，目标模型出现在哪些渠道就路由到哪些）；写了就必须是
`KNOWN_PROVIDERS` 之一。

解析**宽容**（与 `engine.reasoning.parse_effort_map` /
`engine.model_resolver.parse_fallback_groups` 同风格）：坏段忽略并记 warning，
一条笔误不能让整个网关起不来，也不能因此丢掉其它合法段。真正的「撞车 / 目标
不存在」是**注入期**的错误（要等模型目录到齐才判定），见
`api.model_merge._apply_custom_aliases`。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .engine.model_resolver import KNOWN_PROVIDERS

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ModelAlias:
    """一条解析后的别名配置。"""

    name: str                      # 对外名（原样保留大小写，比较时忽略）
    model: str                     # 目标模型：对外 id / 展示名 / 任一渠道原 id
    provider: str | None = None    # `@渠道`；None = 不钉渠道


def parse_model_aliases(raw: str) -> dict[str, ModelAlias]:
    """配置文本 → `{对外名: ModelAlias}`；留空 = 无别名。

    分隔：`;` 分段、`=` 分「对外名 / 目标」、`@` 分「模型 / 渠道」。重复对外名
    后者覆盖前者（与 `parse_fallback_groups` 的重复组名同口径）。

    坏段（空对外名、空目标、缺 `=`、`@` 后为空或不是已知渠道）逐段记
    warning 后跳过；空段（尾随 `;`、连续 `;`）静默跳过。
    """
    aliases: dict[str, ModelAlias] = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        name, separator, target_raw = chunk.partition("=")
        name = name.strip()
        if not separator or not name:
            logger.warning("忽略模型别名段 %r：缺少 `=` 或对外名为空", chunk)
            continue
        target = target_raw.strip()
        if not target:
            logger.warning("忽略模型别名 %r：目标模型为空", name)
            continue
        model, provider = target, None
        if "@" in target:
            model_raw, _, provider_raw = target.rpartition("@")
            model = model_raw.strip()
            provider = provider_raw.strip().lower()
            if not model or provider not in KNOWN_PROVIDERS:
                logger.warning(
                    "忽略模型别名 %r：目标 %r 的渠道 %r 未知（应为 %s 之一）",
                    name, target, provider_raw.strip(), " / ".join(KNOWN_PROVIDERS))
                continue
        aliases[name] = ModelAlias(name=name, model=model, provider=provider)
    return aliases
