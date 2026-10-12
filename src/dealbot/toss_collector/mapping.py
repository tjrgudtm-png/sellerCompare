"""토스 쉐어링크 API 응답 → TossProduct 변환.

정확한 응답 필드명은 API 승인 후 공식 문서/실제 응답(`dealbot probe toss`)으로 확인해
config.yaml 의 toss.field_mapping 에 반영한다. 필수 필드가 없으면 추측하지 않고 실패 처리한다.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ..http import dig
from ..models import TossProduct
from ..normalizer import parse_bool, parse_fee, parse_price


class MappingError(ValueError):
    pass


def _get(item: dict, mapping: dict, key: str) -> Any:
    path = mapping.get(key)
    return dig(item, path) if path else None


def map_product(item: dict, mapping: dict, collected_at: datetime, source: str | None = None) -> TossProduct:
    if not isinstance(item, dict):
        raise MappingError("상품 항목이 객체가 아닙니다")
    pid = _get(item, mapping, "id")
    name = _get(item, mapping, "name")
    url = _get(item, mapping, "url")
    missing = [k for k, v in (("id", pid), ("name", name), ("url", url)) if v in (None, "")]
    if missing:
        raise MappingError(f"필수 필드 누락: {', '.join(missing)}")

    sale = parse_price(_get(item, mapping, "sale_price"))
    discount = parse_price(_get(item, mapping, "discount_price"))
    if sale is None and discount is None:
        raise MappingError("가격 필드 없음/파싱 불가")

    fee = parse_fee(_get(item, mapping, "shipping_fee"))
    if fee is None and parse_bool(_get(item, mapping, "free_shipping")) is True:
        fee = 0

    return TossProduct(
        product_id=str(pid),
        name=str(name).strip(),
        url=str(url).strip(),
        collected_at=collected_at,
        brand=(str(b).strip() or None) if (b := _get(item, mapping, "brand")) else None,
        model_code=(str(m).strip() or None) if (m := _get(item, mapping, "model_code")) else None,
        option_text=(str(o).strip() or None) if (o := _get(item, mapping, "option")) else None,
        sale_price=sale,
        discount_price=discount,
        shipping_fee=fee,
        sold_out=parse_bool(_get(item, mapping, "sold_out")),
        image_url=_get(item, mapping, "image"),
        source=source,
        raw=item,
    )


def merge_detail(base: TossProduct, detail: TossProduct) -> TossProduct:
    """상세 조회 결과(최신 가격·품절)를 목록 결과 위에 덮어쓴다. 상세에 없는 값은 유지."""
    for field in ("sale_price", "discount_price", "shipping_fee", "sold_out", "brand",
                  "model_code", "option_text", "image_url"):
        value = getattr(detail, field)
        if value is not None:
            setattr(base, field, value)
    return base
