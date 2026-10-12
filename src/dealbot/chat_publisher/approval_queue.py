from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .base import PublishError, PublishResult


class ApprovalQueuePublisher:
    """사람 승인 방식: 게시 예정 메시지를 outbox 폴더에 저장한다.
    운영자가 파일을 열어 오픈채팅방에 복사·붙여넣기 한 뒤 sent/ 폴더로 옮긴다."""

    channel = "approval_queue"

    def __init__(self, outbox: Path):
        self.outbox = outbox

    def publish(self, message: str) -> PublishResult:
        try:
            self.outbox.mkdir(parents=True, exist_ok=True)
            name = datetime.now().strftime("%Y%m%d_%H%M%S_%f") + ".txt"
            path = self.outbox / name
            path.write_text(message, encoding="utf-8")
        except OSError as e:
            raise PublishError(f"outbox 저장 실패: {e}", retryable=True) from e
        return PublishResult("queued", str(path))
