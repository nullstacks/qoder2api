"""凭证标签：解析、规范化与读回（纯函数，不碰数据库）。

标签是运维自己贴的记号（「生产」/「备用」/「测试」/「低倍率」），用来在凭证列表里
按标记收敛，不参与任何调度或鉴权判定——这一点必须写死：一旦标签影响选号，
`tags` 就从展示字段变成策略字段，改名会静默改变线上流量。

三条纪律：

- **存储形态固定为逗号分隔文本**（与 `allowed_models` / `allowed_ips` 同款），
  读回时再拆成列表给前端。写入口校验、读路径容错：库里若有手工改出来的脏数据
  （超长 / 超量 / 空段），读回只丢弃越界项，绝不抛错把整个凭证列表带崩。
- **去重按大小写不敏感、保留首次写法**：`Prod` 与 `prod` 是同一个标签，
  但显示哪个由用户先写的那个决定，不做静默改写。
- **写入时拒绝，而不是静默截断**：标签数量和长度超限直接 400 告诉用户，
  静默丢弃会让用户以为标签存上了（下次打开却少一个）。
"""

from __future__ import annotations

# 单凭证标签数上限：标签是给人看的收敛维度，超过一屏就失去意义了。
MAX_TAGS = 16
# 单个标签长度上限（中文标签 8 字左右足够；纯防脏数据与界面撑爆）。
MAX_TAG_LENGTH = 24

# 输入分隔符：逗号（中英文都收）。中文输入法下逗号极易打成全角，
# 若只认半角，用户会得到「一个叫『生产，备用』的标签」这种结果。
_SEPARATORS = str.maketrans({"，": ",", ";": ",", "；": ","})


def split_tags(raw: str | None) -> list[str]:
    """逗号分隔文本 → 去空白后的非空标签列表（不过滤长度/数量）。"""
    if not raw:
        return []
    return [item.strip() for item in raw.translate(_SEPARATORS).split(",") if item.strip()]


def parse_tags(raw: object) -> list[str]:
    """API 输入 → 标签列表；非法（超长 / 超量 / 非字符串项）抛 ValueError。

    同时接受列表（前端 JSON 数组）与逗号分隔字符串（手工调接口 / OpenAPI 试玩）：
    两种写法都能表达同一件事，只认一种会让另一半调用方撞 400。
    """
    if raw is None:
        return []
    items: list[object]
    if isinstance(raw, str):
        items = split_tags(raw)
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("tags must be a list or a comma-separated string")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str):
            raise ValueError("tags items must be strings")
        tag = item.strip()
        if not tag:
            continue
        if len(tag) > MAX_TAG_LENGTH:
            raise ValueError(f"标签过长（上限 {MAX_TAG_LENGTH} 字符）: {tag[:MAX_TAG_LENGTH]}…")
        # 存储形态是逗号分隔文本：标签自身含分隔符会被拆成两条，读写不等价。
        # 列表输入绕过了 split（且 split 会把全角逗号也算分隔符），必须在写入前拦住。
        if "," in tag.translate(_SEPARATORS):
            raise ValueError(f"标签不能包含逗号或分号: {tag!r}")
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
    if len(normalized) > MAX_TAGS:
        raise ValueError(f"标签最多 {MAX_TAGS} 条")
    return normalized


def normalize_tags(raw: object) -> str:
    """API 输入 → 落库文本（逗号分隔，供调用方回显与存储共用一份规范化）。"""
    return ",".join(parse_tags(raw))


def tags_text(tags: list[str]) -> str:
    """已校验的标签列表 → 落库文本（仓储写入路径用）。

    假定调用方已过 `parse_tags`（API 入口保证）；这里只做拼接，不重复校验——
    重复校验会在仓储层抛出 400 语义的异常，而仓储不知道 HTTP 状态码这回事。
    """
    return ",".join(tags)


def tags_value(stored: str | None) -> list[str]:
    """落库文本 → 列表。脏数据只丢弃越界项，不抛错（读路径容错）。

    超长/超量的历史数据一律滤掉：这些行可能是手工改库或早期版本写坏的数据，
    读列表时让它们整份失败，等于用一条脏标签换掉整页凭证。
    """
    items = split_tags(stored)
    out: list[str] = []
    seen: set[str] = set()
    for tag in items:
        if len(tag) > MAX_TAG_LENGTH:
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
    return out[:MAX_TAGS]
