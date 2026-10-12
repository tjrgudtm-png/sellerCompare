"""옵션 특가 / 개당 특가 테스트 (사용자 예시 1·2)"""
from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import NOW, offer, toss

from dealbot.deal_filter import DealFilter, Threshold
from dealbot.models import DealCandidate, DealType, TossOption
from dealbot.option_deals import OptionDealDetector
from dealbot.price_comparator import PriceComparator
from dealbot.product_matcher import ProductMatcher
from dealbot.toss_collector.mapping import map_options, map_product

DET = OptionDealDetector(min_options=3, exclude_keywords=["추가구성", "사은품", "샘플"])
FILTER = DealFilter(allowed_hosts=["toss.im"], unit=Threshold(True, Decimal(40), 5000),
                    option=Threshold(True, Decimal(40), 5000))


def shirt(options, fee=0, **kw):
    return toss(name="테스트브랜드 오버핏 맨투맨", price=24900, discount=None, fee=fee,
                options=[TossOption(n, p, s) for n, p, s in options], **kw)


BASE = [("블랙 / M", 24900, False), ("블랙 / L", 24900, False), ("화이트 / M", 25900, False),
        ("네이비 / L", 26900, False)]


def test_example1_one_cheap_option_is_deal():
    c = FILTER.evaluate(DET.detect(shirt(BASE + [("그레이 / M", 12900, False)])))
    assert c.eligible and c.deal_type == DealType.OPTION
    assert [o.name for o in c.deal_options] == ["그레이 / M"]
    assert c.comparison.coupang_final == 25400 and c.comparison.diff == 12500


def test_small_option_gap_is_not_deal():
    c = FILTER.evaluate(DET.detect(shirt(BASE + [("그레이 / M", 19900, False)])))   # 21.7%
    assert not c.eligible


def test_rate_ok_but_amount_too_small():
    opts = [("a", 9000, False), ("b", 9000, False), ("c", 9000, False), ("d", 5000, False)]  # 44%, 4,000원
    c = FILTER.evaluate(DET.detect(shirt(opts)))
    assert not c.eligible and any("차액 미달" in r for r in c.reject_reasons)


def test_sold_out_cheap_option_ignored():
    assert DET.detect(shirt(BASE + [("그레이 / M", 9900, True)])).comparison.diff < 5000


def test_bait_option_excluded():
    cand = DET.detect(shirt(BASE + [("추가구성) 양말", 3900, False)]))
    assert cand is None or "양말" not in cand.deal_options[0].name


def test_different_composition_not_compared():
    opts = [("블랙 1개", 9900, False), ("블랙 3개", 25900, False), ("화이트 3개", 25900, False),
            ("그레이 3개", 25900, False)]
    assert DET.detect(shirt(opts)) is None   # 1개 옵션은 3개 묶음과 비교하지 않음


def test_too_few_options():
    assert DET.detect(shirt([("a", 25000, False), ("b", 12000, False)])) is None


def test_unknown_option_price_never_zero():
    c = DET.detect(shirt(BASE + [("그레이 / M", None, False)]))
    assert c is None or all(o.price for o in c.deal_options)


def test_unknown_shipping_blocks_option_deal():
    c = FILTER.evaluate(DET.detect(shirt(BASE + [("그레이 / M", 12900, False)], fee=None)))
    assert not c.eligible


def test_map_options_absolute_and_delta():
    item = {"productId": 1, "productName": "티셔츠", "price": 20000, "productUrl": "https://toss.im/p/1",
            "options": [{"color": "블랙", "size": "M", "price": "19,900원"},
                        {"color": "화이트", "size": "L", "price": "가격문의"}]}
    m = {"id": "productId", "name": "productName", "sale_price": "price", "url": "productUrl",
         "options_path": "options", "option_item_name": ["color", "size"], "option_item_price": "price"}
    p = map_product(item, m, NOW)
    assert [(o.name, o.price) for o in p.options] == [("블랙 / M", 19900), ("화이트 / L", None)]
    delta = {**m, "option_price_mode": "delta"}
    item2 = {**item, "options": [{"color": "블랙", "size": "M", "price": "+3,000"},
                                 {"color": "블랙", "size": "S", "price": -2000}]}
    assert [o.price for o in map_options(item2, delta, 20000)] == [23000, 18000]
    assert map_options({"x": 1}, m, 20000) == []


# ---- 예시 2: 개당 가격 (쿠팡 동일 품목, 묶음 수량만 다름) ----
COLA = "테스트브랜드 제로콜라 355ml x 20개"


def unit_candidate(toss_price, coupang_name, coupang_price):
    t = toss(name=COLA, price=19900, discount=toss_price)
    o = offer(name=coupang_name, price=coupang_price)
    m = ProductMatcher().match(t, o)
    from dealbot.normalizer import extract_attributes
    comp = PriceComparator().compare_unit(t.final_price, 20, o.final_price, extract_attributes(o.name).count)
    return FILTER.evaluate(DealCandidate(t, o, m, comp, deal_type=DealType.UNIT)), m


def test_example2_unit_price_deal():
    c, m = unit_candidate(10000, "테스트브랜드 제로콜라, 355ml, 40개", 39800)
    assert m.pack_only_diff and c.eligible
    assert c.comparison.toss_unit == 500 and c.comparison.diff == 9900


def test_unit_not_cheap_enough():
    c, _ = unit_candidate(14000, "테스트브랜드 제로콜라, 355ml, 40개", 39800)   # 개당 700 vs 995 → 29.6%
    assert not c.eligible


@pytest.mark.parametrize("name", ["테스트브랜드 제로콜라, 500ml, 40개",      # 용량 다름
                                  "다른브랜드 제로콜라, 355ml, 40개",        # 브랜드 다름
                                  "테스트브랜드 제로콜라 355ml 40개 증정 세트"])  # 구성 다름
def test_pack_only_diff_requires_same_item(name):
    assert not ProductMatcher().match(toss(name=COLA), offer(name=name)).pack_only_diff


def test_compare_unit_math_and_invalid():
    c = PriceComparator().compare_unit(10000, 20, 39800, 40)
    assert c.valid and c.coupang_unit == Decimal("995") and round(c.rate, 1) == Decimal("49.7")
    assert not PriceComparator().compare_unit(10000, None, 39800, 40).valid
    assert not PriceComparator().compare_unit(10000, 20, None, 40).valid
