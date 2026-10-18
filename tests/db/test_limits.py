"""凭证级限额护栏的持久化：credential_limits 表 + 当日用量聚合。"""

from __future__ import annotations

import time

from src.db.conn import Database
from src.db.limits import LimitsRepository, _per_model_value, day_usage
from src.db.migrate import apply_schema


def _open(tmp_path):
    db = Database(tmp_path / "limits.sqlite3")
    apply_schema(db.connect())
    return db, LimitsRepository(db)


def test_set_get_and_clear_limits(tmp_path):
    _db, repo = _open(tmp_path)
    assert repo.get("cred_1") is None

    repo.set("cred_1", keep_credits=50.0, daily_tokens=1000, daily_credits=3.5,
             per_model_tokens={"glm-5": 100}, updated_by="root", now=123)
    record = repo.get("cred_1")
    assert record is not None
    assert (record.keep_credits, record.daily_tokens, record.daily_credits) == \
        (50.0, 1000, 3.5)
    assert record.per_model_tokens == {"glm-5": 100}
    assert record.updated_at == 123 and record.updated_by == "root"
    assert repo.map()["cred_1"].credential_id == "cred_1"

    # 整体替换：再次写入只带一个护栏，其余归 None
    repo.set("cred_1", daily_tokens=5)
    assert repo.get("cred_1").keep_credits is None

    # 全关（空配置）→ 删除该行，回到「不受限」
    repo.set("cred_1", updated_by="root")
    assert repo.get("cred_1") is None
    assert repo.map() == {}


def test_set_with_all_zero_clears_row(tmp_path):
    _db, repo = _open(tmp_path)
    repo.set("cred_1", keep_credits=0, daily_tokens=0, daily_credits=0,
             per_model_tokens={"glm-5": 0})
    assert repo.get("cred_1") is None


def test_per_model_dirty_json_is_tolerated(tmp_path):
    """脏 per_model_tokens（非 JSON / 非对象 / 非数值项）当空/跳过，不崩。"""
    assert _per_model_value(None) == {}
    assert _per_model_value("not json") == {}
    assert _per_model_value("[1,2]") == {}
    assert _per_model_value('{"a": "x", "b": 3, "c": null}') == {"b": 3}


def test_per_model_text_drops_blank_and_zero():
    from src.db.limits import _per_model_text

    assert _per_model_text({"": 5, "a": 0, "b": 7}) == '{"b": 7}'


def test_day_usage_aggregates_per_credential_and_model(tmp_path):
    db, _repo = _open(tmp_path)
    conn = db.connect()
    now = int(time.time())
    rows = [
        # (credential_id, model, ts, input, output, credit)
        ("c1", "glm-5", now, 100, 50, 1.5),
        ("c1", "glm-5", now - 10, 10, 0, 0.5),
        ("c1", "kimi", now, 20, 0, None),          # credit 缺失按 0
        ("c2", "glm-5", now - 100000, 999, 999, 9),  # 早于 since → 不计
        (None, "glm-5", now, 5, 5, 1),             # 无凭证 → 不计
    ]
    for i, (cid, model, ts, inp, out, credit) in enumerate(rows):
        conn.execute(
            "INSERT INTO usage_events (id, ts, username, provider, credential_id, model, "
            "ok, input_tokens, output_tokens, credit) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f"e{i}", ts, "u", "codebuddy", cid, model, 1, inp, out, credit))
    conn.commit()

    usage = day_usage(db, since=now - 3600)
    assert set(usage) == {"c1"}
    c1 = usage["c1"]
    assert c1.tokens == 100 + 50 + 10 + 20
    assert c1.credits == 2.0
    assert c1.per_model == {"glm-5": 160, "kimi": 20}
