"""1. 상품 가격 수집 테스트 (토스 매핑/페이지네이션, 쿠팡 파싱/서명/예산, HTTP 재시도)"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest
from conftest import ConnErr, FakeResponse, FakeSession, NOW

from dealbot.coupang_collector import CoupangBudgetExceeded, CoupangPartnersClient, parse_offers, sign
from dealbot.http import HttpClient, HttpStatusError, NetworkError, ResponseFormatError
from dealbot.monitoring import RunStats
from dealbot.normalizer import parse_fee, parse_price
from dealbot.toss_collector import TossConfigError, TossSharelinkClient
from dealbot.toss_collector.mapping import MappingError, map_product

MAPPING = {"items_path": "data.products", "id": "productId", "name": "productName", "brand": "brandName",
           "sale_price": "price", "discount_price": "discountPrice", "shipping_fee": "deliveryFee",
           "free_shipping": "freeShipping", "url": "productUrl", "sold_out": "soldOut"}


def item(**kw):
    base = {"productId": 1, "productName": "상품", "price": "12,900원", "discountPrice": 9900,
            "deliveryFee": "무료", "productUrl": "https://toss.im/p/1", "soldOut": "N"}
    base.update(kw)
    return base


def test_map_product_parses_prices_and_flags():
    p = map_product(item(), MAPPING, NOW)
    assert (p.sale_price, p.discount_price, p.shipping_fee, p.sold_out) == (12900, 9900, 0, False)
    assert p.final_price == 9900


def test_map_product_missing_required_fields_raises():
    with pytest.raises(MappingError):
        map_product(item(productUrl=None), MAPPING, NOW)
    with pytest.raises(MappingError):
        map_product(item(price=None, discountPrice=None), MAPPING, NOW)


def test_unknown_shipping_fee_leaves_final_price_unknown():
    p = map_product(item(deliveryFee=None), MAPPING, NOW)
    assert p.shipping_fee is None and p.final_price is None
    p2 = map_product(item(deliveryFee=None, freeShipping=True), MAPPING, NOW)
    assert p2.final_price == 9900


def test_sold_out_detected():
    assert map_product(item(soldOut=True), MAPPING, NOW).sold_out is True


@pytest.mark.parametrize("raw,expected", [("39,900원", 39900), (0, None), ("", None), (None, None),
                                          ("가격문의", None), (-100, None), (True, None)])
def test_parse_price_never_returns_zero(raw, expected):
    assert parse_price(raw) == expected


def test_parse_fee_free_is_zero():
    assert parse_fee("무료배송") == 0 and parse_fee(3000) == 3000 and parse_fee(None) is None


def _toss_cfg(cfg, **extra):
    over = {"toss": {"base_url": "https://api.example", "token_url": "https://auth.example/token",
                     "sources": [{"name": "best", "path": "/best"}],
                     "pagination": {"page_size": 2, "max_pages": 5}}}
    over["toss"].update(extra)
    return cfg(over, env={"TOSS_ACCESS_KEY": "ak_test", "TOSS_SECRET_KEY": "sk_test"})


def test_toss_client_paginates_and_dedups(cfg):
    c = _toss_cfg(cfg)
    page1 = {"data": {"products": [item(productId=1), item(productId=2)]}}
    page2 = {"data": {"products": [item(productId=2), item(productId=3, productName=None)]}}
    page3 = {"data": {"products": []}}
    session = FakeSession([FakeResponse(payload={"access_token": "tok", "expires_in": 3600}),
                           FakeResponse(payload=page1), FakeResponse(payload=page2), FakeResponse(payload=page3)])
    client = TossSharelinkClient(c, HttpClient(session=session, min_interval=0, sleep=lambda s: None))
    stats = RunStats()
    products = client.collect(stats)
    assert [p.product_id for p in products] == ["1", "2"]
    assert stats.collect_failed == 1  # 이름 누락 상품
    assert session.calls[1][2]["params"] == {"page": 1, "size": 2}
    assert session.calls[1][2]["headers"]["Authorization"] == "Bearer tok"


def test_toss_client_without_keys_is_config_error(cfg):
    c = cfg({"toss": {"base_url": "https://api.example", "sources": [{"name": "b", "path": "/b"}]}})
    with pytest.raises(TossConfigError):
        TossSharelinkClient(c, HttpClient(session=FakeSession([]))).collect(RunStats())


def test_coupang_signature_format():
    auth = sign("secret", "access", "GET", "/v2/x", "keyword=a&limit=1",
                datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc))
    assert auth.startswith("CEA algorithm=HmacSHA256, access-key=access, signed-date=260102T030405Z, signature=")
    assert re.search(r"signature=[0-9a-f]{64}$", auth)


def test_parse_offers_and_error_code():
    payload = {"rCode": "0", "data": {"productData": [
        {"productId": 1, "productName": "a", "productPrice": 1000, "isFreeShipping": True, "productUrl": "u"},
        {"productId": 2, "productName": "b", "productPrice": None, "isFreeShipping": False, "productUrl": "u"}]}}
    offers = parse_offers(payload, "q", NOW)
    assert offers[0].final_price == 1000
    assert offers[1].price is None and offers[1].final_price is None
    with pytest.raises(ResponseFormatError):
        parse_offers({"rCode": "400", "rMessage": "bad"}, "q", NOW)


def test_coupang_shipping_unknown_when_not_free():
    o = parse_offers({"rCode": "0", "data": {"productData": [
        {"productId": 1, "productName": "a", "productPrice": 1000, "isFreeShipping": False}]}}, "q", NOW)[0]
    assert o.final_price is None


def test_coupang_hourly_budget_and_cache(cfg, repo):
    c = cfg({"coupang": {"max_calls_per_hour": 1}},
            env={"COUPANG_ACCESS_KEY": "a", "COUPANG_SECRET_KEY": "s"})
    resp = {"rCode": "0", "data": {"productData": [{"productId": 1, "productName": "a", "productPrice": 100,
                                                    "isFreeShipping": True}]}}
    session = FakeSession([FakeResponse(payload=resp)])
    client = CoupangPartnersClient(c, HttpClient(session=session, min_interval=0), repo)
    assert len(client.search("q1")) == 1
    assert len(client.search("q1")) == 1          # 캐시 - 호출 없음
    with pytest.raises(CoupangBudgetExceeded):
        client.search("q2")                        # 예산 소진
    assert len(session.calls) == 1


def test_http_retries_network_error_then_succeeds():
    slept = []
    session = FakeSession([ConnErr("down"), FakeResponse(503, text="busy"), FakeResponse(payload={"ok": 1})])
    http = HttpClient(session=session, max_retries=3, backoff_base=1, min_interval=0, sleep=slept.append)
    assert http.get_json("https://h/x") == {"ok": 1}
    assert len(slept) == 2 and slept[1] > slept[0]  # 지수 백오프


def test_http_distinguishes_errors_and_limits_retries():
    http = HttpClient(session=FakeSession([FakeResponse(404, text="nf")]), min_interval=0, sleep=lambda s: None)
    with pytest.raises(HttpStatusError) as e:
        http.get_json("https://h/x")
    assert e.value.status == 404 and e.value.kind == "http"

    session = FakeSession([ConnErr("x")] * 3)
    http = HttpClient(session=session, max_retries=2, min_interval=0, sleep=lambda s: None)
    with pytest.raises(NetworkError):
        http.get_json("https://h/x")
    assert len(session.calls) == 3  # 1회 + 재시도 2회, 무한 재시도 없음


def test_http_bad_json_is_format_error():
    http = HttpClient(session=FakeSession([FakeResponse(200, payload=None, text="<html>")]),
                      min_interval=0, sleep=lambda s: None)
    with pytest.raises(ResponseFormatError):
        http.get_json("https://h/x")
