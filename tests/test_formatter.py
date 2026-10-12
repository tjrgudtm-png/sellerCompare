"""7. 메시지 포맷 테스트"""
from __future__ import annotations

import pytest
from conftest import offer, toss

from dealbot.deal_filter import DealFilter
from dealbot.message_formatter import MessageFormatError, MessageFormatter
from dealbot.models import DealCandidate
from dealbot.price_comparator import PriceComparator
from dealbot.product_matcher import ProductMatcher


def candidate(t=None, o=None):
    t = t or toss(discount=39900, price=59900)
    o = o or offer(price=59900)
    c = DealCandidate(t, o, ProductMatcher().match(t, o), PriceComparator().compare(t.final_price, o.final_price))
    return DealFilter(allowed_hosts=["toss.im"]).evaluate(c)


def test_message_exact_format():
    # 요구사항 예시 가격(39,900 / 59,900): 33.4%·20,000원 → 규칙상 S급 기준(30%·20,000원)을 충족
    msg = MessageFormatter().format(candidate())
    assert msg.splitlines() == [
        "🚨 [S급 특가] 테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙",
        "💰 토스쇼핑 가격: 39,900원 (배송비 포함)",
        "🛒 쿠팡 가격: 59,900원 (표시가 기준)",
        "💸 가격 차이: 20,000원",
        "📉 쿠팡 대비: 33.4% 저렴",
        "🔗 토스쇼핑 바로가기",
        "https://toss.im/sample/T1",
        "📌 비교 기준: 동일 모델 / 동일 구성",
        "🕒 가격 확인 시각: 2026-10-12 12:00",
    ]


def test_a_grade_title_and_share_url_preferred():
    t = toss(discount=44900, share_url="https://toss.im/s/abc")
    msg = MessageFormatter().format(candidate(t, offer(price=59900)))   # 25%, 15,000원
    assert msg.startswith("🔥 [A급 특가]")
    assert "https://toss.im/s/abc" in msg


def test_long_message_is_compacted_but_keeps_core_info():
    t = toss(name="테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙 " + "아주긴설명 " * 30)
    msg = MessageFormatter(max_length=220, footer="광고 고지 문구").format(candidate(t))
    assert len(msg) <= 220 and "광고 고지 문구" in msg and "📌" not in msg
    for part in ("[S급 특가]", "39,900원", "59,900원", "20,000원", "33.4%", "https://toss.im/sample/T1"):
        assert part in msg


def test_refuses_unverified_candidate():
    c = candidate(o=offer(price=41000))  # 차액 미달 -> 비적격
    assert not c.eligible
    with pytest.raises(MessageFormatError):
        MessageFormatter().format(c)
