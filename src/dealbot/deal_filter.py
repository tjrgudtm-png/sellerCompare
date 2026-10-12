"""자동 게시 대상 선별. 모든 조건을 만족해야 eligible (AND)."""

from __future__ import annotations

from decimal import Decimal
from urllib.parse import urlsplit

from .models import DealCandidate, DealGrade, MatchLevel


def is_valid_toss_url(url: str | None, allowed_hosts: list[str]) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return False
    host = parts.hostname or ""
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


class DealFilter:
    def __init__(self, min_rate: float = 20, min_amount: int = 10000,
                 publish_grades: list[str] | None = None, allowed_hosts: list[str] | None = None):
        self.min_rate = Decimal(str(min_rate))
        self.min_amount = int(min_amount)
        self.publish_grades = {DealGrade(g) for g in (publish_grades or ["S", "A"])}
        self.allowed_hosts = allowed_hosts or []

    @classmethod
    def from_config(cls, cfg) -> "DealFilter":
        return cls(cfg.get("deal.min_saving_rate"), cfg.get("deal.min_saving_amount"),
                   cfg.get("deal.publish_grades"), cfg.get("toss.allowed_url_hosts"))

    def evaluate(self, c: DealCandidate) -> DealCandidate:
        r: list[str] = []
        t = c.toss
        if c.offer is None or c.match is None:
            r.append("쿠팡 비교 대상 없음")
        elif c.match.level != MatchLevel.HIGH:
            r.append(f"동일상품 신뢰도 {c.match.level.value}")
        if t.item_price is None:
            r.append("토스 판매가 미확인")
        if t.shipping_fee is None:
            r.append("토스 배송비 미확인")
        if c.offer is not None and c.offer.final_price is None:
            r.append("쿠팡 최종가 미확인(배송비/가격)")
        if t.sold_out is not False:
            r.append("품절" if t.sold_out else "판매 가능 여부 미확인")
        if not is_valid_toss_url(t.post_url, self.allowed_hosts):
            r.append("토스 상품 링크 유효하지 않음")
        comp = c.comparison
        if comp is None or not comp.valid:
            r.append(f"가격 비교 불가: {comp.reason if comp else '비교 없음'}")
        else:
            if comp.rate < self.min_rate:
                r.append(f"절감률 미달({comp.rate:.1f}% < {self.min_rate}%)")
            if comp.diff < self.min_amount:
                r.append(f"차액 미달({comp.diff:,}원 < {self.min_amount:,}원)")
            if comp.grade not in self.publish_grades:
                r.append(f"게시 대상 등급 아님({comp.grade.value})")
        c.reject_reasons = r
        c.eligible = not r
        return c
