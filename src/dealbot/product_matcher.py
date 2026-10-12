"""토스쇼핑 상품과 쿠팡 검색 결과의 동일 상품 여부 판정.

원칙: 상품명이 비슷하다는 이유만으로 HIGH 를 주지 않는다.
HIGH 조건
  - 충돌(브랜드/모델코드/용량/중량/수량/색상/인치 불일치)이 하나도 없고
  - 토큰 유사도가 기준 이상이며
  - 브랜드가 확인되고 (토스 브랜드가 쿠팡 상품명에 포함)
  - (a) 모델코드가 양쪽에서 일치하거나
    (b) 용량/중량이 양쪽 모두 명시되어 일치하고, 수량도 양쪽 모두 명시되어 일치
충돌이 있으면 LOW, 그 외(정보 부족)는 MEDIUM.
"""

from __future__ import annotations

from typing import Callable

from .models import CoupangOffer, MatchLevel, MatchResult, ProductAttributes, TossProduct
from .normalizer import extract_attributes, normalize_name

ImageSimilarity = Callable[[str, str], float | None]


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """짧은 쪽 토큰이 긴 쪽에 얼마나 포함되는지 (쿠팡 상품명은 옵션이 붙어 길어지는 경향)."""
    if not a or not b:
        return 0.0
    small, large = (a, b) if len(a) <= len(b) else (b, a)
    return len(small & large) / len(small)


def toss_attributes(p: TossProduct) -> ProductAttributes:
    return extract_attributes(p.name, p.option_text, p.brand, p.model_code)


def _both(a, b) -> bool:
    return a is not None and b is not None


class ProductMatcher:
    def __init__(self, min_similarity_high: float = 0.5, min_similarity_low: float = 0.25,
                 require_explicit_quantity: bool = True,
                 image_similarity: ImageSimilarity | None = None,
                 min_image_similarity: float = 0.85):
        self.min_high = min_similarity_high
        self.min_low = min_similarity_low
        self.require_explicit_quantity = require_explicit_quantity
        self.image_similarity = image_similarity
        self.min_image_similarity = min_image_similarity

    @classmethod
    def from_config(cls, cfg) -> "ProductMatcher":
        return cls(
            min_similarity_high=float(cfg.get("matching.min_token_similarity_high")),
            min_similarity_low=float(cfg.get("matching.min_token_similarity_low")),
            require_explicit_quantity=bool(cfg.get("matching.require_explicit_quantity")),
        )

    def match(self, toss: TossProduct, offer: CoupangOffer) -> MatchResult:
        ta = toss_attributes(toss)
        ca = extract_attributes(offer.name)
        reasons: list[str] = []
        conflicts: list[str] = []
        basis: list[str] = []

        sim = max(jaccard(ta.tokens, ca.tokens), containment(ta.tokens, ca.tokens))
        reasons.append(f"토큰유사도={sim:.2f}")

        # 브랜드
        brand_ok = False
        if ta.brand:
            coupang_norm = normalize_name(offer.name).replace(" ", "")
            if ta.brand.replace(" ", "") in coupang_norm:
                brand_ok = True
                reasons.append("브랜드 일치")
            elif ca.brand and ca.brand != ta.brand:
                conflicts.append(f"브랜드 불일치({ta.brand}≠{ca.brand})")
            else:
                reasons.append("쿠팡 상품명에서 브랜드 미확인")
        else:
            reasons.append("토스 브랜드 정보 없음")

        # 모델코드
        model_ok = False
        if ta.model_codes and ca.model_codes:
            if ta.model_codes & ca.model_codes:
                model_ok = True
                reasons.append("모델코드 일치")
                basis.append("동일 모델")
            else:
                conflicts.append(f"모델코드 불일치({sorted(ta.model_codes)}≠{sorted(ca.model_codes)})")
        elif ta.model_codes or ca.model_codes:
            reasons.append("모델코드 한쪽만 존재")

        # 용량/중량/인치
        spec_ok = None  # None=비교 불가, True=일치, False=불일치
        for label, tv, cv in (("용량", ta.volume_ml, ca.volume_ml), ("중량", ta.weight_g, ca.weight_g),
                              ("크기", ta.size_inch, ca.size_inch)):
            if _both(tv, cv):
                if tv == cv:
                    spec_ok = True if spec_ok is not False else False
                    reasons.append(f"{label} 일치({tv})")
                else:
                    spec_ok = False
                    conflicts.append(f"{label} 불일치({tv}≠{cv})")
            elif tv is not None or cv is not None:
                reasons.append(f"{label} 한쪽만 명시")

        # 수량
        qty_ok = None
        if _both(ta.count, ca.count):
            if ta.count == ca.count:
                qty_ok = True
                reasons.append(f"수량 일치({ta.count})")
            else:
                qty_ok = False
                conflicts.append(f"수량 불일치({ta.count}≠{ca.count})")
        elif ta.count is not None or ca.count is not None:
            reasons.append("수량 한쪽만 명시")
        else:
            reasons.append("수량 양쪽 모두 미표기")

        # 색상
        if ta.colors and ca.colors and not (ta.colors & ca.colors):
            conflicts.append(f"색상 불일치({sorted(ta.colors)}≠{sorted(ca.colors)})")
        elif ta.colors and not ca.colors:
            reasons.append("쿠팡 색상 미확인")

        # 묶음/구성
        if ta.bundle_keywords != ca.bundle_keywords:
            reasons.append(f"구성 키워드 차이({sorted(ta.bundle_keywords)} vs {sorted(ca.bundle_keywords)})")

        # 이미지 유사도 (선택 기능)
        if self.image_similarity and toss.image_url and offer.image_url:
            img = self.image_similarity(toss.image_url, offer.image_url)
            if img is not None:
                reasons.append(f"이미지유사도={img:.2f}")
                if img < self.min_image_similarity / 2:
                    conflicts.append("이미지 상이")

        if conflicts:
            return MatchResult(MatchLevel.LOW, conflicts + reasons, basis, sim)
        if sim < self.min_low:
            return MatchResult(MatchLevel.LOW, ["상품명 유사도 낮음"] + reasons, basis, sim)

        bundle_same = ta.bundle_keywords == ca.bundle_keywords
        if spec_ok and qty_ok:
            spec_qty_ok = True
        elif spec_ok and qty_ok is None and ta.count is None and ca.count is None:
            spec_qty_ok = not self.require_explicit_quantity
        else:
            spec_qty_ok = False

        qty_one_sided = (ta.count is None) != (ca.count is None)
        if (sim >= self.min_high and brand_ok and bundle_same and not qty_one_sided
                and (model_ok or spec_qty_ok)
                and not (ta.colors and not ca.colors)):
            if spec_qty_ok:
                basis.append("동일 용량/수량")
            basis.append("동일 구성")
            return MatchResult(MatchLevel.HIGH, reasons, basis, sim)
        return MatchResult(MatchLevel.MEDIUM, reasons, basis, sim)
