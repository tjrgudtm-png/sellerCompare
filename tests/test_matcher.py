"""2. 동일 상품 및 다른 옵션 판별 테스트"""
from __future__ import annotations

import pytest
from conftest import offer, toss

from dealbot.models import MatchLevel
from dealbot.product_matcher import ProductMatcher

M = ProductMatcher()


def level(t, o):
    return M.match(t, o).level


def test_same_model_code_is_high():
    r = M.match(toss(), offer())
    assert r.level == MatchLevel.HIGH and "동일 모델" in r.basis


def test_different_model_code_is_low():
    assert level(toss(), offer(name="테스트브랜드 무선 이어폰 SAMPLE-EB200 블랙")) == MatchLevel.LOW


def test_different_color_is_low():
    assert level(toss(), offer(name="테스트브랜드 무선 이어폰 SAMPLE-EB100 화이트")) == MatchLevel.LOW


def test_same_volume_and_count_is_high():
    t = toss(name="테스트브랜드 생수 500ml x 20개")
    assert level(t, offer(name="테스트브랜드 생수, 500ml, 20개")) == MatchLevel.HIGH


@pytest.mark.parametrize("coupang_name", ["테스트브랜드 생수, 500ml, 40개", "테스트브랜드 생수, 2L, 20개"])
def test_different_count_or_volume_is_low(coupang_name):
    assert level(toss(name="테스트브랜드 생수 500ml x 20개"), offer(name=coupang_name)) == MatchLevel.LOW


def test_quantity_only_on_one_side_is_medium():
    assert level(toss(name="테스트브랜드 섬유유연제 2.5L"),
                 offer(name="테스트브랜드 섬유유연제 2.5L, 2개")) == MatchLevel.MEDIUM


def test_similar_name_alone_is_not_high():
    # 브랜드/모델/용량 근거 없이 이름만 비슷
    assert level(toss(name="프리미엄 무선 이어폰", brand=None),
                 offer(name="프리미엄 무선 이어폰 블루투스")) == MatchLevel.MEDIUM


def test_brand_mismatch_is_not_high():
    assert level(toss(name="다른브랜드 무선 이어폰 SAMPLE-EB100 블랙", brand="다른브랜드"),
                 offer()) != MatchLevel.HIGH


def test_bundle_composition_difference_is_not_high():
    assert level(toss(), offer(name="테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙 케이스 증정 세트")) != MatchLevel.HIGH


def test_unrelated_product_is_low():
    assert level(toss(), offer(name="전혀 다른 주방 세제 리필")) == MatchLevel.LOW


def test_explicit_quantity_requirement_configurable():
    t = toss(name="테스트브랜드 섬유유연제 2.5L")
    o = offer(name="테스트브랜드 섬유유연제 2.5L")
    assert ProductMatcher(require_explicit_quantity=True).match(t, o).level == MatchLevel.MEDIUM
    assert ProductMatcher(require_explicit_quantity=False).match(t, o).level == MatchLevel.HIGH
