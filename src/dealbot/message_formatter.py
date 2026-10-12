"""오픈채팅방 게시 메시지 생성. 실제 수집·비교 값만 사용한다."""

from __future__ import annotations

from zoneinfo import ZoneInfo

from .models import DealCandidate, DealGrade
from .price_comparator import display_rate

TITLES = {DealGrade.S: "🚨 [S급 특가]", DealGrade.A: "🔥 [A급 특가]"}


class MessageFormatError(ValueError):
    pass


class MessageFormatter:
    def __init__(self, max_length: int = 1000, max_name_length: int = 60, footer: str = "",
                 tz: str = "Asia/Seoul"):
        self.max_length = max_length
        self.max_name_length = max_name_length
        self.footer = footer.strip()
        self.tz = ZoneInfo(tz)

    @classmethod
    def from_config(cls, cfg) -> "MessageFormatter":
        return cls(int(cfg.get("message.max_length")), int(cfg.get("message.max_name_length")),
                   str(cfg.get("message.footer") or ""), str(cfg.get("app.timezone")))

    def format(self, c: DealCandidate) -> str:
        comp, toss, match = c.comparison, c.toss, c.match
        if not (c.eligible and comp and comp.valid and match and comp.grade in TITLES):
            raise MessageFormatError("검증되지 않은 후보로 메시지를 만들 수 없습니다")
        checked_at = toss.collected_at.astimezone(self.tz).strftime("%Y-%m-%d %H:%M")
        basis = " / ".join(match.basis) if match.basis else "동일 상품"
        name = self._shorten(toss.name, self.max_name_length)

        def build(name: str, compact: bool) -> str:
            lines = [
                f"{TITLES[comp.grade]} {name}",
                f"💰 토스쇼핑 가격: {comp.toss_final:,}원" + ("" if compact else " (배송비 포함)"),
                f"🛒 쿠팡 가격: {comp.coupang_final:,}원" + ("" if compact else " (표시가 기준)"),
                f"💸 가격 차이: {comp.diff:,}원",
                f"📉 쿠팡 대비: {display_rate(comp.rate)}% 저렴",
                "🔗 토스쇼핑 바로가기",
                toss.post_url,
            ]
            if not compact:
                lines.append(f"📌 비교 기준: {basis}")
            lines.append(f"🕒 가격 확인 시각: {checked_at}")
            if self.footer:  # 광고 고지 등 필수 문구일 수 있어 축약 시에도 유지
                lines.append(self.footer)
            return "\n".join(lines)

        msg = build(name, compact=False)
        if len(msg) > self.max_length:
            msg = build(self._shorten(toss.name, 30), compact=True)
        if len(msg) > self.max_length:
            raise MessageFormatError("필수 정보만으로도 최대 길이를 초과합니다")
        return msg

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
