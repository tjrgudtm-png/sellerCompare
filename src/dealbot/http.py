"""외부 API 호출 공통 래퍼.

- 호스트별 최소 요청 간격 + 동시 요청 수 제한
- 네트워크 오류와 HTTP 오류를 구분된 예외로 변환
- 재시도는 네트워크 오류 / 429 / 5xx 에만, 지수형 백오프 + 최대 횟수
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from .monitoring import event


class ExternalError(Exception):
    """외부 서비스 호출 실패의 공통 부모."""

    kind = "external"


class NetworkError(ExternalError):
    kind = "network"


class HttpStatusError(ExternalError):
    kind = "http"

    def __init__(self, status: int, url: str, body: str = ""):
        super().__init__(f"HTTP {status} {url}")
        self.status = status
        self.url = url
        self.body = body[:500]


class ResponseFormatError(ExternalError):
    kind = "format"


RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float = 15,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        backoff_max: float = 60.0,
        min_interval: float = 1.0,
        max_concurrency: int = 2,
        user_agent: str = "dealbot",
        session: Any = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.min_interval = min_interval
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self._sleep = sleep
        self._sem = threading.BoundedSemaphore(max(1, max_concurrency))
        self._lock = threading.Lock()
        self._last_call: dict[str, float] = {}

    @classmethod
    def from_config(cls, cfg, **kw) -> "HttpClient":
        return cls(
            timeout=float(cfg.get("http.timeout_seconds")),
            max_retries=int(cfg.get("http.max_retries")),
            backoff_base=float(cfg.get("http.backoff_base_seconds")),
            backoff_max=float(cfg.get("http.backoff_max_seconds")),
            min_interval=float(cfg.get("http.min_interval_seconds")),
            max_concurrency=int(cfg.get("http.max_concurrency")),
            user_agent=str(cfg.get("http.user_agent")),
            **kw,
        )

    def _throttle(self, host: str) -> None:
        with self._lock:
            last = self._last_call.get(host)
            now = time.monotonic()
            if last is not None:
                wait = self.min_interval - (now - last)
                if wait > 0:
                    self._sleep(wait)
            self._last_call[host] = time.monotonic()

    def backoff(self, attempt: int, retry_after: str | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), self.backoff_max)
            except ValueError:
                pass
        delay = self.backoff_base * (2 ** (attempt - 1))
        return min(delay + random.uniform(0, delay * 0.1), self.backoff_max)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        host = urlsplit(url).netloc
        kwargs.setdefault("timeout", self.timeout)
        attempts = self.max_retries + 1
        last_exc: ExternalError | None = None
        for attempt in range(1, attempts + 1):
            with self._sem:
                self._throttle(host)
                try:
                    resp = self.session.request(method, url, **kwargs)
                except (requests.ConnectionError, requests.Timeout) as e:
                    last_exc = NetworkError(f"{type(e).__name__}: {host}")
                    retry_after = None
                except requests.RequestException as e:
                    raise NetworkError(f"{type(e).__name__}: {host}") from e
                else:
                    if resp.status_code < 400:
                        return resp
                    err = HttpStatusError(resp.status_code, f"{host}{urlsplit(url).path}", resp.text)
                    if resp.status_code not in RETRYABLE_STATUS:
                        raise err
                    last_exc = err
                    retry_after = resp.headers.get("Retry-After")
            if attempt < attempts:
                delay = self.backoff(attempt, retry_after)
                event(logging.INFO, "외부 요청 재시도 대기", host=host, attempt=attempt,
                      error_kind=last_exc.kind, error=str(last_exc), delay=round(delay, 2))
                self._sleep(delay)
        assert last_exc is not None
        raise last_exc

    def get_json(self, url: str, **kwargs) -> Any:
        return _json(self.request("GET", url, **kwargs))

    def post_json(self, url: str, **kwargs) -> Any:
        return _json(self.request("POST", url, **kwargs))


def _json(resp: requests.Response) -> Any:
    try:
        return resp.json()
    except ValueError as e:
        raise ResponseFormatError(f"JSON 파싱 실패: {resp.url}") from e


def dig(data: Any, path: str | None) -> Any:
    """'a.b.0.c' 형식 경로로 중첩 dict/list 값을 꺼낸다. 없으면 None."""
    if not path:
        return None
    node = data
    for part in path.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return None
        if node is None:
            return None
    return node
