"""오픈채팅방 게시 메시지 생성. 실제 수집·비교 값만 사용한다."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from .models import DealCandidate, DealGrade, DealType
from .price_comparator import display_rate

TITLES = {DealGrade.S: "🚨 [S급 특가]", DealGrade.A: "🔥 [A급 특가]"}
OPTION_TITLE = "🎯 [옵션 특가]"
UNIT_TITLE = "📦 [개당 특가]"


class MessageFormatError(ValueError):
    pass


def won(value: Decimal | int) -> str:
    return f"{int(Decimal(value).to_integral_value(rounding=ROUND_HALF_UP)):,}원"


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
        comp = c.comparison
        if not (c.eligible and comp and comp.valid):
            raise MessageFormatError("검증되지 않은 후보로 메시지를 만들 수 없습니다")
        if c.deal_type == DealType.COUPANG and not (c.match and comp.grade in TITLES):
            raise MessageFormatError("게시 등급이 아닌 비교 결과입니다")
        if c.deal_type == DealType.UNIT and not (c.offer and comp.toss_unit is not None):
            raise MessageFormatError("개당 비교 정보가 없습니다")
        if c.deal_type == DealType.OPTION and not c.deal_options:
            raise MessageFormatError("특가 옵션 정보가 없습니다")

        msg = self._build(c, self._shorten(c.toss.name, self.max_name_length), compact=False)
        if len(msg) > self.max_length:
            msg = self._build(c, self._shorten(c.toss.name, 30), compact=True)
        if len(msg) > self.max_length:
            raise MessageFormatError("필수 정보만으로도 최대 길이를 초과합니다")
        return msg

    def _build(self, c: DealCandidate, name: str, compact: bool) -> str:
        body = {DealType.COUPANG: self._coupang, DealType.UNIT: self._unit,
                DealType.OPTION: self._option}[c.deal_type](c, name, compact)
        lines = body + [f"🕒 가격 확인 시각: {c.toss.collected_at.astimezone(self.tz):%Y-%m-%d %H:%M}"]
        if self.footer:  # 광고 고지 등 필수 문구일 수 있어 축약 시에도 유지
            lines.append(self.footer)
        return "\n".join(lines)

    @staticmethod
    def _link(c: DealCandidate) -> list[str]:
        return ["🔗 토스쇼핑 바로가기", c.toss.post_url]

    def _coupang(self, c: DealCandidate, name: str, compact: bool) -> list[str]:
        comp = c.comparison
        lines = [
            f"{TITLES[comp.grade]} {name}",
            f"💰 토스쇼핑 가격: {comp.toss_final:,}원" + ("" if compact else " (배송비 포함)"),
            f"🛒 쿠팡 가격: {comp.coupang_final:,}원" + ("" if compact else " (표시가 기준)"),
            f"💸 가격 차이: {comp.diff:,}원",
            f"📉 쿠팡 대비: {display_rate(comp.rate)}% 저렴",
            *self._link(c),
        ]
        if not compact:
            basis = " / ".join(c.match.basis) if c.match.basis else "동일 상품"
            lines.append(f"📌 비교 기준: {basis}")
        return lines

    def _unit(self, c: DealCandidate, name: str, compact: bool) -> list[str]:
        comp = c.comparison
        lines = [
            f"{UNIT_TITLE} {name}",
            f"💰 토스쇼핑: {comp.toss_final:,}원 ({comp.toss_count}개, 개당 {won(comp.toss_unit)})",
            f"🛒 쿠팡: {comp.coupang_final:,}원 ({comp.coupang_count}개, 개당 {won(comp.coupang_unit)})",
            f"📉 개당 {display_rate(comp.rate)}% 저렴 ({comp.toss_count}개 기준 {comp.diff:,}원 이득)",
            *self._link(c),
        ]
        if not compact:
            lines.append("📌 비교 기준: 동일 상품 / 묶음 수량만 다름 (배송비 포함, 쿠팡 표시가)")
        return lines

    def _option(self, c: DealCandidate, name: str, compact: bool) -> list[str]:
        comp = c.comparison
        names = [o.name for o in c.deal_options]
        shown = ", ".join(names[:3]) + (f" 외 {len(names) - 3}개" if len(names) > 3 else "")
        lines = [
            f"{OPTION_TITLE} {name}",
            f"👉 특가 옵션: {shown}",
            f"💰 옵션 가격: {comp.toss_final:,}원" + ("" if compact else " (배송비 포함)"),
            f"📊 다른 옵션 가격: {comp.coupang_final:,}원"
            + ("" if compact else f" (옵션 {c.compared_option_count}개 중간값)"),
            f"💸 차이: {comp.diff:,}원 ({display_rate(comp.rate)}% 저렴)",
            *self._link(c),
        ]
        if not compact:
            lines.append("⚠️ 구매 시 해당 옵션을 선택해 가격을 꼭 확인하세요")
        return lines

    @staticmethod
    def _shorten(text: str, limit: int) -> str:
        text = " ".join(text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
