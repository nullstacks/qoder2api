"""测试替身存储。

`MemoryStore` 是最小 `SettingsStore`：`tests/config/test_provider_wiring.py` 与
`tests/provider/codebuddy/test_intl.py` 曾各写一份逐字节等价的实现，收敛到这里。
"""

from __future__ import annotations

__all__ = ["MemoryStore"]


class MemoryStore:
    """最小 SettingsStore：只实现读写三动作。"""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.rows: dict[str, str] = dict(initial or {})

    def load(self) -> dict[str, str]:
        return dict(self.rows)

    def set(self, key: str, value: str, now: int | None = None) -> None:
        self.rows[key] = value

    def delete(self, key: str) -> None:
        self.rows.pop(key, None)
