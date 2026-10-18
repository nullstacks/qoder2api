"""用户文件存储与密码哈希。"""

from __future__ import annotations

import pytest

from src.auth.users import UsersFileError, UsersFileStore, create_password_hash, verify_password


def test_password_hash_roundtrip():
    h = create_password_hash("p@ss", iterations=600_000)
    assert verify_password("p@ss", h)
    assert not verify_password("wrong", h)

def test_password_hash_rejects_bad_iterations():
    with pytest.raises(ValueError):
        create_password_hash("p", iterations=100)

@pytest.mark.parametrize("bad", ["", "plain", "md5$1$a$b", "pbkdf2_sha256$x$a$b",
                                 "pbkdf2_sha256$1000$a$b", "pbkdf2_sha256$600000!!!a$b"])
def test_verify_password_rejects_malformed(bad):
    assert verify_password("p", bad) is False

def test_users_file_store(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text(
        "# comment\n"
        f"alice:{create_password_hash('pw1', iterations=600_000)}\n"
        f"\ncarol:{create_password_hash('pw2', iterations=600_000)}\n",
        encoding="utf-8",
    )
    store = UsersFileStore(path)
    assert store.verify("alice", "pw1")
    assert not store.verify("alice", "nope")
    assert not store.verify("bob", "pw1")
    assert "carol" in store.list_usernames() and "bob" not in store.list_usernames()
    assert set(store.list_usernames()) == {"alice", "carol"}
    store.validate()

def test_users_file_missing(tmp_path):
    with pytest.raises(UsersFileError):
        UsersFileStore(tmp_path / "nope.txt").validate()

def test_users_file_empty(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("# only comment\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()

def test_users_file_invalid_line(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("nocolon\n", encoding="utf-8")
    with pytest.raises(UsersFileError):
        UsersFileStore(path).validate()
