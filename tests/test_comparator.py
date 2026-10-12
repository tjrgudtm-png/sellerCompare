"""3. 할인율·차액 계산 / 4. S급·A급 분류 테스트"""
from __future__ import annotations

from decimal import Decimal

import pytest

from dealbot.models import DealGrade
from dealbot.price_comparator import PriceComparator, display_rate

C = PriceComparator()


def test_diff_and_rate():
    c = C.compare(39900, 59900)
    assert c.valid and c.diff == 20000
    assert display_rate(c.rate) == "33.4"
    assert c.rate == Decimal(20000) * 100 / Decimal(59900)


@pytest.mark.parametrize("toss,coupang,grade", [
    (70000, 100000, DealGrade.S),       # 30%, 30,000 -> S 경계
    (70001, 100000, DealGrade.A),       # 29.999% -> S 미달, A 충족
    (30000, 50000, DealGrade.S),        # 40%, 20,000 -> S 금액 경계
    (31000, 50000, DealGrade.A),        # 38%, 19,000 -> S 금액 미달
    (80000, 100000, DealGrade.A),       # 20%, 20,000 -> A 비율 경계
    (80001, 100000, DealGrade.NORMAL),  # 19.999%
    (40000, 50000, DealGrade.A),        # 20%, 10,000 -> A 양쪽 경계
    (14000, 20000, DealGrade.NORMAL),   # 30%, 6,000 -> 금액 미달
    (9000, 19000, DealGrade.A),         # 52.6%, 10,000 -> S 금액 미달
    (100000, 90000, DealGrade.NORMAL),  # 토스가 더 비쌈
])
def test_grades(toss, coupang, grade):
    assert C.compare(toss, coupang).grade == grade


def test_thresholds_configurable():
    c = PriceComparator(s_rate=50, s_amount=50000, a_rate=10, a_amount=1000)
    assert c.compare(90000, 100000).grade == DealGrade.A
    assert c.compare(70000, 100000).grade == DealGrade.A


@pytest.mark.parametrize("toss,coupang", [(None, 1000), (1000, None), (0, 1000), (1000, 0)])
def test_invalid_prices_never_compared(toss, coupang):
    c = C.compare(toss, coupang)
    assert not c.valid and c.grade is None and c.diff is None
