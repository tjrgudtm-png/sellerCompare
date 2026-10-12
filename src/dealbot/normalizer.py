"""상품명/옵션 정규화와 비교 속성(브랜드, 모델코드, 용량, 수량, 색상 등) 추출."""

from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import ProductAttributes

_NOISE_BRACKETS = re.compile(
    r"[\[\(【](?:[^\]\)】]*?(?:무료배송|당일|특가|핫딜|단독|쿠폰|할인|공식|정품|최저가|한정|이벤트|타임딜|오늘출발|로켓)[^\]\)】]*)[\]\)】]")
_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^0-9a-z가-힣.+\-x×* ]")

_VOLUME = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|mL|ML|l|L|리터|밀리리터)(?![a-zA-Z])")
_WEIGHT = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|KG|Kg|g|G|그램|킬로그램|mg|MG)(?![a-zA-Z])")
_INCH = re.compile(r"(\d+(?:\.\d+)?)\s*(인치|inch|\"|형)")
_COUNT_UNITS = r"개입|개|입|팩|매|롤|구|병|캔|봉|포|정|캡슐|박스|box|BOX|ea|EA|P|p|세트|켤레|장|족|팩입"
_COUNT = re.compile(rf"(\d+)\s*(?:{_COUNT_UNITS})(?![a-zA-Z가-힣])")
_MULT = re.compile(r"[x×*]\s*(\d+)(?!\s*(?:ml|l|g|kg|mm|cm))", re.IGNORECASE)
_MODEL = re.compile(r"(?<![A-Z0-9])([A-Z]{1,6}-?[A-Z0-9]*\d[A-Z0-9\-]{2,})(?![A-Z0-9])")
_BRAND_PREFIX = re.compile(r"^\s*[\[【]([^\]】]{1,20})[\]】]")

COLORS = {
    "블랙": "black", "black": "black", "검정": "black", "화이트": "white", "white": "white",
    "흰색": "white", "그레이": "gray", "gray": "gray", "grey": "gray", "회색": "gray",
    "실버": "silver", "silver": "silver", "골드": "gold", "gold": "gold", "네이비": "navy",
    "navy": "navy", "블루": "blue", "blue": "blue", "파랑": "blue", "레드": "red", "red": "red",
    "빨강": "red", "핑크": "pink", "pink": "pink", "그린": "green", "green": "green",
    "베이지": "beige", "beige": "beige", "브라운": "brown", "brown": "brown", "퍼플": "purple",
    "purple": "purple", "옐로우": "yellow", "yellow": "yellow", "아이보리": "ivory",
    "ivory": "ivory", "민트": "mint", "mint": "mint", "오렌지": "orange", "orange": "orange",
}
BUNDLE_WORDS = {"세트", "기획", "증정", "사은품", "묶음", "번들", "+", "구성", "리필", "본품", "케이스포함"}

# 수량/용량 단위 토큰은 모델코드로 오인하지 않도록 제외
_UNIT_TOKEN = re.compile(r"^\d+(?:\.\d+)?(?:ML|L|G|KG|MG|EA|P|GB|TB|MM|CM|M|W|V|MAH|HZ)$")


