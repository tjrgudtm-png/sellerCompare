"""메시지 전송 인터페이스. 가격 비교 로직과 분리되어 전송 방식을 교체할 수 있다."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class PublishError(Exception):
    def __init__(self, message: str, retryable: bool = True):
        super().__init__(message)
        self.retryable = retryable


@dataclass
class PublishResult:
    # sent: 전송 확인 / handed_off: 사람이 최종 전송 / queued: 승인 대기열 저장 / dry_run: 출력만
    status: str
    detail: str = ""


class ChatPublisher(Protocol):
    channel: str

    def publish(self, message: str) -> PublishResult: ...
