"""6. 가격 수집 실패 시 오판 방지 테스트 - 확인 못한 가격으로 특가를 만들지 않는다"""
from __future__ import annotations

from conftest import offer, toss
from helpers_pipeline import FakeToss, make_pipeline

from dealbot.coupang_collector import CoupangBudgetExceeded
from dealbot.http import NetworkError
from dealbot.pipeline import Pipeline


def test_unknown_toss_shipping_not_posted_and_no_coupang_call(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss(fee=None)], [offer()])
    stats = p.run_once()
    assert stats.posted == 0 and stats.deals_found == 0
    assert p.coupang.queries == []  # 호출 예산 낭비 없음


def test_unknown_coupang_shipping_not_posted_goes_to_review(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss()], [offer(free=False)])
    stats = p.run_once()
    assert stats.posted == 0 and pub.sent == []
    assert len(repo.list_reviews()) == 1


def test_missing_coupang_price_not_treated_as_zero(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss()], [offer(price=None)])
    assert p.run_once().posted == 0


def test_coupang_network_error_produces_no_deal(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss()], coupang_error=NetworkError("down"))
    stats = p.run_once()
    assert stats.posted == 0 and stats.status == "partial"
    assert any(f["kind"] == "network" for f in stats.failures)


def test_budget_exceeded_defers_without_failure(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss()], coupang_error=CoupangBudgetExceeded("x"))
    stats = p.run_once()
    assert stats.posted == 0 and stats.compare_skipped == 1 and not stats.failures


def test_sold_out_not_posted(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss(sold_out=True)], [offer()])
    assert p.run_once().posted == 0


def test_unknown_sold_out_state_not_posted(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss(sold_out=None)], [offer()])
    assert p.run_once().posted == 0


def test_invalid_toss_url_not_posted(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss(url="https://evil.example.com/p/1")], [offer()])
    assert p.run_once().posted == 0


def test_medium_match_not_posted_even_if_cheap(cfg, repo):
    p, pub, _ = make_pipeline(cfg, repo, [toss(name="테스트브랜드 무선 이어폰 블랙")],
                              [offer(name="테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙", price=999000)])
    stats = p.run_once()
    assert stats.posted == 0 and stats.match_medium == 1


def test_toss_collection_failure_marks_run_failed_but_does_not_raise(cfg, repo):
    c = cfg()
    p = Pipeline(c, repo, FakeToss(error=NetworkError("down")), None, None)

    class _P:
        channel = "fake"
    p.publisher = _P()
    stats = p.run_once()
    assert stats.status == "failed" and stats.posted == 0
