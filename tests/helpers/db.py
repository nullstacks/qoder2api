"""SQLite 临时库与仓储构造助手。

各测试文件此前各自写一份 `db`/`repo` 夹具（`_load`/`_insert` 同理），
这里给出一份逐字节等价的共享实现；夹具本体仍留在 `tests/conftest.py`。
"""

from __future__ import annotations

import sqlite3

from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository

from .constants import SECRET

__all__ = [
    "apply_schema_to",
    "credential_repo",
    "insert_usage_event",
    "new_database",
]


def new_database(tmp_path, filename: str = "t.sqlite3") -> Database:
    """建临时库并建表，返回 `Database`（调用方负责 `close()`）。"""
    db = Database(tmp_path / filename)
    apply_schema(db.connect())
    return db


def apply_schema_to(db: Database) -> Database:
    """对已存在的 `Database` 建表（幂等）。"""
    apply_schema(db.connect())
    return db


def credential_repo(tmp_path, filename: str = "t.sqlite3"):
    """返回 `(CredentialRepository, Database)`；调用方负责 `db.close()`。"""
    db = new_database(tmp_path, filename)
    return CredentialRepository(db, CredentialCipher(SECRET)), db


def insert_usage_event(
    db: Database,
    *,
    id: str,
    ts: int,
    provider: str,
    model: str,
    username: str = "u",
    credential_id: str | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    cached_tokens: int | None = None,
    credit: float | None = None,
    credit_estimated: int = 0,
    ok: int = 1,
) -> None:
    """插入一条 `usage_events` 明细（默认 ok=1）。

    与各脚本测试里手写的 `_insert` 等价，字段超集：调用方只传需要覆盖的。
    """
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, ok,"
            " input_tokens, output_tokens, cached_tokens, credit, credit_estimated)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (id, ts, username, provider, credential_id, model, ok,
             input_tokens, output_tokens, cached_tokens, credit, credit_estimated),
        )


def fetch_all(db: Database, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """便捷查询：取回全部行。"""
    return db.connect().execute(sql, params).fetchall()
