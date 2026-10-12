"""가격 비교와 특가 등급 계산.

할인 차액 = 쿠팡 최종 가격 - 토스쇼핑 최종 가격
절감률(%) = 할인 차액 / 쿠팡 최종 가격 × 100
등급 판정은 반올림 전 정밀값으로 한다.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from .models import Comparison, DealGrade


class PriceComparator:
    def __init__(self, s_rate: float = 30, s_amount: int = 20000,
                 a_rate: float = 20, a_amount: int = 10000):
        self.s_rate = Decimal(str(s_rate))
        self.s_amount = int(s_amount)
        self.a_rate = Decimal(str(a_rate))
        self.a_amount = int(a_amount)

    @classmethod
    def from_config(cls, cfg) -> "PriceComparator":
        g = cfg.get("deal.grades")
        return cls(g["S"]["min_rate"], g["S"]["min_amount"], g["A"]["min_rate"], g["A"]["min_amount"])

    def compare(self, toss_final: int | None, coupang_final: int | None) -> Comparison:
        if toss_final is None:
            return Comparison(toss_final, coupang_final, reason="토스 최종가 미확인")
        if coupang_final is None:
            return Comparison(toss_final, coupang_final, reason="쿠팡 최종가 미확인")
        if toss_final <= 0 or coupang_final <= 0:
            return Comparison(toss_final, coupang_final, reason="비정상 가격(0원 이하)")
        diff = coupang_final - toss_final
        rate = Decimal(diff) * 100 / Decimal(coupang_final)
        return Comparison(toss_final, coupang_final, diff, rate, self.grade(diff, rate), valid=True)

    def compare_unit(self, toss_final: int | None, toss_count: int | None,
                     coupang_final: int | None, coupang_count: int | None) -> Comparison:
        """묶음 수량이 다른 동일 품목의 개당 가격 비교.

        절감률 = (쿠팡 개당가 - 토스 개당가) / 쿠팡 개당가 × 100
        차액   = (쿠팡 개당가 - 토스 개당가) × 토스 수량  (토스 상품 1개 구매 시 아끼는 금액)
        등급(S/A)은 매기지 않는다 - 개당 특가 기준은 DealFilter 에서 적용.
        """
        base = self.compare(toss_final, coupang_final)
        if not base.valid:
            return base
        if not toss_count or not coupang_count or toss_count <= 0 or coupang_count <= 0:
            return Comparison(toss_final, coupang_final, reason="수량 미확인 - 개당 비교 불가")
        tu = Decimal(toss_final) / toss_count
        cu = Decimal(coupang_final) / coupang_count
        rate = (cu - tu) * 100 / cu
        diff = int(((cu - tu) * toss_count).to_integral_value(rounding=ROUND_HALF_UP))
        return Comparison(toss_final, coupang_final, diff, rate, None, valid=True,
                          toss_count=toss_count, coupang_count=coupang_count, toss_unit=tu, coupang_unit=cu)

    def grade(self, diff: int, rate: Decimal) -> DealGrade:
        if rate >= self.s_rate and diff >= self.s_amount:
            return DealGrade.S
        if rate >= self.a_rate and diff >= self.a_amount:
            return DealGrade.A
        return DealGrade.NORMAL


def display_rate(rate: Decimal) -> str:
    return str(rate.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))
