from __future__ import annotations

from ..http import ExternalError, HttpClient, HttpStatusError
from .base import PublishError, PublishResult


class TelegramPublisher:
    """대체 채널: Telegram Bot API sendMessage (공식 API)."""

    channel = "telegram"

    def __init__(self, http: HttpClient, token: str | None, chat_id: str | None,
                 api_base: str = "https://api.telegram.org"):
        if not token or not chat_id:
            raise PublishError("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID 미설정", retryable=False)
        self.http = http
        self.url = f"{api_base.rstrip('/')}/bot{token}/sendMessage"
        self.chat_id = chat_id

    def publish(self, message: str) -> PublishResult:
        try:
            data = self.http.post_json(self.url, json={"chat_id": self.chat_id, "text": message,
                                                       "disable_web_page_preview": False})
        except HttpStatusError as e:
            raise PublishError(f"telegram HTTP {e.status}", retryable=e.status >= 500 or e.status == 429) from e
        except ExternalError as e:
            raise PublishError(f"telegram {e.kind} 오류", retryable=True) from e
        if not isinstance(data, dict) or not data.get("ok"):
            raise PublishError("telegram 응답 ok=false", retryable=False)
        return PublishResult("sent", f"message_id={data.get('result', {}).get('message_id')}")
