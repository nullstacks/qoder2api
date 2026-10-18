#!/usr/bin/env python3
"""scripts/ 下一次性运维脚本的共享基建。

此前每个脚本各自复制一份 `backup()`（6 份）与 `_DbAdapter`（4 份），副本已经
分叉：适配器的 `transaction()` 丢了 `Database` 的 `BEGIN IMMEDIATE`，写库前
不取写锁 → 读改写之间存在丢更新窗口。集中在这里，只有一份实现。

这些脚本不是包内模块，靠 `sys.path` 插入仓库根后被 `import scripts._common`
（隐式命名空间包）或测试的 `load_script` 加载。
"""

from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


def backup(db_path: Path) -> Path:
    """写库前备份主库文件到同目录（`.bak-<时间戳>`），失败即中止。

    用 SQLite 在线备份 API 而不是复制文件：WAL 模式下直接 copy 主库文件会
    丢掉尚未 checkpoint 的已提交事务。
    """
    target = db_path.with_name(f"{db_path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    source = sqlite3.connect(str(db_path))
    try:
        destination = sqlite3.connect(str(target))
        try:
            source.backup(destination)
        finally:
            destination.close()
    finally:
        source.close()
    return target


class DbAdapter:
    """把裸连接包装成 `Database` 的形态（回填 / rollup 只用 connect、transaction）。

    与 `Database.transaction()` 保持同一语义，尤其 `BEGIN IMMEDIATE`：脚本的
    读改写（如 `save_quota` 的 before/after）必须立即取写锁，否则两个写者会
    基于同一旧快照更新（丢更新）。此前 4 份副本都漏了这一步。
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._depth = 0

    def connect(self) -> sqlite3.Connection:
        return self._conn

    @contextmanager
    def transaction(self):
        """嵌套只有最外层提交/回滚，与 `Database.transaction` 同语义。"""
        depth = self._depth
        if depth == 0 and not self._conn.in_transaction:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth = depth + 1
        try:
            yield self._conn
        except BaseException:
            if depth == 0:
                self._conn.rollback()
            raise
        finally:
            self._depth = depth
        if depth == 0:
            self._conn.commit()
