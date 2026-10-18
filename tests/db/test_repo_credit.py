"""凭证仓储：积分流水记录、裁剪与查询端点。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from src.auth.session import create_session_token
from src.config import Settings
from src.db.conn import Database
from src.db.crypto import CredentialCipher
from src.db.migrate import apply_schema
from src.db.repo import CredentialRepository, CreditEventRepository
from src.main import build_app
from src.provider.base import Quota
from src.tasks.retention import RetentionTask
from tests.helpers.constants import SECRET


def test_save_quota_records_net_credit_change(tmp_path):
    """余额变化才记流水，且记的是净变化，不是动作归因。

    上游签到/成长接口不打日志；这里断言 source 只有 observed/sync 两种，
    绝不出现 checkpoint/growth 这类上游从未告知的归因。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    # 首次探测：只建立基线（sync），不算积分
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    first = events.recent(cid)
    assert len(first) == 1
    assert first[0]["source"] == "sync" and first[0]["delta"] is None
    assert first[0]["after"] == 100 and first[0]["before"] is None

    # 余额没变 → 不记（否则每轮探测落一行 0，表被噪声淹没）
    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now + 60))
    assert len(events.recent(cid)) == 1
    # 浮点噪声不算变化：上游 JSON 的浮点运算会给出 100.00000000000001 这种值
    credentials.save_quota(cid, Quota(remaining=100 + 1e-13, total=200, probed_at=now + 90))
    assert len(events.recent(cid)) == 1

    # 余额增加（签到/成长/任何来源都可能）→ 记净变化 + 区间起点
    credentials.save_quota(cid, Quota(remaining=115, total=200, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] == 15              # 落库前已按 epsilon 收敛，不残留 14.999999
    assert latest["before"] == pytest.approx(100) and latest["after"] == 115
    # 区间起点是「上次探测」，不是「上次有变动」：中间那两次没记行但都观测过，
    # 覆盖区间必须从最近一次观测算起，否则会把一段无人观测的时间也算进去
    assert latest["window_start"] == now + 90
    assert latest["source"] == "observed"

    # 余额减少（对话消耗）→ delta 为负，同样如实记
    credentials.save_quota(cid, Quota(remaining=110, total=200, probed_at=now + 180))
    assert events.recent(cid)[0]["delta"] == -5
    db.close()

def test_save_quota_keeps_last_credits_when_credit_probe_missing(tmp_path):
    """积分账探测失败（写 None）时保留上次值，不把右侧那行抹掉一整轮。

    积分账是 best-effort 探测：积分接口偶发失败只记 warning、不判整次探测失败。
    若直接写 NULL，右侧「积分 x / y」会凭空消失到下一轮，与主账「失败保留
    上次余额」的口径不一致。无积分账的渠道两列本就恒 NULL，补写 None 仍为 NULL。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    cid = credentials.add(provider="codearts", credential_data={"x": "y"}, now=now)

    # 正常探测：两本账都有值
    credentials.save_quota(cid, Quota(remaining=43.5, total=1000.0,
                                      credits_remaining=13498.72, credits_total=13500.0,
                                      probed_at=now))
    row = credentials.list_all()[0]
    assert row["quota_credits_remaining"] == pytest.approx(13498.72)
    # 主账失败/重探但积分账缺失（None）→ 保留上次积分值
    credentials.save_quota(cid, Quota(remaining=40.0, total=1000.0,
                                      credits_remaining=None, credits_total=None,
                                      probed_at=now + 60))
    row = credentials.list_all()[0]
    assert row["quota_remaining"] == pytest.approx(40.0)          # 主账照常更新
    assert row["quota_credits_remaining"] == pytest.approx(13498.72)  # 积分账保旧值
    assert row["quota_credits_total"] == pytest.approx(13500.0)
    # 无积分账的渠道：两列始终为 NULL（COALESCE 不会造出 0）
    other = credentials.add(provider="trae", credential_data={"x": "y"}, now=now)
    credentials.save_quota(other, Quota(remaining=5.0, total=10.0, probed_at=now))
    other_row = next(r for r in credentials.list_all() if r["id"] == other)
    assert other_row["quota_credits_remaining"] is None
    assert other_row["quota_credits_total"] is None
    db.close()


def test_list_all_splits_expiring_credits_by_ledger(tmp_path):
    """到期积分按账本拆开：主账＝合并阶梯 − 积分账那一段，积分账另起两个字段。

    福利组读主账字段、内置组读积分账字段。非 CodeArts 无积分阶梯，
    主账字段与改动前完全一致（拆分不改变既有渠道的数值）。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    now = int(time.time())
    cid = credentials.add(provider="codearts", credential_data={"x": "y"}, now=now)

    # 每日池（次日 0 点，36h 内）＋积分套餐（7 天内、36h 外）
    pool_end = now + 6 * 3600
    credits_end = now + 3 * 86400
    credentials.save_quota(cid, Quota(
        remaining=900.0, total=1000.0,
        expiry_ladder=[(pool_end, 900.0), (credits_end, 13498.72)],
        credits_remaining=13498.72, credits_total=13500.0,
        credits_expiry_ladder=[(credits_end, 13498.72)],
        probed_at=now))
    row = credentials.list_all(
        expiring_window=36 * 3600, expiring_secondary_window=7 * 86400, now=now)[0]
    # 主账（福利组）：只有每日池那一段落在 36h 主窗口内
    assert row["quota_expiring_credits"] == pytest.approx(900.0)
    # 次窗口（7 天）含两段，但主账字段仍是「合并 − 积分账」＝每日池那一段
    assert row["quota_expiring_credits_secondary"] == pytest.approx(900.0)
    # 积分账（内置组）：套餐到期日在 36h 外，主窗口为 0，7 天内才有
    assert row["quota_credits_expiring"] == 0.0
    assert row["quota_credits_expiring_secondary"] == pytest.approx(13498.72)
    assert row["quota_credits_expiry_ladder"] == [(credits_end, 13498.72)]

    # 非 CodeArts：无积分阶梯 → 主账字段＝合并阶梯求和，积分账字段恒 None
    other = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"},
                            now=now)
    credentials.save_quota(other, Quota(
        remaining=10.0, total=100.0, expiry_ladder=[(now + 3600, 30.0)], probed_at=now))
    other_row = next(r for r in credentials.list_all(
        expiring_window=36 * 3600, expiring_secondary_window=7 * 86400,
        now=now) if r["id"] == other)
    assert other_row["quota_expiring_credits"] == pytest.approx(30.0)
    assert other_row["quota_credits_expiring"] is None
    assert other_row["quota_credits_expiring_secondary"] is None
    assert other_row["quota_credits_expiry_ladder"] is None
    db.close()


