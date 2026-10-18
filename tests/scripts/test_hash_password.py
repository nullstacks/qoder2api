"""scripts/hash_password.py 的用户文件读写测试。"""

from __future__ import annotations

import stat

import pytest

from src.auth.users import verify_password
from tests.helpers.paths import load_script, script_path

SCRIPT = script_path("hash_password")


def _load():
    return load_script("hash_password")


SCRIPT_MODULE = _load()


def test_main_creates_user_file_with_restrictive_mode(tmp_path):
    path = tmp_path / "nested" / "users.txt"
    assert SCRIPT_MODULE.main(["alice", "--output", str(path), "--password", "pw"]) == 0
    assert verify_password("pw", path.read_text(encoding="utf-8").split(":", 1)[1].strip())
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_main_replaces_existing_username(tmp_path):
    path = tmp_path / "users.txt"
    SCRIPT_MODULE.main(["alice", "--output", str(path), "--password", "pw1"])
    SCRIPT_MODULE.main(["bob", "--output", str(path), "--password", "pw2"])
    SCRIPT_MODULE.main(["alice", "--output", str(path), "--password", "pw3"])

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    records = dict(line.split(":", 1) for line in lines)
    assert set(records) == {"alice", "bob"}
    assert verify_password("pw3", records["alice"])
    assert verify_password("pw2", records["bob"])


def test_main_preserves_comments_and_blank_lines(tmp_path):
    path = tmp_path / "users.txt"
    path.write_text("# 注释\n\nalice:pbkdf2_sha256$600000$x$y\n", encoding="utf-8")
    SCRIPT_MODULE.main(["bob", "--output", str(path), "--password", "pw"])
    assert len(path.read_text(encoding="utf-8").strip().splitlines()) == 2


@pytest.mark.parametrize("argv", [[""], ["  "]])
def test_main_rejects_bad_input(argv, tmp_path):
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main([*argv, "--output", str(tmp_path / "u.txt")])


@pytest.mark.parametrize("username", ["a:b", "a\nb", "a\rb"])
def test_main_rejects_username_with_separator_or_control_chars(username, tmp_path):
    """用户名会写进 `名:hash` 行：冒号/换行会截断或注入记录。"""
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main([username, "--output", str(tmp_path / "u.txt"),
                            "--password", "pw"])


def test_main_password_stdin(tmp_path, monkeypatch):
    """--password-stdin：从 stdin 读密码，不进 argv。"""
    import io

    monkeypatch.setattr(SCRIPT_MODULE.sys, "stdin", io.StringIO("stdin-pw\n"))
    path = tmp_path / "u.txt"
    assert SCRIPT_MODULE.main(["alice", "--output", str(path),
                               "--password-stdin"]) == 0
    assert verify_password("stdin-pw", path.read_text(encoding="utf-8").split(":", 1)[1].strip())


def test_main_password_stdin_empty_rejected(tmp_path, monkeypatch):
    import io

    monkeypatch.setattr(SCRIPT_MODULE.sys, "stdin", io.StringIO("\n"))
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--output", str(tmp_path / "u.txt"),
                            "--password-stdin"])


def test_main_rejects_empty_password(tmp_path):
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--output", str(tmp_path / "u.txt"), "--password", ""])


def test_main_rejects_inconsistent_confirmation(tmp_path, monkeypatch):
    answers = iter(["pw1", "pw2"])
    monkeypatch.setattr(SCRIPT_MODULE.getpass, "getpass", lambda *_: next(answers))
    with pytest.raises(SystemExit):
        SCRIPT_MODULE.main(["alice", "--output", str(tmp_path / "u.txt")])


def test_main_accepts_matching_confirmation(tmp_path, monkeypatch):
    answers = iter(["pw", "pw"])
    monkeypatch.setattr(SCRIPT_MODULE.getpass, "getpass", lambda *_: next(answers))
    path = tmp_path / "u.txt"
    assert SCRIPT_MODULE.main(["alice", "--output", str(path)]) == 0
    assert verify_password("pw", path.read_text(encoding="utf-8").split(":", 1)[1].strip())


def test_upsert_and_load_records_helpers(tmp_path):
    path = tmp_path / "u.txt"
    assert SCRIPT_MODULE.load_records(path) == []
    records = SCRIPT_MODULE.upsert(["a:1", "b:2"], "a", "new")
    assert sorted(records) == ["a:new", "b:2"]


def test_write_atomic_cleans_up_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "u.txt"

    def boom(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(SCRIPT_MODULE.os, "replace", boom)
    with pytest.raises(RuntimeError):
        SCRIPT_MODULE.write_atomic(path, ["a:1"])
    assert list(tmp_path.iterdir()) == []
