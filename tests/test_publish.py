"""8. 채팅 게시 실패 및 재시도 테스트 + 게시 채널별 동작"""
from __future__ import annotations

import pytest
from conftest import FakeResponse, FakeSession, offer, toss
from helpers_pipeline import FakePublisher, make_pipeline

from dealbot.chat_publisher import (ApprovalQueuePublisher, DryRunPublisher, KakaoPcPublisher, PublishError,
                                    TelegramPublisher, build_publisher)
from dealbot.http import HttpClient


def test_retryable_failure_then_success(cfg, repo):
    pub = FakePublisher([PublishError("일시 오류"), PublishError("일시 오류")])
    p, _, slept = make_pipeline(cfg, repo, [toss()], [offer()], publisher=pub)
    stats = p.run_once()
    assert stats.posted == 1 and len(pub.sent) == 3
    assert slept == [1.0, 2.0]                      # 지수 백오프
    assert repo.recent_posts()[0]["status"] == "sent" and repo.recent_posts()[0]["attempts"] == 3


def test_gives_up_after_max_attempts(cfg, repo):
    pub = FakePublisher([PublishError("x")] * 5)
    p, _, _ = make_pipeline(cfg, repo, [toss()], [offer()], publisher=pub,
                            overrides={"publish": {"max_attempts": 3}})
    stats = p.run_once()
    assert stats.posted == 0 and stats.post_failed == 1 and len(pub.sent) == 3
    row = repo.recent_posts()[0]
    assert row["status"] == "failed" and row["error"] == "x"


def test_non_retryable_failure_stops_immediately(cfg, repo):
    pub = FakePublisher([PublishError("설정 오류", retryable=False)])
    p, _, _ = make_pipeline(cfg, repo, [toss()], [offer()], publisher=pub)
    p.run_once()
    assert len(pub.sent) == 1


def test_unexpected_exception_not_retried(cfg, repo):
    pub = FakePublisher([RuntimeError("boom")])
    p, _, _ = make_pipeline(cfg, repo, [toss()], [offer()], publisher=pub)
    stats = p.run_once()
    assert len(pub.sent) == 1 and stats.post_failed == 1


def test_failed_post_retried_next_run_and_success_not_repeated(cfg, repo):
    pub = FakePublisher([PublishError("x", retryable=False)])
    p, _, _ = make_pipeline(cfg, repo, [toss()], [offer()], publisher=pub)
    p.run_once()
    assert p.run_once().posted == 1
    assert p.run_once().posted == 0 and p.run_once().dedup_skipped == 1


def test_max_posts_per_run(cfg, repo):
    products = [toss(pid=f"T{i}", url=f"https://toss.im/sample/T{i}") for i in range(4)]
    p, pub, _ = make_pipeline(cfg, repo, products, [offer()], overrides={"publish": {"max_posts_per_run": 2}})
    assert p.run_once().posted == 2


def test_publish_disabled_forces_dry_run(cfg):
    c = cfg({"publish": {"enabled": False, "mode": "kakao_pc"}})
    assert isinstance(build_publisher(c), DryRunPublisher)


def test_approval_queue_writes_file(tmp_path):
    res = ApprovalQueuePublisher(tmp_path / "out").publish("hello")
    assert res.status == "queued" and (tmp_path / "out").glob("*.txt")
    assert list((tmp_path / "out").glob("*.txt"))[0].read_text(encoding="utf-8") == "hello"


def test_telegram_publisher_and_errors():
    ok = FakeSession([FakeResponse(payload={"ok": True, "result": {"message_id": 7}})])
    pub = TelegramPublisher(HttpClient(session=ok, min_interval=0), "123:abc", "@ch")
    assert pub.publish("hi").status == "sent"
    bad = FakeSession([FakeResponse(400, text="bad")])
    with pytest.raises(PublishError) as e:
        TelegramPublisher(HttpClient(session=bad, min_interval=0), "123:abc", "@ch").publish("hi")
    assert not e.value.retryable
    with pytest.raises(PublishError):
        TelegramPublisher(None, None, None)


class FakeWin:
    def __init__(self, hwnd=1, edit=2, existing=""):
        self.hwnd, self.edit, self.text = hwnd, edit, existing
        self.entered = False
        self.focused = False

    def find_window(self, title):
        return self.hwnd

    def find_edit(self, hwnd):
        return self.edit

    def get_text_length(self, edit):
        return len(self.text)

    def set_text(self, edit, text):
        self.text = text

    def press_enter(self, edit):
        self.entered = True
        self.text = ""

    def focus(self, hwnd):
        self.focused = True


def test_kakao_assist_mode_fills_but_does_not_send():
    win = FakeWin()
    res = KakaoPcPublisher("특가방", api=win).publish("msg")
    assert res.status == "handed_off" and win.text == "msg" and not win.entered and win.focused


def test_kakao_auto_requires_risk_acceptance():
    with pytest.raises(PublishError):
        KakaoPcPublisher("특가방", send_mode="auto", accept_risk=False, api=FakeWin())
    win = FakeWin()
    res = KakaoPcPublisher("특가방", send_mode="auto", accept_risk=True, api=win, sleep=lambda s: None).publish("m")
    assert res.status == "sent" and win.entered


def test_kakao_missing_window_or_pending_text():
    with pytest.raises(PublishError) as e:
        KakaoPcPublisher("특가방", api=FakeWin(hwnd=0)).publish("m")
    assert not e.value.retryable
    with pytest.raises(PublishError):
        KakaoPcPublisher("특가방", api=FakeWin(existing="아직 안보낸 메시지")).publish("m")


def test_kakao_hourly_cap_and_gap():
    with pytest.raises(PublishError):
        KakaoPcPublisher("방", max_posts_per_hour=2, recent_post_count=lambda since: 2, api=FakeWin()).publish("m")
    slept = []
    win = FakeWin()
    pub = KakaoPcPublisher("방", min_seconds_between_posts=30, api=win, sleep=slept.append)
    pub.publish("a")
    win.text = ""
    pub.publish("b")
    assert slept and slept[0] > 25
