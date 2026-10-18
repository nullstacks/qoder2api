"""测试共享基础设施。

本包只放**纯函数与常量**，不放 pytest 夹具：夹具需要被 conftest 或测试模块
导入才生效，放在这里不会自动发现，反而容易误以为已生效。需要共享的夹具统一
放 `tests/conftest.py`。

分模块：
- `constants`：测试通用常量（APP_SECRET、预置用户）
- `paths`：仓库内绝对路径（脚本、上游样本），避免各文件重复 `Path(__file__).parent.parent`
- `db`：SQLite 内存/临时库与仓储构造
- `http`：`httpx.MockTransport` 客户端工厂与 SSE 帧构造
- `auth`：会话 Cookie 与登录助手
"""

from __future__ import annotations

__all__ = ["auth", "constants", "db", "http", "paths"]
