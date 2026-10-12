"""쿠팡 가격 수집 - 쿠팡 파트너스 Open API 상품 검색.

GET /v2/providers/affiliate_open_api/apis/openapi/products/search?keyword=...&limit=...
인증: Authorization: CEA algorithm=HmacSHA256, access-key=..., signed-date=yyMMddTHHmmssZ, signature=hex
      signature = HMAC-SHA256(secret, signed-date + METHOD + path + query)

제약 (코드로 반영):
- 파트너스 가입·승인 필요, 검색 API 호출 한도가 매우 작다 → 시간당 예산(DB 영속) + 결과 캐시.
- 응답에는 옵션 구성, 배송비 금액, 와우/카드/쿠폰 할인 구분이 없다.
  → productPrice 는 '표시가'로만 사용, isFreeShipping=true 인 경우만 최종가 확정.
쿠팡 웹 페이지 크롤링은 구현하지 않는다 (robots.txt·봇 차단 우회 금지).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode

from ..http import HttpClient, ResponseFormatError
from ..models import CoupangOffer
from ..monitoring import event
from ..normalizer import parse_bool, parse_price
from ..repository import Repository


class CoupangConfigError(Exception):
    pass


class CoupangBudgetExceeded(Exception):
    """시간당 호출 예산 소진 - 오류가 아니라 이번 실행에서 비교를 미루는 신호."""


class CoupangSource(Protocol):
    def search(self, query: str) -> list[CoupangOffer]: ...


def sign(secret_key: str, access_key: str, method: str, path: str, query: str,
         now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    signed_date = now.strftime("%y%m%dT%H%M%SZ")
    message = signed_date + method.upper() + path + query
    signature = hmac.new(secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).hexdigest()
    return (f"CEA algorithm=HmacSHA256, access-key={access_key}, "
            f"signed-date={signed_date}, signature={signature}")


def parse_offers(payload: dict, query: str, fetched_at: datetime) -> list[CoupangOffer]:
    if not isinstance(payload, dict):
        raise ResponseFormatError("쿠팡 응답이 객체가 아닙니다")
    if str(payload.get("rCode", "0")) != "0":
        raise ResponseFormatError(f"쿠팡 API 오류 rCode={payload.get('rCode')} {payload.get('rMessage', '')}")
    data = payload.get("data") or {}
    items = data.get("productData") if isinstance(data, dict) else None
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ResponseFormatError("productData 가 리스트가 아닙니다")
    offers = []
    for it in items:
        if not isinstance(it, dict) or not it.get("productId") or not it.get("productName"):
            continue
        offers.append(CoupangOffer(
            coupang_product_id=str(it["productId"]),
            name=str(it["productName"]),
            url=str(it.get("productUrl") or ""),
            price=parse_price(it.get("productPrice")),
            is_rocket=parse_bool(it.get("isRocket")),
            is_free_shipping=parse_bool(it.get("isFreeShipping")),
            fetched_at=fetched_at,
            query=query,
            image_url=it.get("productImage"),
            raw=it,
        ))
    return offers


class CoupangPartnersClient:
    SERVICE = "coupang_search"

    def __init__(self, cfg, http: HttpClient, repo: Repository):
        self.cfg = cfg
        self.http = http
        self.repo = repo

    def _check(self) -> None:
        s = self.cfg.secrets
        if not (s.coupang_access_key and s.coupang_secret_key):
            raise CoupangConfigError("COUPANG_ACCESS_KEY/COUPANG_SECRET_KEY 미설정")

    def fetch_raw(self, query: str) -> dict:
        self._check()
        path = self.cfg.get("coupang.search_path")
        params = {"keyword": query, "limit": int(self.cfg.get("coupang.search_limit"))}
        if self.cfg.get("coupang.sub_id"):
            params["subId"] = self.cfg.get("coupang.sub_id")
        qs = urlencode(params)
        auth = sign(self.cfg.secrets.coupang_secret_key, self.cfg.secrets.coupang_access_key, "GET", path, qs)
        url = self.cfg.get("coupang.base_url").rstrip("/") + path + "?" + qs
        return self.http.get_json(url, headers={"Authorization": auth,
                                                "Content-Type": "application/json;charset=UTF-8"})

    def search(self, query: str) -> list[CoupangOffer]:
        ttl = timedelta(minutes=float(self.cfg.get("coupang.cache_ttl_minutes")))
        cached = self.repo.get_search_cache(query, ttl)
        if cached is not None:
            return [_offer_from_cache(d) for d in cached]
        self._check()
        if not self.repo.try_consume_api_call(self.SERVICE, int(self.cfg.get("coupang.max_calls_per_hour"))):
            raise CoupangBudgetExceeded("쿠팡 검색 API 시간당 호출 예산 소진")
        payload = self.fetch_raw(query)
        offers = parse_offers(payload, query, datetime.now(timezone.utc))
        self.repo.put_search_cache(query, [_offer_to_cache(o) for o in offers])
        event(logging.INFO, "쿠팡 검색", query=query, results=len(offers))
        return offers


def _offer_to_cache(o: CoupangOffer) -> dict:
    return {"coupang_product_id": o.coupang_product_id, "name": o.name, "url": o.url, "price": o.price,
            "is_rocket": o.is_rocket, "is_free_shipping": o.is_free_shipping,
            "fetched_at": o.fetched_at.isoformat(), "query": o.query, "image_url": o.image_url}


def _offer_from_cache(d: dict) -> CoupangOffer:
    return CoupangOffer(**{**d, "fetched_at": datetime.fromisoformat(d["fetched_at"])})


class FixtureCoupangSource:
    """오프라인 검증용: {"검색어": <쿠팡 검색 API 응답 JSON>} 형식 파일."""

    def __init__(self, path: Path):
        self.responses: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def search(self, query: str) -> list[CoupangOffer]:
        if query not in self.responses:
            return []
        return parse_offers(self.responses[query], query, datetime.now(timezone.utc))


__all__ = ["CoupangBudgetExceeded", "CoupangConfigError", "CoupangPartnersClient", "CoupangSource",
           "FixtureCoupangSource", "parse_offers", "sign"]
