"""签到定时（E4）：时点解析、抖动窗口、按「日期 + 时点」防重复。

两条容易退化的语义：

1. **留空 = 旧行为**：`checkin_times=""` 时 `due()` 恒 True（全天每 10 分钟
   一轮），老部署升级后签到节奏不变。
2. **去重键用「时点」而不是实际执行时刻**：抖动只改窗口、不改归属，同一时点
   当天最多放行一次；跨天自动重置。

随机源全部注入，测试不依赖真随机、不 sleep。
"""

from __future__ import annotations

import time

import pytest

from src.config import Settings
from src.tasks.checkin import CheckinTask, parse_checkin_times
from tests.helpers.constants import SECRET


def _at(hour: int, minute: int, *, day: int = 11, second: int = 0) -> time.struct_time:
    """构造本地时间结构（2026-09-11 为基准日）。"""
    return time.struct_time((2026, 9, day, hour, minute, second, 0, 0, -1))


def _task(*, times: str = "", jitter: int = 0, rng=None) -> CheckinTask:
    # due() 不碰凭证库，传 None 足够；providers 同理
    return CheckinTask(None, {}, times=times, jitter_minutes=jitter, rng=rng)  # type: ignore[arg-type]


# ------------------------------------------------------------------ 解析

def test_parse_checkin_times_sorts_and_dedupes():
    assert parse_checkin_times("21:10,09:37,09:37") == (9 * 60 + 37, 21 * 60 + 10)
    # 首尾空白与空段都容忍
    assert parse_checkin_times(" 09:05 , ,21:10,") == (9 * 60 + 5, 21 * 60 + 10)


def test_parse_checkin_times_empty_means_unrestricted():
    assert parse_checkin_times("") == ()
    assert parse_checkin_times("   ") == ()
    assert parse_checkin_times(",") == ()


@pytest.mark.parametrize("text", ["25:00", "09:99", "24:00", "12:60", "-1:30"])
def test_parse_checkin_times_rejects_out_of_range(text):
    assert parse_checkin_times(text) == ()


def test_parse_checkin_times_ignores_bad_segments_only(caplog):
    """坏段只丢自己，不能连合法时点一起丢（宽容解析）。"""
    with caplog.at_level("WARNING"):
        parsed = parse_checkin_times("09:37,abc,12,09:99,25:00,:30,10:,21:10")
    assert parsed == (9 * 60 + 37, 21 * 60 + 10)
    assert sum("已忽略" in record.getMessage() for record in caplog.records) == 6


def test_parse_checkin_times_requires_colon(caplog):
    with caplog.at_level("WARNING"):
        assert parse_checkin_times("0937") == ()
    assert any("缺少冒号" in record.getMessage() for record in caplog.records)


# ------------------------------------------------------- 留空 = 旧行为

def test_due_without_times_is_always_true():
    """留空 = 全天每 10 分钟一轮：任意时刻都放行，且不写任何去重状态。"""
    task = _task()
    for moment in (_at(0, 0), _at(9, 37), _at(23, 59)):
        assert task.due(now=moment) is True
    assert task._slot_offsets == {} and task._fired_slots == set()


def test_due_ignores_broken_config_and_falls_back_to_old_behavior():
    """整段配置都是坏段 → 解析为空 → 回到旧行为（不能整天不签到）。"""
    task = _task(times="9点半,晚上")
    assert task.due(now=_at(3, 3)) is True


# --------------------------------------------------- 有配置：命中与抖动

def test_due_hits_configured_minute_without_jitter():
    task = _task(times="09:37,21:10")
    assert task.due(now=_at(9, 37)) is True


@pytest.mark.parametrize("hour,minute", [(9, 36), (9, 38), (12, 0), (21, 9), (21, 11)])
def test_due_misses_outside_configured_minute_without_jitter(hour, minute):
    """抖动=0 时窗口为 0：只有恰在该分钟命中，前后一分钟都不算。"""
    task = _task(times="09:37,21:10")
    assert task.due(now=_at(hour, minute)) is False


def test_due_fires_inside_jitter_window_before_slot():
    """抖动向前提前 offset 分钟：窗口 [时点-offset, 时点] 内放行。"""
    task = _task(times="09:37", jitter=5, rng=lambda span: 5)
    assert task.due(now=_at(9, 31)) is False     # 窗口外（再早 1 分钟）
    assert task.due(now=_at(9, 32)) is True      # 窗口起点


def test_due_misses_after_slot_even_with_jitter():
    task = _task(times="09:37", jitter=15, rng=lambda span: 0)
    assert task.due(now=_at(9, 37)) is True
    assert task.due(now=_at(9, 38)) is False


def test_jitter_offset_is_sampled_once_per_slot_per_day():
    """窗口内的第二次询问不得重抽偏移——否则窗口会跳动，既可能漏也可能重复。"""
    spans: list[int] = []

    def rng(span: int) -> int:
        spans.append(span)
        return 2

    task = _task(times="09:37", jitter=5, rng=rng)
    assert task.due(now=_at(9, 30)) is False     # 首次询问即抽偏移（09:35 起）
    assert spans == [5]
    assert task.due(now=_at(9, 35)) is True
    assert task.due(now=_at(9, 36)) is False     # 已放行 + 偏移未重抽
    assert spans == [5]


