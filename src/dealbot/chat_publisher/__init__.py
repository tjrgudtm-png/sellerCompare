from __future__ import annotations

from .approval_queue import ApprovalQueuePublisher
from .base import ChatPublisher, PublishError, PublishResult
from .dry_run import DryRunPublisher
from .kakao_pc import KakaoPcPublisher
from .telegram import TelegramPublisher


def build_publisher(cfg, http=None, repo=None) -> ChatPublisher:
    """publish.enabled=false 이면 설정된 모드와 무관하게 항상 dry-run."""
    if not cfg.get("publish.enabled"):
        return DryRunPublisher()
    mode = cfg.get("publish.mode")
    if mode == "dry_run":
        return DryRunPublisher()
    if mode == "approval_queue":
        return ApprovalQueuePublisher(cfg.path("publish.approval_queue.dir"))
    if mode == "telegram":
        return TelegramPublisher(http, cfg.secrets.telegram_bot_token, cfg.secrets.telegram_chat_id,
                                 cfg.get("publish.telegram.api_base"))
    if mode == "kakao_pc":
        k = cfg.get("publish.kakao")
        recent = (lambda since: repo.count_posts_since("kakao_pc", since)) if repo else None
        return KakaoPcPublisher(k["room_name"], k["send_mode"], bool(k["auto_send_accept_risk"]),
                                float(k["min_seconds_between_posts"]), int(k["max_posts_per_hour"]),
                                recent)
    raise PublishError(f"알 수 없는 publish.mode: {mode}", retryable=False)


__all__ = ["ApprovalQueuePublisher", "ChatPublisher", "DryRunPublisher", "KakaoPcPublisher",
           "PublishError", "PublishResult", "TelegramPublisher", "build_publisher"]
