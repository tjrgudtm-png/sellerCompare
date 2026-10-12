"""주기 실행과 중복 실행 방지.

- 프로세스 간: OS 파일 락 (Windows msvcrt / POSIX fcntl). 다른 dealbot 이 실행 중이면 건너뜀.
- 프로세스 내: 이전 실행이 끝나기 전에는 다음 실행을 시작하지 않음 (단일 스레드 루프).
- 실행이 주기보다 오래 걸리면 밀린 주기를 몰아서 실행하지 않고 다음 정시 주기로 넘어간다.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from .monitoring import event


class LockBusy(Exception):
    pass


class RunLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None
        self._thread_lock = threading.Lock()

    def acquire(self) -> None:
        if not self._thread_lock.acquire(blocking=False):
            raise LockBusy("같은 프로세스에서 실행 중")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(self.path, "a+")
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                fh.close()
                raise LockBusy(f"다른 프로세스가 실행 중: {self.path}") from e
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            self._fh = fh
        except BaseException:
            self._thread_lock.release()
            raise

    def release(self) -> None:
        if self._fh is not None:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None
                self._thread_lock.release()

    def __enter__(self) -> "RunLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def run_locked(lock: RunLock, job: Callable[[], object]) -> object | None:
    """락을 얻으면 job 실행, 이미 실행 중이면 None (건너뜀)."""
    try:
        lock.acquire()
    except LockBusy as e:
        event(logging.WARNING, "이전 실행이 아직 진행 중이라 이번 주기를 건너뜀", reason=str(e))
        return None
    try:
        return job()
    finally:
        lock.release()


class Scheduler:
    def __init__(self, interval_minutes: float, job: Callable[[], object], lock: RunLock,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.interval = interval_minutes * 60
        self.job = job
        self.lock = lock
        self.clock = clock
        self.sleep = sleep
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run_forever(self, max_runs: int | None = None) -> int:
        runs = 0
        next_at = self.clock()
        while not self._stop.is_set():
            now = self.clock()
            if now < next_at:
                self.sleep(min(next_at - now, 5.0))
                continue
            try:
                run_locked(self.lock, self.job)
            except Exception:  # 개별 실행 오류로 스케줄러가 죽지 않게
                logging.getLogger("dealbot").exception("스케줄 실행 중 예외")
            runs += 1
            if max_runs is not None and runs >= max_runs:
                break
            # 다음 주기: 밀린 주기는 건너뛰고 미래의 첫 주기로
            next_at += self.interval
            now = self.clock()
            if next_at <= now:
                skipped = int((now - next_at) // self.interval) + 1
                next_at += skipped * self.interval
                event(logging.WARNING, "실행 시간이 주기를 초과하여 주기를 건너뜀", skipped=skipped)
        return runs
