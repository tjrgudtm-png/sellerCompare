"""9. 연속 실행 및 스케줄 중복 방지 테스트"""
from __future__ import annotations

import threading

import pytest

from dealbot.scheduler import LockBusy, RunLock, Scheduler, run_locked


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_lock_prevents_overlapping_runs(tmp_path):
    lock = RunLock(tmp_path / "x.lock")
    calls = []

    def job():
        calls.append("outer")
        assert run_locked(RunLock(tmp_path / "x.lock"), lambda: calls.append("inner")) is None
        assert run_locked(lock, lambda: calls.append("same")) is None
        return "done"

    assert run_locked(lock, job) == "done"
    assert calls == ["outer"]
    assert run_locked(lock, lambda: "again") == "again"  # 해제 후 재획득 가능


def test_lock_released_on_exception(tmp_path):
    lock = RunLock(tmp_path / "x.lock")
    with pytest.raises(ValueError):
        run_locked(lock, lambda: (_ for _ in ()).throw(ValueError()))
    assert run_locked(lock, lambda: 1) == 1


def test_scheduler_runs_on_interval_and_survives_errors(tmp_path):
    clock = Clock()
    runs = []

    def job():
        runs.append(clock.t)
        if len(runs) == 2:
            raise RuntimeError("boom")

    s = Scheduler(10, job, RunLock(tmp_path / "l"), clock=clock, sleep=clock.sleep)
    assert s.run_forever(max_runs=3) == 3
    assert runs == [0.0, 600.0, 1200.0]


def test_long_run_skips_missed_ticks_instead_of_piling_up(tmp_path):
    clock = Clock()
    runs = []

    def job():
        runs.append(clock.t)
        clock.t += 1500  # 25분 걸리는 실행

    s = Scheduler(10, job, RunLock(tmp_path / "l"), clock=clock, sleep=clock.sleep)
    s.run_forever(max_runs=3)
    assert runs == [0.0, 1800.0, 3600.0]  # 겹치지 않고 다음 정시 주기로


def test_concurrent_threads_only_one_runs(tmp_path):
    lock = RunLock(tmp_path / "l")
    started = threading.Event()
    release = threading.Event()
    results = []

    def slow():
        started.set()
        release.wait(5)
        return "slow"

    t = threading.Thread(target=lambda: results.append(run_locked(lock, slow)))
    t.start()
    started.wait(5)
    results.append(run_locked(lock, lambda: "fast"))
    release.set()
    t.join(5)
    assert sorted(map(str, results)) == ["None", "slow"]


def test_lockbusy_raised_directly(tmp_path):
    a = RunLock(tmp_path / "l")
    a.acquire()
    with pytest.raises(LockBusy):
        RunLock(tmp_path / "l").acquire()
    a.release()