def test_save_quota_records_both_ledgers_separately(tmp_path):
    """同一轮探测为两本账各记一条流水，`recent(ledger=...)` 分开读。

    CodeArts 的两本账（每日 token 池 / 积分账）在上游是两个独立接口、数值量级
    差一个数量级，混在一个列表里会被读成同一本账的连续读数，故必须按 `ledger`
    分流：同一 `ts` 两行（id 不同），各自只看得见自己那本。
    """
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codearts", credential_data={"x": "y"}, now=now)

    # 首次探测：两本账各建立一条基线（sync），各自 before 为空
    credentials.save_quota(cid, Quota(remaining=900.0, total=1000.0,
                                      credits_remaining=13498.72, credits_total=13500.0,
                                      probed_at=now))
    pool = events.recent(cid, ledger="pool")
    credits = events.recent(cid, ledger="credits")
    assert len(pool) == 1 and len(credits) == 1
    assert pool[0]["ledger"] == "pool" and credits[0]["ledger"] == "credits"
    assert pool[0]["after"] == pytest.approx(900.0)
    assert credits[0]["after"] == pytest.approx(13498.72)
    # 同一轮探测：两行 ts 相同、id 不同（各账一本，互不覆盖）
    assert pool[0]["ts"] == credits[0]["ts"] == now
    assert pool[0]["id"] != credits[0]["id"]

    # 第二轮两本账都变：各记各的净变化，不会互相串数
    credentials.save_quota(cid, Quota(remaining=880.0, total=1000.0,
                                      credits_remaining=13490.0, credits_total=13500.0,
                                      probed_at=now + 60))
    assert [row["delta"] for row in events.recent(cid, ledger="pool")] == [-20.0, None]
    assert [row["delta"] for row in events.recent(cid, ledger="credits")] == [-8.72, None]

    # 未知账本名回落 pool（与老库默认列值一致），绝不把两本账混在一起
    assert [row["ledger"] for row in events.recent(cid, ledger="nope")] == ["pool", "pool"]
    # 默认参数即 pool（既有调用方语义不变）
    assert [row["ledger"] for row in events.recent(cid)] == ["pool", "pool"]
    db.close()


def test_save_quota_records_unquantifiable_change(tmp_path):
    """余额变未知（探测失败后）仍记行：这是异常，但绝不量化成 0。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)

    credentials.save_quota(cid, Quota(remaining=100, total=200, probed_at=now))
    credentials.mark_probe_failed(cid, now=now + 60)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now + 120))
    latest = events.recent(cid)[0]
    assert latest["delta"] is None            # 不猜 0
    assert latest["before"] == 100 and latest["after"] is None
    assert latest["source"] == "observed"
    db.close()

def test_save_quota_skips_event_for_unknown_to_unknown(tmp_path):
    """两端都未知 = 没学到任何东西，不写空行。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=None, total=None, probed_at=now))
    assert events.recent(cid) == []
    db.close()

