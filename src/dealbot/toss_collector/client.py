"""토스쇼핑 상품 수집.

공식 경로: 토스 쉐어링크 Open API (승인제, Access/Secret Key + 호출 서버 IP 등록 필요).
- 베스트 / 카테고리 베스트 / 하루특가 상품 목록, 상품 상세(최신 가격·품절), 쉐어링크 발급 제공.
- 목록·상세 응답의 productUrl 은 추적되지 않는 일반 링크 → 게시용 링크는 발급 API 사용 권장.
URL, 경로, 인증 방식, 필드명은 승인 후 공식 문서로 확인해 config 로 지정한다 (코드에 추측값을 박지 않음).
웹 페이지 스크래핑은 구현하지 않는다.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from ..http import ExternalError, HttpClient, ResponseFormatError, dig
from ..models import TossProduct
from ..monitoring import RunStats, event
from .mapping import MappingError, map_product, merge_detail


class TossConfigError(Exception):
    pass


class TossSource(Protocol):
    def collect(self, stats: RunStats) -> list[TossProduct]: ...

    def issue_share_url(self, product: TossProduct) -> str | None: ...


def _collect_items(payload, mapping: dict, now: datetime, source: str, stats: RunStats,
                   seen: dict[str, TossProduct]) -> int:
    items = dig(payload, mapping.get("items_path")) if mapping.get("items_path") else payload
    if not isinstance(items, list):
        raise ResponseFormatError(f"상품 목록 경로({mapping.get('items_path')})에서 리스트를 찾지 못했습니다")
    for item in items:
        try:
            p = map_product(item, mapping, now, source)
        except MappingError as e:
            stats.collect_failed += 1
            stats.fail("toss_collect", "mapping", str(e), source=source)
            continue
        seen.setdefault(p.product_id, p)  # 같은 상품이 여러 목록에 있어도 1건
    return len(items)


class TossSharelinkClient:
    def __init__(self, cfg, http: HttpClient):
        self.cfg = cfg
        self.http = http
        self.base_url = (cfg.get("toss.base_url") or "").rstrip("/")
        self.mapping = cfg.get("toss.field_mapping")
        self._token: str | None = None
        self._token_exp = 0.0

    def _check_config(self) -> None:
        problems = []
        if not self.base_url:
            problems.append("toss.base_url 미설정")
        if not self.cfg.get("toss.sources"):
            problems.append("toss.sources 미설정")
        if not (self.cfg.secrets.toss_access_key and self.cfg.secrets.toss_secret_key):
            problems.append("TOSS_ACCESS_KEY/TOSS_SECRET_KEY 미설정")
        if problems:
            raise TossConfigError("토스 쉐어링크 API 설정 누락: " + ", ".join(problems))

    def _auth_headers(self) -> dict:
        token_url = self.cfg.get("toss.token_url")
        if not token_url:
            raise TossConfigError("toss.token_url 미설정 (승인 후 공식 문서의 토큰 발급 URL 입력)")
        if self._token is None or time.time() >= self._token_exp:
            data = self.http.post_json(token_url, data={
                "grant_type": "client_credentials",
                "client_id": self.cfg.secrets.toss_access_key,
                "client_secret": self.cfg.secrets.toss_secret_key,
            })
            token = data.get("access_token") if isinstance(data, dict) else None
            if not token:
                raise ResponseFormatError("토큰 응답에 access_token 이 없습니다")
            self._token = token
            self._token_exp = time.time() + max(60, int(data.get("expires_in", 3600)) - 60)
        return {"Authorization": f"Bearer {self._token}"}

    def fetch_raw(self, path: str, params: dict | None = None) -> dict:
        return self.http.get_json(self.base_url + path, params=params or {}, headers=self._auth_headers())

    def collect(self, stats: RunStats) -> list[TossProduct]:
        self._check_config()
        pg = self.cfg.get("toss.pagination")
        seen: dict[str, TossProduct] = {}
        for src in self.cfg.get("toss.sources"):
            name, path = src.get("name", "source"), src["path"]
            params = dict(src.get("params") or {})
            cursor = None
            for page_idx in range(int(pg["max_pages"])):
                q = dict(params)
                if pg["type"] == "page":
                    q[pg["page_param"]] = int(pg["first_page"]) + page_idx
                    q[pg["size_param"]] = pg["page_size"]
                elif pg["type"] == "cursor" and cursor:
                    q[pg["cursor_param"]] = cursor
                try:
                    payload = self.fetch_raw(path, q)
                    count = _collect_items(payload, self.mapping, datetime.now(timezone.utc), name,
                                           stats, seen)
                except ExternalError as e:
                    stats.fail("toss_collect", e.kind, str(e), source=name, page=page_idx)
                    break
                if pg["type"] == "none" or count == 0:
                    break
                if pg["type"] == "page" and count < int(pg["page_size"]):
                    break
                if pg["type"] == "cursor":
                    cursor = dig(payload, pg["next_cursor_path"])
                    if not cursor:
                        break
        products = list(seen.values())
        self._enrich_details(products, stats)
        return products

    def _enrich_details(self, products: list[TossProduct], stats: RunStats) -> None:
        detail = self.cfg.get("toss.detail")
        if not detail.get("enabled"):
            return
        for p in products[: int(detail["max_per_run"])]:
            try:
                payload = self.fetch_raw(detail["path"].format(product_id=p.product_id))
                item = dig(payload, detail.get("item_path")) if detail.get("item_path") else payload
                merge_detail(p, map_product(item, self.mapping, datetime.now(timezone.utc), p.source))
                p.collected_at = datetime.now(timezone.utc)
            except (ExternalError, MappingError) as e:
                # 상세 조회 실패 시 목록 값 유지하되, 최신성 확인 실패로 기록
                stats.fail("toss_detail", getattr(e, "kind", "mapping"), str(e), product_id=p.product_id)

    def issue_share_url(self, product: TossProduct) -> str | None:
        sl = self.cfg.get("toss.share_link")
        if not sl.get("enabled"):
            return None
        body = {sl["id_field"]: product.product_id, **(sl.get("extra_body") or {})}
        payload = self.http.post_json(self.base_url + sl["path"], json=body, headers=self._auth_headers())
        url = dig(payload, sl["short_url_path"])
        return str(url) if url else None


class FixtureTossSource:
    """오프라인 검증용: 저장된 JSON 응답 파일(들)을 동일한 매핑 로직으로 읽는다.

    파일 형식: 실제 API 목록 응답과 같은 구조. `dealbot probe toss` 로 저장한 파일도 사용 가능.
    """

    def __init__(self, cfg, paths: list[Path]):
        self.mapping = cfg.get("toss.field_mapping")
        self.paths = paths

    def collect(self, stats: RunStats) -> list[TossProduct]:
        seen: dict[str, TossProduct] = {}
        for path in self.paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                _collect_items(payload, self.mapping, datetime.now(timezone.utc), path.stem, stats, seen)
            except (OSError, ValueError, ExternalError) as e:
                stats.fail("toss_collect", "fixture", f"{path.name}: {e}")
        event(logging.INFO, "토스 픽스처 로드", files=len(self.paths), products=len(seen))
        return list(seen.values())

    def issue_share_url(self, product: TossProduct) -> str | None:
        return None
