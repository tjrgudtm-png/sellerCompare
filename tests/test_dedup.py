"""5. 중복 게시 방지 테스트"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from dealbot.deduplication import Deduplicator, RepostPolicy, canonical_url, fingerprint
from dealbot.repository import Repository


def reserve(d, *, pid="T1", url="https://toss.im/p/1", grade="A", price=30000, channel="kakao_pc"):
    return d.reserve(channel=channel, product_id=pid, name="상품", option=None, url=url, grade=grade,
                     toss_price=price, coupang_price=50000, message="m")


def test_same_deal_not_posted_twice(repo):
    d = Deduplicator(repo, RepostPolicy())
    pid, _ = reserve(d)
    repo.update_post(pid, "sent", 1)
    assert reserve(d)[0] is None


def test_pending_blocks_concurrent_reservation(repo):
    d = Deduplicator(repo, RepostPolicy())
    assert reserve(d)[0] is not None
    post, reason = reserve(d)
    assert post is None and "pending" in reason


def test_same_url_or_product_id_counts_as_duplicate(repo):
    d = Deduplicator(repo, RepostPolicy())
    pid, _ = reserve(d)
    repo.update_post(pid, "sent", 1)
    assert reserve(d, pid="OTHER", url="https://toss.im/p/1?utm_source=x")[0] is None
    assert reserve(d, url="https://toss.im/p/999")[0] is None


def test_repost_on_grade_upgrade(repo):
    d = Deduplicator(repo, RepostPolicy())
    pid, _ = reserve(d, grade="A")
    repo.update_post(pid, "sent", 1)
    post, reason = reserve(d, grade="S")
    assert post is not None and "등급 상승" in reason


def test_repost_on_large_additional_drop_only(repo):
    d = Deduplicator(repo, RepostPolicy(min_drop_rate=Decimal(5), min_drop_amount=3000))
    pid, _ = reserve(d, price=30000)
    repo.update_post(pid, "sent", 1)
    assert reserve(d, price=29000)[0] is None             # 1,000원 하락: 부족
    post, reason = reserve(d, price=27000)                # 3,000원(10%) 하락
    assert post is not None and "추가 가격 하락" in reason


def test_failed_post_can_be_retried_next_run(repo):
    d = Deduplicator(repo, RepostPolicy())
    pid, _ = reserve(d)
    repo.update_post(pid, "failed", 3, "err")
    assert reserve(d)[0] is not None


def test_history_survives_restart(tmp_path):
    path = tmp_path / "db.sqlite"
    r1 = Repository(path)
    r1.init_schema()
    pid, _ = reserve(Deduplicator(r1, RepostPolicy()))
    r1.update_post(pid, "sent", 1)
    r1.close()
    r2 = Repository(path)
    r2.init_schema()
    assert reserve(Deduplicator(r2, RepostPolicy()))[0] is None
    r2.close()


def test_channels_are_independent(repo):
    d = Deduplicator(repo, RepostPolicy())
    pid, _ = reserve(d, channel="dry_run")
    repo.update_post(pid, "dry_run", 1)
    assert reserve(d, channel="kakao_pc")[0] is not None


def test_stale_pending_becomes_unknown_and_blocks(repo):
    d = Deduplicator(repo, RepostPolicy())
    reserve(d)
    repo.conn.execute("UPDATE posts SET updated_at='2000-01-01T00:00:00+00:00'")
    assert repo.expire_stale_pending(timedelta(minutes=30)) == 1
    assert reserve(d)[0] is None  # 결과 불명은 이중 게시 방지를 위해 게시된 것으로 간주


def test_same_deal_after_hours_option(repo):
    d = Deduplicator(repo, RepostPolicy(same_deal_after=timedelta(hours=1)))
    pid, _ = reserve(d)
    repo.update_post(pid, "sent", 1)
    assert reserve(d)[0] is None
    repo.conn.execute("UPDATE posts SET updated_at='2000-01-01T00:00:00+00:00'")
    assert reserve(d)[0] is not None


def test_fingerprint_stable_and_canonical():
    assert canonical_url("https://Toss.im/p/1/?utm_source=a&b=2") == "https://toss.im/p/1?b=2"
    assert fingerprint("1", "[특가] 상품 A", None, "https://toss.im/p/1") == \
        fingerprint("1", "상품  a", "", "https://toss.im/p/1?utm_campaign=z")