def nfkc(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


def normalize_name(name: str) -> str:
    text = nfkc(name)
    text = _NOISE_BRACKETS.sub(" ", text)
    text = text.lower()
    text = _NON_WORD.sub(" ", text)
    return _SPACES.sub(" ", text).strip()


def tokens(name: str) -> frozenset[str]:
    return frozenset(t for t in re.split(r"[\s+\-x×*]+", normalize_name(name)) if len(t) >= 2)


def parse_price(value: Any) -> int | None:
    """가격 파싱. 파싱 불가/0 이하/비정상 값은 None (0원으로 처리하지 않음)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        num = Decimal(str(value))
    else:
        digits = re.sub(r"[^\d.]", "", str(value))
        if not digits:
            return None
        try:
            num = Decimal(digits)
        except InvalidOperation:
            return None
    if num <= 0 or num > Decimal(1_000_000_000):
        return None
    return int(num.to_integral_value())


def parse_fee(value: Any) -> int | None:
    """배송비 파싱. 0원(무료)은 유효값. 파싱 불가는 None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value)
    if "무료" in text:
        return 0
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_signed_int(value: Any) -> int | None:
    """옵션 추가금처럼 0/음수가 유효한 금액 파싱 ("+3,000원", "-2000", 0). 파싱 불가는 None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return int(value)
    m = re.search(r"([+-]?)\s*([\d,]+)", str(value))
    if not m:
        return None
    num = int(m.group(2).replace(",", ""))
    return -num if m.group(1) == "-" else num


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in {"true", "y", "yes", "1", "soldout", "sold_out", "품절"}:
        return True
    if s in {"false", "n", "no", "0", "onsale", "on_sale", "판매중"}:
        return False
    return None


def _decimal(s: str) -> Decimal:
    return Decimal(s)


def extract_attributes(name: str, option: str | None = None, brand: str | None = None,
                       model_code: str | None = None) -> ProductAttributes:
    text = nfkc(f"{name} {option or ''}")
    lower = text.lower()

    volumes = []
    for num, unit in _VOLUME.findall(text):
        v = _decimal(num)
        volumes.append(v * 1000 if unit.lower() in {"l", "리터"} else v)
    weights = []
    for num, unit in _WEIGHT.findall(text):
        v = _decimal(num)
        u = unit.lower()
        weights.append(v * 1000 if u in {"kg", "킬로그램"} else v / 1000 if u == "mg" else v)
    inches = [_decimal(n) for n, _ in _INCH.findall(text)]

    # "2개입 x 3팩" -> 6, "500ml x 24개" -> 24 (x 뒤 숫자가 단위 수량과 같으면 중복으로 본다)
    unit_counts = [int(c) for c in _COUNT.findall(text)]
    mult_counts = [int(c) for c in _MULT.findall(text) if int(c) not in unit_counts]
    count = None
    if unit_counts or mult_counts:
        count = 1
        for c in unit_counts + mult_counts:
            count *= c

    upper = text.upper()
    codes = set()
    for m in _MODEL.findall(upper):
        code = m.strip("-")
        if _UNIT_TOKEN.match(code) or code.isdigit():
            continue
        codes.add(code.replace("-", ""))
    if model_code:
        codes.add(nfkc(model_code).upper().replace("-", "").replace(" ", ""))

    words = set(re.split(r"[\s/,()\[\]\-+]+", lower))
    colors = frozenset(v for k, v in COLORS.items() if k in words)
    bundle = frozenset(w for w in BUNDLE_WORDS if w in text)

    if not brand:
        m = _BRAND_PREFIX.match(text)
        if m and not _NOISE_BRACKETS.match(m.group(0)):
            brand = m.group(1)

    return ProductAttributes(
        brand=normalize_name(brand) if brand else None,
        model_codes=frozenset(codes),
        volume_ml=max(volumes) if volumes else None,
        weight_g=max(weights) if weights else None,
        count=count,
        size_inch=max(inches) if inches else None,
        colors=colors,
        bundle_keywords=bundle,
        tokens=tokens(f"{name} {option or ''}"),
    )


def search_query(name: str, brand: str | None, attrs: ProductAttributes, max_tokens: int = 6) -> str:
    """쿠팡 검색 키워드: 모델코드가 있으면 브랜드+모델코드, 없으면 정규화 상품명 앞부분."""
    if attrs.model_codes:
        # 검색에는 원래 표기(하이픈 포함)를 우선 사용
        raw = [m.strip("-") for m in _MODEL.findall(nfkc(name).upper())
               if m.strip("-").replace("-", "") in attrs.model_codes]
        code = max(raw or attrs.model_codes, key=len)
        return f"{brand or ''} {code}".strip()
    words = normalize_name(name).split()
    if brand and normalize_name(brand) not in normalize_name(name):
        words.insert(0, brand)
    return " ".join(words[:max_tokens])
