#!/usr/bin/env python3
"""手动触发一次 SQLite 一致性备份（后台任务之外的运维入口）。

后台任务（`tasks/backup.py`）默认每日自动备份一次；本脚本用于「升级/迁移前
手动留一份」这类需要立刻备份的场景。与后台任务共用 `src/db/backup.py`：
`VACUUM INTO` 一致性快照 + `integrity_check` + chmod 600 + 保留 N 份。

为什么不是复制文件：WAL 模式下主库文件不含尚未 checkpoint 的已提交事务，
直接 `cp` 会丢数据。

用法：
    python3 scripts/backup_db.py                 # 备份到 DATA_DIR/backups/，保留 7 份
    python3 scripts/backup_db.py --keep 14       # 保留 14 份
    python3 scripts/backup_db.py --db PATH       # 指定数据库
    python3 scripts/backup_db.py --out DIR       # 指定备份目录
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_settings  # noqa: E402
from src.db.backup import DEFAULT_KEEP, backup_database  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SQLite 一致性备份（VACUUM INTO）")
    parser.add_argument("--db", help="SQLite 路径（默认 DATA_DIR/coding2api.sqlite3）")
    parser.add_argument("--out", help="备份目录（默认 主库同级 backups/）")
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP,
                        help=f"保留份数，0 表示不清理（默认 {DEFAULT_KEEP}）")
    args = parser.parse_args(argv)

    db_path = Path(args.db) if args.db else Path(load_settings().db_path)
    if not db_path.is_file():
        parser.error(f"数据库不存在: {db_path}")

    target = backup_database(db_path, keep=args.keep,
                             directory=Path(args.out) if args.out else None)
    print(f"已备份: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
