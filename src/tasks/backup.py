"""SQLite 备份后台任务：周期性把主库一致性快照写进 `DATA_DIR/backups/`。

为什么要有后台任务而不是只留脚本：脚本要人记得跑，而「忘了备份」恰恰是备份
最常见的失效方式。本任务与 `retention` 一样跑在进程内（周期可热更），每次
落一份独立快照并保留最近 N 份。

与 `retention` 的分工：清理是删、备份是留，两者都不碰对方的数据。备份读取
主库时用 `VACUUM INTO` 的一致性快照，不会与并发写入互相干扰（见 db/backup.py）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..config import live
from ..db.backup import backup_database


class BackupTask:
    """一轮 = 备份主库一次并清理旧份。

    构造只收「库路径 + 保留份数」，周期由 TaskRunner 的循环控制（与其余任务
    同一套热更机制）。`keep` 可以是零参取值器（每轮现读，热更「保留份数」
    立即生效）；`keep <= 0` 表示不清理（只备份）。
    """

    def __init__(self, db_path: str | Path, *,
                 keep: int | Callable[[], int] = 7) -> None:
        self._db_path = str(db_path)
        self._keep = live(keep)

    def run_once(self) -> dict[str, object]:
        """同步执行一轮（磁盘 IO，调用方负责放到线程池，见 runner）。"""
        keep = int(self._keep())
        target = backup_database(self._db_path, keep=keep)
        return {"backup": target.name, "path": str(target), "keep": keep}
