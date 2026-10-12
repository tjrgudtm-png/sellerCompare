from __future__ import annotations

import logging

from ..monitoring import event
from .base import PublishResult


class DryRunPublisher:
    """테스트 모드: 실제 전송 없이 게시 예정 메시지만 출력한다."""

    channel = "dry_run"

    def __init__(self, printer=print):
        self.printer = printer

    def publish(self, message: str) -> PublishResult:
        self.printer("\n===== [DRY-RUN] 게시 예정 메시지 =====\n" + message + "\n" + "=" * 36)
        event(logging.INFO, "dry-run 게시 예정 메시지 출력", length=len(message))
        return PublishResult("dry_run", "출력만 함")