def test_jitter_offset_is_clamped_to_slot_and_rng_result():
    """偏移不能越过当日 0 点；随机源给出越界值也要夹回 0..span。"""
    spans: list[int] = []

    def rng(span: int) -> int:
        spans.append(span)
        return 30                                # 越界：应夹到 span

    task = _task(times="00:05", jitter=30, rng=rng)
    assert task.due(now=_at(0, 0)) is True
    assert spans == [5]                          # span = min(jitter, 时点)
    assert task.due(now=_at(0, 1)) is False


def test_jitter_uses_default_random_source(monkeypatch):
    """未注入随机源时走 `random.randint`（生产路径），仍只抽一次。"""
    calls: list[int] = []

    def fake_randint(low: int, high: int) -> int:
        calls.append(high)
        return 3

    monkeypatch.setattr("src.tasks.checkin.random.randint", fake_randint)
    task = _task(times="10:00", jitter=10)
    assert task.due(now=_at(9, 56)) is False
    assert task.due(now=_at(9, 57)) is True
    assert calls == [10]


def test_negative_jitter_treated_as_zero():
    task = _task(times="09:37", jitter=-5)
    assert task.due(now=_at(9, 36)) is False
    assert task.due(now=_at(9, 37)) is True


def test_multiple_slots_fire_independently():
    """多时点各自放行一次：早上跑过不影响晚上那个时点。"""
    task = _task(times="09:37,21:10")
    assert task.due(now=_at(9, 37)) is True
    assert task.due(now=_at(9, 40)) is False
    assert task.due(now=_at(21, 10)) is True
    assert task.due(now=_at(21, 10)) is False


# --------------------------------------------------------- 防重复与跨天

def test_same_slot_fires_only_once_per_day():
    """抖动让实际执行时刻落在不同分钟，去重键仍按「日期 + 时点」——只跑一次。"""
    task = _task(times="09:37", jitter=5, rng=lambda span: 5)
    assert task.due(now=_at(9, 32)) is True
    assert task.due(now=_at(9, 33)) is False
    assert task.due(now=_at(9, 37)) is False
    assert task._fired_slots == {"2026-09-11:577"}


def test_slot_resets_next_day():
    task = _task(times="09:37")
    assert task.due(now=_at(9, 37)) is True
    assert task.due(now=_at(9, 37, day=12)) is True


def test_stale_day_state_is_pruned():
    """跨天后丢掉前一天的偏移与放行记录（否则长驻进程无上限增长）。"""
    task = _task(times="09:37", jitter=5, rng=lambda span: 1)
    assert task.due(now=_at(9, 36)) is True
    assert set(task._slot_offsets) == {"2026-09-11:577"}
    assert task._fired_slots == {"2026-09-11:577"}
    # 次日第一次询问：只留下当天的键（偏移在新的一天重新抽定）
    assert task.due(now=_at(0, 1, day=12)) is False
    assert set(task._slot_offsets) == {"2026-09-12:577"}
    assert task._fired_slots == set()


def test_slot_state_survives_same_day_queries():
    task = _task(times="09:37", jitter=5, rng=lambda span: 2)
    assert task.due(now=_at(8, 0)) is False
    assert task._slot_offsets == {"2026-09-11:577": 2}
    assert task.due(now=_at(9, 0)) is False
    assert task._slot_offsets == {"2026-09-11:577": 2}


def test_times_and_jitter_are_hot_reloadable():
    """时点与抖动存的是取值器：改配置立刻生效，不必重启。"""
    current = {"times": "", "jitter": 0}
    task = CheckinTask(None, {},  # type: ignore[arg-type]
                       times=lambda: current["times"],
                       jitter_minutes=lambda: current["jitter"],
                       rng=lambda span: span)
    assert task.due(now=_at(12, 0)) is True       # 留空 = 旧行为

    current["times"] = "09:37"
    current["jitter"] = 4
    assert task.due(now=_at(9, 32)) is False
    assert task.due(now=_at(9, 33)) is True


# ----------------------------------------------------------- runner 接线

def test_build_runner_wires_checkin_schedule(tmp_path):
    """装配点把时点/抖动以零参 lambda 传进 CheckinTask（热更而非快照）。"""
    from src.db.conn import Database
    from src.db.crypto import CredentialCipher
    from src.db.migrate import apply_schema
    from src.db.repo import CredentialRepository
    from src.stats.collector import StatsCollector
    from src.tasks.runner import build_runner

    db = Database(tmp_path / "checkin.sqlite3")
    apply_schema(db.connect())
    repo = CredentialRepository(db, CredentialCipher(SECRET))
    config = Settings(_env_file=None, APP_SECRET=SECRET, DATA_DIR=str(tmp_path),
                      CHECKIN_TIMES="09:37,21:10", CHECKIN_JITTER_MINUTES="15")
    runner = build_runner(repo, {}, StatsCollector(db), config)

    checkin = runner._checkin  # noqa: SLF001 - 装配接线断言
    assert checkin._times() == "09:37,21:10"          # noqa: SLF001
    assert checkin._jitter() == 15                     # noqa: SLF001
    # 留空配置时仍回到旧行为
    default = build_runner(repo, {}, StatsCollector(db),
                           Settings(_env_file=None, APP_SECRET=SECRET,
                                    DATA_DIR=str(tmp_path)))
    assert default._checkin.due(now=_at(3, 0)) is True  # noqa: SLF001
