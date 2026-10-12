"""중복 게시 방지.

식별: 토스 상품ID, 정규화 상품명+옵션, 정규화 URL 중 하나라도 같으면 같은 상품으로 본다.
재게시 허용:
  - 특가 등급 상승 (S > A)
  - 토스 가격이 이전 게시 가격 대비 추가로 크게 하락 (금액·비율 모두 기준 이상)
  - (선택) 같은 특가라도 설정 시간이 지난 경우
게시 진행 중(pending) 레코드가 있으면 항상 건너뛴다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import DealGrade
from .normalizer import normalize_name
from .repository import Repository, from_iso, utcnow

GRADE_RANK = {DealGrade.NORMAL.value: 0, DealGrade.A.value: 1, DealGrade.S.value: 2}
_TRACKING_PARAMS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "ref", "from"}


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = sorted((k, v) for k, v in parse_qsl(parts.query) if k.lower() not in _TRACKING_PARAMS)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"),
                       urlencode(query), ""))


def fingerprint(product_id: str, name: str, option: str | None, url: str) -> str:
    key = "|".join([product_id, normalize_name(name), normalize_name(option or ""), canonical_url(url)])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


@dataclass
class RepostPolicy:
    on_grade_upgrade: bool = True
    min_drop_rate: Decimal = Decimal("5")
    min_drop_amount: int = 3000
    same_deal_after: timedelta | None = None
    pending_timeout: timedelta = timedelta(minutes=30)

    @classmethod
    def from_config(cls, cfg) -> "RepostPolicy":
        hours = cfg.get("repost.same_deal_after_hours")
        return cls(
            on_grade_upgrade=bool(cfg.get("repost.on_grade_upgrade")),
            min_drop_rate=Decimal(str(cfg.get("repost.min_additional_drop_rate"))),
            min_drop_amount=int(cfg.get("repost.min_additional_drop_amount")),
            same_deal_after=timedelta(hours=float(hours)) if hours else None,
            pending_timeout=timedelta(minutes=float(cfg.get("repost.pending_timeout_minutes"))),
        )

    def decide(self, last, pending, grade: str, toss_price: int) -> tuple[bool, str]:
        if pending is not None:
            return False, "게시 진행 중(pending)"
        if last is None:
            return True, "신규 특가"
        if self.on_grade_upgrade and GRADE_RANK.get(grade, 0) > GRADE_RANK.get(last["grade"], 0):
            return True, f"등급 상승 {last['grade']}→{grade}"
        prev = int(last["toss_price"])
        drop = prev - toss_price
        if drop > 0:
            rate = Decimal(drop) * 100 / Decimal(prev)
            if drop >= self.min_drop_amount and rate >= self.min_drop_rate:
                return True, f"추가 가격 하락 {drop:,}원({rate:.1f}%)"
        if self.same_deal_after is not None:
            if utcnow() - from_iso(last["updated_at"]) >= self.same_deal_after:
                return True, "재게시 대기시간 경과"
        return False, "이미 게시한 특가"


class Deduplicator:
    def __init__(self, repo: Repository, policy: RepostPolicy):
        self.repo = repo
        self.policy = policy

    def reserve(self, *, channel: str, product_id: str, name: str, option: str | None, url: str,
                grade: str, toss_price: int, coupang_price: int, message: str) -> tuple[int | None, str]:
        """중복 검사 + 게시 예약을 원자적으로 수행. (post_id 또는 None, 사유) 반환."""
        fp = fingerprint(product_id, name, option, url)
        reason_box: list[str] = []

        def decide(last, pending):
            ok, reason = self.policy.decide(last, pending, grade, toss_price)
            reason_box.append(reason)
            return ok, reason

        post_id = self.repo.reserve_post(channel, fp, product_id, canonical_url(url), grade,
                                         toss_price, coupang_price, message, decide)
        return post_id, reason_box[0] if reason_box else ""
