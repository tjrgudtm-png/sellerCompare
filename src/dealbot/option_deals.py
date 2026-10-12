"""옵션 특가 탐지: 같은 토스 상품 안에서 특정 옵션만 다른 옵션보다 크게 싼 경우.

예) 맨투맨 옵션 대부분 2만원대인데 '그레이 / M' 만 1만원대 → 옵션 특가.

판정 규칙 (쿠팡 호출 없음)
- 판매 중이고 가격이 확인된 옵션만 사용 (가격 미확인 옵션은 0원으로 보지 않고 제외).
- 미끼 옵션 제외: 옵션명에 '추가구성/단품/샘플/사은품…' 키워드가 있으면 비교하지 않는다.
- 구성이 다른 옵션끼리는 비교하지 않는다: 옵션명에서 추출한 수량·용량·중량이 같은 옵션끼리만 비교
  (예: '1개' vs '3개 묶음' 은 다른 구성). 색상/사이즈만 다른 옵션끼리 비교된다.
- 같은 구성 옵션이 min_options 개 이상일 때, 최저가 옵션을 나머지 옵션 가격의 '중앙값'과 비교한다.
  (평균은 이상치 하나에 흔들리므로 판정은 중앙값으로 한다)
- 특가 여부(40%·5,000원 등)는 DealFilter 에서 판정한다.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from statistics import median

from .models import Comparison, DealCandidate, DealType, TossOption, TossProduct
from .normalizer import extract_attributes


class OptionDealDetector:
    def __init__(self, min_options: int = 3, exclude_keywords: list[str] | None = None):
        self.min_options = max(2, int(min_options))
        self.exclude_keywords = [k for k in (exclude_keywords or []) if k]

    @classmethod
    def from_config(cls, cfg) -> "OptionDealDetector":
        o = cfg.get("deal.option_outlier")
        return cls(o["min_options"], o["exclude_keywords"])

    def is_bait(self, option: TossOption) -> bool:
        name = option.name.replace(" ", "")
        return any(k.replace(" ", "") in name for k in self.exclude_keywords)

    @staticmethod
    def composition_key(option: TossOption) -> tuple:
        a = extract_attributes(option.name)
        return (a.count, a.volume_ml, a.weight_g)

    def detect(self, product: TossProduct) -> DealCandidate | None:
        usable = [o for o in product.options
                  if o.price is not None and o.sold_out is not True and not self.is_bait(o)]
        groups: dict[tuple, list[TossOption]] = defaultdict(list)
        for o in usable:
            groups[self.composition_key(o)].append(o)

        best: DealCandidate | None = None
        for group in groups.values():
            if len(group) < self.min_options:
                continue
            low = min(o.price for o in group)
            cheap = [o for o in group if o.price == low]
            others = [o.price for o in group if o.price != low]
            if len(others) < self.min_options - 1:
                continue  # 비교 기준이 될 '다른 가격' 옵션이 부족
            ref = int(median(others))
            fee = product.shipping_fee
            toss_final = low + fee if fee is not None else None
            ref_final = ref + fee if fee is not None else None
            diff = ref - low
            rate = Decimal(diff) * 100 / Decimal(ref)
            comp = Comparison(toss_final, ref_final, diff, rate, None,
                              valid=toss_final is not None,
                              reason=None if toss_final is not None else "토스 배송비 미확인")
            cand = DealCandidate(product, None, None, comp, deal_type=DealType.OPTION,
                                 deal_options=cheap, compared_option_count=len(others))
            if best is None or rate > best.comparison.rate:
                best = cand
        return best
