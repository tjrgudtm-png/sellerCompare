"""파이프라인 단계 간에 주고받는 도메인 모델.

가격은 모두 '원' 단위 정수이며, 확인하지 못한 가격은 반드시 ``None`` 으로 둔다.
0원은 '가격 미확인'의 의미로 절대 사용하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum


class MatchLevel(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class DealGrade(str, Enum):
    S = "S"
    A = "A"
    NORMAL = "NORMAL"


class DealType(str, Enum):
    COUPANG = "coupang"   # 쿠팡 동일 구성 상품과 비교 (S/A 등급)
    UNIT = "unit"         # 쿠팡 동일 품목, 묶음 수량만 다름 → 개당 가격 비교
    OPTION = "option"     # 같은 토스 상품 안에서 특정 옵션만 크게 저렴


@dataclass
class ProductAttributes:
    """상품명/옵션에서 추출한 비교용 속성. 추출하지 못한 값은 None/빈 집합."""

    brand: str | None = None
    model_codes: frozenset[str] = frozenset()
    # 기준 단위로 환산된 용량/중량: {"ml": 500} / {"g": 1000}
    volume_ml: Decimal | None = None
    weight_g: Decimal | None = None
    count: int | None = None
    size_inch: Decimal | None = None
    colors: frozenset[str] = frozenset()
    bundle_keywords: frozenset[str] = frozenset()
    tokens: frozenset[str] = frozenset()


@dataclass
class TossOption:
    name: str
    price: int | None          # 옵션 선택 시 상품가 (추가금 방식이면 기본가+추가금으로 환산된 값)
    sold_out: bool | None = None


@dataclass
class TossProduct:
    product_id: str
    name: str
    url: str
    collected_at: datetime
    brand: str | None = None
    model_code: str | None = None
    option_text: str | None = None
    sale_price: int | None = None        # 정가/판매가
    discount_price: int | None = None    # 할인 적용가
    shipping_fee: int | None = None      # None = 배송비 미확인
    sold_out: bool | None = None         # None = 품절 여부 미확인
    share_url: str | None = None         # 쉐어링크 발급 URL (게시용)
    image_url: str | None = None
    source: str | None = None            # 수집 출처 (best, daily_deal ...)
    options: list[TossOption] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def item_price(self) -> int | None:
        """배송비 제외 실제 결제 상품가."""
        candidates = [p for p in (self.discount_price, self.sale_price) if p is not None]
        return min(candidates) if candidates else None

    @property
    def final_price(self) -> int | None:
        """배송비 포함 최종 구매가. 하나라도 확인 불가면 None."""
        if self.item_price is None or self.shipping_fee is None:
            return None
        return self.item_price + self.shipping_fee

    @property
    def post_url(self) -> str:
        return self.share_url or self.url


@dataclass
class CoupangOffer:
    coupang_product_id: str
    name: str
    url: str
    price: int | None                    # 쿠팡 표시 판매가 (쿠폰/카드/와우 할인 미반영)
    is_rocket: bool | None
    is_free_shipping: bool | None
    fetched_at: datetime
    query: str = ""
    image_url: str | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def shipping_fee(self) -> int | None:
        # 파트너스 API는 배송비 금액을 주지 않는다. 무료배송이 명시된 경우만 0으로 확정.
        return 0 if self.is_free_shipping is True else None

    @property
    def final_price(self) -> int | None:
        if self.price is None or self.shipping_fee is None:
            return None
        return self.price + self.shipping_fee


@dataclass
class MatchResult:
    level: MatchLevel
    reasons: list[str]
    basis: list[str] = field(default_factory=list)  # 메시지용 비교 기준 (예: "동일 모델")
    score: float = 0.0
    # 수량만 다르고 나머지(브랜드·모델/개당 용량·구성)는 같은 품목 → 개당 가격 비교 가능
    pack_only_diff: bool = False


@dataclass
class Comparison:
    toss_final: int | None
    coupang_final: int | None
    diff: int | None = None
    rate: Decimal | None = None          # 절감률(%), 반올림 전 정밀값
    grade: DealGrade | None = None
    valid: bool = False
    reason: str | None = None
    # 개당 비교일 때만 채움
    toss_count: int | None = None
    coupang_count: int | None = None
    toss_unit: Decimal | None = None
    coupang_unit: Decimal | None = None


@dataclass
class DealCandidate:
    toss: TossProduct
    offer: CoupangOffer | None
    match: MatchResult | None
    comparison: Comparison | None
    eligible: bool = False
    reject_reasons: list[str] = field(default_factory=list)
    deal_type: DealType = DealType.COUPANG
    # 옵션 특가일 때: 특가 옵션들(같은 최저가)과 비교에 쓴 다른 옵션 수
    deal_options: list[TossOption] = field(default_factory=list)
    compared_option_count: int = 0

    @property
    def grade_label(self) -> str:
        if self.deal_type == DealType.COUPANG and self.comparison and self.comparison.grade:
            return self.comparison.grade.value
        return self.deal_type.value.upper()
