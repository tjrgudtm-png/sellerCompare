from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from dealbot.config import load_config
from dealbot.models import CoupangOffer, TossProduct
from dealbot.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 12, 3, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg(tmp_path):
    def make(overrides: dict | None = None, env: dict | None = None):
        base = {"app": {"db_path": str(tmp_path / "db.sqlite"), "log_dir": str(tmp_path / "logs"),
                        "health_file": str(tmp_path / "health.json"),
                        "lock_file": str(tmp_path / "run.lock")},
                "http": {"min_interval_seconds": 0}}
        from dealbot.config import deep_merge
        return load_config(None, deep_merge(base, overrides or {}), env=env or {})
    return make


@pytest.fixture
def repo(tmp_path):
    r = Repository(tmp_path / "test.sqlite")
    r.init_schema()
    yield r
    r.close()


def toss(pid="T1", name="테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙", price=59900, discount=39900, fee=0,
         sold_out=False, brand="테스트브랜드", url=None, **kw) -> TossProduct:
    return TossProduct(product_id=pid, name=name, url=url or f"https://toss.im/sample/{pid}",
                       collected_at=NOW, brand=brand, sale_price=price, discount_price=discount,
                       shipping_fee=fee, sold_out=sold_out, **kw)


def offer(name="테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙", price=69900, free=True, pid="C1") -> CoupangOffer:
    return CoupangOffer(coupang_product_id=pid, name=name, url=f"https://link.coupang.com/{pid}", price=price,
                        is_rocket=True, is_free_shipping=free, fetched_at=NOW)


class FakeResponse:
    def __init__(self, status=200, payload=None, text=None, headers=None, url="https://x"):
        self.status_code = status
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload or {})
        self.headers = headers or {}
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """requests.Session 대체. responses 항목은 FakeResponse 또는 예외 인스턴스."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def no_sleep():
    slept = []
    return slept


ConnErr = requests.ConnectionError
