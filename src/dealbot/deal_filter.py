"""자동 게시 대상 선별. 모든 조건을 만족해야 eligible (AND).

유형별 기준
- coupang: 동일상품 HIGH, 절감률·차액 최소 기준, S/A 등급
- unit   : 쿠팡 동일 품목(수량만 다름) 개당 가격 기준 (기본 40%·5,000원)
- option : 같은 상품 다른 옵션 대비 (기본 40%·5,000원)
공통: 토스 가격·배송비 확인, 판매 중, 토스 링크 유효
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlsplit

from .models import DealCandidate, DealGrade, DealType, MatchLevel


def is_valid_toss_url(url: str | None, allowed_hosts: list[str]) -> bool:
    if not url:
        return False
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.netloc:
        return False
    host = parts.hostname or ""
    return any(host == h or host.endswith("." + h) for h in allowed_hosts)


@dataclass
class Threshold:
    enabled: bool = True
    min_rate: Decimal = Decimal(40)
    min_amount: int = 5000

    @classmethod
    def from_dict(cls, d: dict) -> "Threshold":
        return cls(bool(d.get("enabled", True)), Decimal(str(d["min_rate"])), int(d["min_amount"]))


class DealFilter:
    def __init__(self, min_rate: float = 20, min_amount: int = 10000,
                 publish_grades: list[str] | None = None, allowed_hosts: list[str] | None = None,
                 unit: Threshold | None = None, option: Threshold | None = None):
        self.min_rate = Decimal(str(min_rate))
        self.min_amount = int(min_amount)
        self.publish_grades = {DealGrade(g) for g in (publish_grades or ["S", "A"])}
        self.allowed_hosts = allowed_hosts or []
        self.unit = unit or Threshold()
        self.option = option or Threshold()

    @classmethod
    def from_config(cls, cfg) -> "DealFilter":
        return cls(cfg.get("deal.min_saving_rate"), cfg.get("deal.min_saving_amount"),
                   cfg.get("deal.publish_grades"), cfg.get("toss.allowed_url_hosts"),
                   Threshold.from_dict(cfg.get("deal.unit_price")),
                   Threshold.from_dict(cfg.get("deal.option_outlier")))

    def evaluate(self, c: DealCandidate) -> DealCandidate:
        r: list[str] = []
        t = c.toss
        if c.deal_type == DealType.OPTION:
            self._check_option(c, r)
        else:
            self._check_coupang_side(c, r)
            if t.item_price is None:
                r.append("토스 판매가 미확인")
        if t.shipping_fee is None:
            r.append("토스 배송비 미확인")
        if t.sold_out is not False:
            r.append("품절" if t.sold_out else "판매 가능 여부 미확인")
        if not is_valid_toss_url(t.post_url, self.allowed_hosts):
            r.append("토스 상품 링크 유효하지 않음")

        comp = c.comparison
        if comp is None or not comp.valid:
            r.append(f"가격 비교 불가: {comp.reason if comp else '비교 없음'}")
        elif c.deal_type == DealType.COUPANG:
            self._check_amounts(comp, self.min_rate, self.min_amount, r)
            if comp.grade not in self.publish_grades:
                r.append(f"게시 대상 등급 아님({comp.grade.value})")
        else:
            rule = self.unit if c.deal_type == DealType.UNIT else self.option
            if not rule.enabled:
                r.append(f"{c.deal_type.value} 특가 비활성화")
            self._check_amounts(comp, rule.min_rate, rule.min_amount, r)
        c.reject_reasons = r
        c.eligible = not r
        return c

    @staticmethod
    def _check_amounts(comp, min_rate: Decimal, min_amount: int, r: list[str]) -> None:
        if comp.rate < min_rate:
            r.append(f"절감률 미달({comp.rate:.1f}% < {min_rate}%)")
        if comp.diff < min_amount:
            r.append(f"차액 미달({comp.diff:,}원 < {min_amount:,}원)")

    @staticmethod
    def _check_coupang_side(c: DealCandidate, r: list[str]) -> None:
        if c.offer is None or c.match is None:
            r.append("쿠팡 비교 대상 없음")
        elif c.deal_type == DealType.UNIT:
            if not c.match.pack_only_diff:
                r.append("개당 비교 불가(동일 품목 미확인)")
        elif c.match.level != MatchLevel.HIGH:
            r.append(f"동일상품 신뢰도 {c.match.level.value}")
        if c.offer is not None and c.offer.final_price is None:
            r.append("쿠팡 최종가 미확인(배송비/가격)")

    @staticmethod
    def _check_option(c: DealCandidate, r: list[str]) -> None:
        if not c.deal_options:
            r.append("특가 옵션 없음")
        elif any(o.sold_out is True or o.price is None for o in c.deal_options):
            r.append("특가 옵션 품절/가격 미확인")