def test_save_quota_event_skipped_when_credential_gone(tmp_path):
    """凭证被并发删除：不补记悬挂行（查询返回 None 的分支）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    credentials.save_quota("cred_missing", Quota(remaining=1, total=2, probed_at=1))
    assert events.recent("cred_missing") == []
    db.close()

def test_credit_events_recent_is_bounded_and_ordered(tmp_path):
    """倒序返回，limit 收敛到 [1, 200]（防止一次拉爆前端）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=100)
    for step in range(5):
        credentials.save_quota(cid, Quota(remaining=100 - step, total=200, probed_at=100 + step))
    rows = events.recent(cid)
    assert [row["ts"] for row in rows] == [104, 103, 102, 101, 100]  # 最新在前
    assert len(events.recent(cid, limit=2)) == 2
    assert len(events.recent(cid, limit=0)) == 1        # 下限收敛
    assert len(events.recent(cid, limit=9999)) == 5     # 上限不报错
    # 别的凭证的记录不会串进来
    other = credentials.add(provider="trae", credential_data={"accessToken": "t"}, now=100)
    assert events.recent(other) == []
    db.close()

def test_credit_events_prune_drops_old_rows(tmp_path):
    """保留期清理：只删过期行，保留期内不动。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))
    assert events.prune(keep_days=90, now=now) == 1
    assert [row["after"] for row in events.recent(cid)] == [2]
    db.close()

def test_credited_events_endpoint_requires_admin_and_known_credential(tmp_path):
    """端到端：非管理员 403；未知凭证 400；已知凭证返回流水。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        now = int(time.time())
        repo = app.state.credentials
        cid = repo.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
        repo.save_quota(cid, Quota(remaining=50, total=100, probed_at=now))
        repo.save_quota(cid, Quota(remaining=60, total=100, probed_at=now + 60))

        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        body = client.get(f"/api/credentials/{cid}/credit-events").json()
        assert [event["delta"] for event in body["events"]] == [10, None]
        assert body["events"][1]["source"] == "sync"
        # limit 透传
        assert len(client.get(
            f"/api/credentials/{cid}/credit-events?limit=1").json()["events"]) == 1
        # 未知凭证 → 400（不是空列表，避免拼错 id 时看起来「没有记录」）
        assert client.get("/api/credentials/cred_nope/credit-events").status_code == 400
        # 非管理员（conftest 里存在的 alice）→ 403
        client.cookies.set("coding2api_session", create_session_token("alice", SECRET))
        assert client.get(f"/api/credentials/{cid}/credit-events").status_code == 403

def test_credit_events_endpoint_selects_ledger(tmp_path):
    """端到端：`ledger` 选账本；缺省 pool，未知取值回落 pool。"""
    settings = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                        ADMIN_USERNAMES="root")
    app = build_app(settings)
    with TestClient(app) as client:
        now = int(time.time())
        repo = app.state.credentials
        cid = repo.add(provider="codearts", credential_data={"x": "y"}, now=now)
        repo.save_quota(cid, Quota(remaining=900.0, total=1000.0,
                                   credits_remaining=13498.72, credits_total=13500.0,
                                   probed_at=now))
        repo.save_quota(cid, Quota(remaining=880.0, total=1000.0,
                                   credits_remaining=13490.0, credits_total=13500.0,
                                   probed_at=now + 60))

        client.cookies.set("coding2api_session", create_session_token("root", SECRET))
        # 缺省 = pool：只看到每日池的两行，看不到积分账
        default = client.get(f"/api/credentials/{cid}/credit-events").json()["events"]
        assert [event["ledger"] for event in default] == ["pool", "pool"]
        assert [event["delta"] for event in default] == [-20.0, None]
        # ledger=credits：只看到积分账的两行
        credits = client.get(
            f"/api/credentials/{cid}/credit-events?ledger=credits").json()["events"]
        assert [event["ledger"] for event in credits] == ["credits", "credits"]
        assert [event["delta"] for event in credits] == [-8.72, None]
        # 未知取值回落 pool（不报错、也不把两本账混在一起）
        unknown = client.get(
            f"/api/credentials/{cid}/credit-events?ledger=nope").json()["events"]
        assert [event["ledger"] for event in unknown] == ["pool", "pool"]


def test_retention_task_prunes_credit_events(tmp_path):
    """保留任务顺带回收积分流水；未传 credit_events 时该键为 0（老调用方兼容）。"""
    db = Database(tmp_path / "t.sqlite3")
    apply_schema(db.connect())
    credentials = CredentialRepository(db, CredentialCipher(SECRET))
    events = CreditEventRepository(db)
    now = int(time.time())
    cid = credentials.add(provider="codebuddy", credential_data={"bearer_token": "t"}, now=now)
    credentials.save_quota(cid, Quota(remaining=1, total=2, probed_at=now - 100 * 86400))
    credentials.save_quota(cid, Quota(remaining=2, total=2, probed_at=now))

    class FakeCollector:
        def rollup_hourly(self) -> int:
            return 3

        def purge_expired(self, days: int) -> int:
            return 7

    task = RetentionTask(FakeCollector(), retention_days=90,
                         credentials=credentials, credit_events=events)
    report = task.run_once()
    assert report["rolled_up"] == 3 and report["purged"] == 7
    assert report["purged_credit_events"] == 1
    # 老调用方（不传 credit_events）仍然可用
    bare = RetentionTask(FakeCollector(), retention_days=90, credentials=credentials)
    assert bare.run_once()["purged_credit_events"] == 0
    db.close()
