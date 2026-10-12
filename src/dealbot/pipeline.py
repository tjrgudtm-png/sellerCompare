"""한 번의 실행 파이프라인.

토스 수집 → 정규화/저장(가격 이력) → 쿠팡 검색 → 동일상품 판별 → 가격 비교 → 특가 판정
→ 중복 확인(예약) → 메시지 생성 → 게시(재시도) → 결과 기록
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from .chat_publisher import ChatPublisher, PublishError
from .coupang_collector import CoupangBudgetExceeded, CoupangConfigError, CoupangSource
from .deal_filter import DealFilter
from .deduplication import Deduplicator, RepostPolicy
from .http import ExternalError
from .message_formatter import MessageFormatError, MessageFormatter
from .models import CoupangOffer, DealCandidate, DealGrade, MatchLevel, MatchResult, TossProduct
from .monitoring import RunStats, event, write_health
from .normalizer import normalize_name, search_query
from .price_comparator import PriceComparator
from .product_matcher import ProductMatcher, toss_attributes
from .repository import Repository
from .toss_collector import TossConfigError, TossSource

GRADE_ORDER = {DealGrade.S: 0, DealGrade.A: 1, DealGrade.NORMAL: 2}


def toss_discount_rate(p: TossProduct) -> float:
    if p.sale_price and p.item_price is not None and p.sale_price > p.item_price:
        return (p.sale_price - p.item_price) / p.sale_price
    return 0.0


class Pipeline:
    def __init__(self, cfg, repo: Repository, toss: TossSource, coupang: CoupangSource,
                 publisher: ChatPublisher, *, sleep: Callable[[float], None] = time.sleep):
        self.cfg = cfg
        self.repo = repo
        self.toss = toss
        self.coupang = coupang
        self.publisher = publisher
        self.matcher = ProductMatcher.from_config(cfg)
        self.comparator = PriceComparator.from_config(cfg)
        self.filter = DealFilter.from_config(cfg)
        self.policy = RepostPolicy.from_config(cfg)
        self.dedup = Deduplicator(repo, self.policy)
        self.formatter = MessageFormatter.from_config(cfg)
        self.sleep = sleep

    # ------------------------------------------------------------------
    def run_once(self) -> RunStats:
        stats = RunStats(started_at=datetime.now(timezone.utc).isoformat())
        stats.run_id = self.repo.start_run()
        event(logging.INFO, "실행 시작", run_id=stats.run_id, channel=self.publisher.channel)
        try:
            expired = self.repo.expire_stale_pending(self.policy.pending_timeout)
            if expired:
                stats.fail("publish", "stale_pending", f"결과 불명 게시 {expired}건을 unknown 처리")
            products = self._collect(stats)
            if products is None:
                stats.status = "failed"
            else:
                deals = self._compare(products, stats)
                self._publish(deals, stats)
                stats.status = "partial" if stats.failures else "success"
        except Exception as e:  # 실행 단위로 격리 - 스케줄러는 계속 돈다
            stats.status = "failed"
            stats.fail("pipeline", "unexpected", f"{type(e).__name__}: {e}")
            logging.getLogger("dealbot").exception("파이프라인 예외")
        finally:
            stats.finished_at = datetime.now(timezone.utc).isoformat()
            self.repo.finish_run(stats.run_id, stats.status, stats.summary(), stats.failures)
            write_health(self.cfg.path("app.health_file"), stats, float(self.cfg.get("app.interval_minutes")))
            event(logging.INFO, "실행 종료", **stats.summary())
        return stats

    # ------------------------------------------------------------------
    def _collect(self, stats: RunStats) -> list[TossProduct] | None:
        try:
            products = self.toss.collect(stats)
        except TossConfigError as e:
            stats.fail("toss_collect", "config", str(e))
            return None
        except ExternalError as e:
            stats.fail("toss_collect", e.kind, str(e))
            return None
        if not products and stats.collect_failed:
            return None
        products = products[: int(self.cfg.get("app.max_products_per_run"))]
        stats.collected = len(products)
        for p in products:
            if self.repo.upsert_toss_product(p, normalize_name(p.name)):
                prev = self.repo.previous_final_price(p.product_id)
                if prev is not None and p.final_price is not None and prev != p.final_price:
                    event(logging.INFO, "토스 가격 변동", product_id=p.product_id,
                          previous=prev, current=p.final_price)
            if not p.share_url:
                p.share_url = self.repo.get_share_url(p.product_id)
        return products

    def _compare(self, products: list[TossProduct], stats: RunStats) -> list[DealCandidate]:
        recompare = timedelta(minutes=float(self.cfg.get("matching.recompare_after_minutes")))
        targets = []
        for p in products:
            if p.sold_out is True:
                stats.compare_skipped += 1
                continue
            if p.final_price is None:
                # 실제 구매가를 모르면 비교 자체가 무의미 - 쿠팡 호출 예산을 쓰지 않는다
                stats.compare_skipped += 1
                self.repo.save_comparison(stats.run_id, p, None, None, None, False, ["토스 최종가 미확인"])
                continue
            if not self.repo.compare_due(p.product_id, p.final_price, recompare):
                stats.compare_skipped += 1
                continue
            targets.append(p)
        targets.sort(key=toss_discount_rate, reverse=True)

        deals: list[DealCandidate] = []
        for i, p in enumerate(targets):
            query = search_query(p.name, p.brand, toss_attributes(p))
            try:
                offers = self.coupang.search(query)
            except CoupangBudgetExceeded:
                stats.compare_skipped += len(targets) - i
                event(logging.INFO, "쿠팡 호출 예산 소진 - 다음 실행으로 연기", remaining=len(targets) - i)
                break
            except CoupangConfigError as e:
                stats.fail("coupang_search", "config", str(e))
                stats.compare_skipped += len(targets) - i
                break
            except ExternalError as e:
                stats.fail("coupang_search", e.kind, str(e), product_id=p.product_id, query=query)
                continue
            self.repo.mark_compared(p.product_id, p.final_price)
            cand = self._evaluate(p, offers)
            stats.compared += 1
            level = cand.match.level if cand.match else MatchLevel.LOW
            setattr(stats, f"match_{level.value.lower()}", getattr(stats, f"match_{level.value.lower()}") + 1)
            self.repo.save_comparison(stats.run_id, p, cand.offer, cand.match, cand.comparison,
                                      cand.eligible, cand.reject_reasons)
            if cand.eligible:
                deals.append(cand)
            elif cand.offer and cand.match and self._needs_review(cand):
                self.repo.add_review(p, cand.offer, cand.match)
        stats.deals_found = len(deals)
        return deals

    def _evaluate(self, p: TossProduct, offers: list[CoupangOffer]) -> DealCandidate:
        scored: list[tuple[CoupangOffer, MatchResult]] = [(o, self.matcher.match(p, o)) for o in offers]
        high = [(o, m) for o, m in scored if m.level == MatchLevel.HIGH]
        medium = [(o, m) for o, m in scored if m.level == MatchLevel.MEDIUM]
        if high:
            # 동일 상품 중 쿠팡 최저 최종가와 비교 (절감액을 과대평가하지 않도록 보수적으로)
            priced = [x for x in high if x[0].final_price is not None]
            offer, match = min(priced or high, key=lambda x: x[0].final_price or x[0].price or 0)
        elif medium:
            offer, match = max(medium, key=lambda x: x[1].score)
        elif scored:
            offer, match = max(scored, key=lambda x: x[1].score)
        else:
            cand = DealCandidate(p, None, None, None)
            return self.filter.evaluate(cand)
        comp = self.comparator.compare(p.final_price, offer.final_price)
        return self.filter.evaluate(DealCandidate(p, offer, match, comp))

    @staticmethod
    def _needs_review(c: DealCandidate) -> bool:
        if c.match.level == MatchLevel.MEDIUM:
            return True
        # 동일 상품이지만 쿠팡 최종가(배송비)를 확정하지 못한 경우
        return c.match.level == MatchLevel.HIGH and c.offer.final_price is None

    # ------------------------------------------------------------------
    def _publish(self, deals: list[DealCandidate], stats: RunStats) -> None:
        deals.sort(key=lambda c: (GRADE_ORDER[c.comparison.grade], -c.comparison.diff))
        limit = int(self.cfg.get("publish.max_posts_per_run"))
        published = 0
        for c in deals:
            if published >= limit:
                event(logging.INFO, "실행당 게시 상한 도달", limit=limit)
                break
            self._ensure_share_url(c, stats)
            self.filter.evaluate(c)  # 링크 교체 후 재검증
            if not c.eligible:
                stats.fail("deal_filter", "revalidate", "; ".join(c.reject_reasons),
                           product_id=c.toss.product_id)
                continue
            try:
                message = self.formatter.format(c)
            except MessageFormatError as e:
                stats.fail("message", "format", str(e), product_id=c.toss.product_id)
                continue
            post_id, reason = self.dedup.reserve(
                channel=self.publisher.channel, product_id=c.toss.product_id, name=c.toss.name,
                option=c.toss.option_text, url=c.toss.url, grade=c.comparison.grade.value,
                toss_price=c.comparison.toss_final, coupang_price=c.comparison.coupang_final,
                message=message)
            if post_id is None:
                stats.dedup_skipped += 1
                event(logging.INFO, "중복 게시 건너뜀", product_id=c.toss.product_id, reason=reason)
                continue
            event(logging.INFO, "게시 시도", product_id=c.toss.product_id, reason=reason, post_id=post_id)
            if self._send_with_retry(post_id, message, stats, c.toss.product_id):
                published += 1

    def _ensure_share_url(self, c: DealCandidate, stats: RunStats) -> None:
        if c.toss.share_url:
            return
        try:
            url = self.toss.issue_share_url(c.toss)
        except (ExternalError, TossConfigError) as e:
            stats.fail("toss_share_link", getattr(e, "kind", "config"), str(e), product_id=c.toss.product_id)
            return
        if url:
            c.toss.share_url = url
            self.repo.set_share_url(c.toss.product_id, url)

    def _send_with_retry(self, post_id: int, message: str, stats: RunStats, product_id: str) -> bool:
        max_attempts = int(self.cfg.get("publish.max_attempts"))
        base = float(self.cfg.get("publish.retry_backoff_seconds"))
        last_error = ""
        for attempt in range(1, max_attempts + 1):
            try:
                result = self.publisher.publish(message)
            except PublishError as e:
                last_error = str(e)
                if not e.retryable or attempt == max_attempts:
                    break
                self.sleep(base * (2 ** (attempt - 1)))
                continue
            except Exception as e:  # 알 수 없는 오류는 이중 게시 위험이 있어 재시도하지 않는다
                last_error = f"{type(e).__name__}: {e}"
                break
            self.repo.update_post(post_id, result.status, attempt)
            stats.posted += 1
            event(logging.INFO, "게시 성공", post_id=post_id, status=result.status, detail=result.detail)
            return True
        self.repo.update_post(post_id, "failed", attempt, last_error)
        stats.post_failed += 1
        stats.fail("publish", self.publisher.channel, last_error, product_id=product_id, attempts=attempt)
        return False
