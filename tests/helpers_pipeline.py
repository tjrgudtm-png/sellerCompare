from __future__ import annotations

from dealbot.chat_publisher import PublishError, PublishResult
from dealbot.pipeline import Pipeline


class FakeToss:
    def __init__(self, products=None, error=None):
        self.products = products or []
        self.error = error

    def collect(self, stats):
        if self.error:
            raise self.error
        return list(self.products)

    def issue_share_url(self, product):
        return None


class FakeCoupang:
    def __init__(self, offers=None, error=None):
        self.offers = offers or []
        self.error = error
        self.queries = []

    def search(self, query):
        self.queries.append(query)
        if self.error:
            raise self.error
        return list(self.offers)


class FakePublisher:
    channel = "fake"

    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.sent = []

    def publish(self, message):
        self.sent.append(message)
        if self.outcomes:
            o = self.outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
        return PublishResult("sent")


def make_pipeline(cfg, repo, products, offers=None, publisher=None, coupang_error=None, overrides=None):
    slept = []
    base = {"matching": {"recompare_after_minutes": 0}, "publish": {"retry_backoff_seconds": 1}}
    from dealbot.config import deep_merge
    c = cfg(deep_merge(base, overrides or {}))
    pub = publisher or FakePublisher()
    p = Pipeline(c, repo, FakeToss(products), FakeCoupang(offers, coupang_error), pub, sleep=slept.append)
    return p, pub, slept


__all__ = ["FakeCoupang", "FakePublisher", "FakeToss", "PublishError", "make_pipeline"]
