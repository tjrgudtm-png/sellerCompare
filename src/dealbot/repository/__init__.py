"""SQLite 저장소: 상품, 가격 이력, 비교 결과, 검토 큐, 게시 이력, 실행 기록."""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from ..models import Comparison, CoupangOffer, MatchResult, TossProduct

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    stats_json TEXT,
    failures_json TEXT
);

CREATE TABLE IF NOT EXISTS toss_products (
    product_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    brand TEXT,
    model_code TEXT,
    option_text TEXT,
    url TEXT NOT NULL,
    share_url TEXT,
    image_url TEXT,
    sold_out INTEGER,
    source TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_compared_at TEXT,
    last_compared_price INTEGER
);

CREATE TABLE IF NOT EXISTS toss_price_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL REFERENCES toss_products(product_id),
    sale_price INTEGER,
    discount_price INTEGER,
    shipping_fee INTEGER,
    final_price INTEGER,
    sold_out INTEGER,
    collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_price_hist_product ON toss_price_history(product_id, id);

CREATE TABLE IF NOT EXISTS coupang_search_cache (
    query TEXT PRIMARY KEY,
    fetched_at TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    service TEXT NOT NULL,
    called_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_api_calls ON api_calls(service, called_at);

CREATE TABLE IF NOT EXISTS comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    product_id TEXT NOT NULL,
    coupang_product_id TEXT,
    coupang_name TEXT,
    coupang_url TEXT,
    match_level TEXT,
    match_reasons TEXT,
    toss_final INTEGER,
    coupang_final INTEGER,
    diff INTEGER,
    rate TEXT,
    grade TEXT,
    eligible INTEGER NOT NULL,
    reject_reasons TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_comparisons_product ON comparisons(product_id, created_at);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id TEXT NOT NULL,
    coupang_product_id TEXT NOT NULL,
    toss_final INTEGER,
    coupang_final INTEGER,
    match_level TEXT NOT NULL,
    reasons TEXT,
    toss_name TEXT,
    coupang_name TEXT,
    toss_url TEXT,
    coupang_url TEXT,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    UNIQUE(product_id, coupang_product_id, toss_final, coupang_final)
);

CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    product_id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    channel TEXT NOT NULL,
    grade TEXT NOT NULL,
    toss_price INTEGER NOT NULL,
    coupang_price INTEGER NOT NULL,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_posts_lookup ON posts(channel, product_id);
CREATE INDEX IF NOT EXISTS ix_posts_fp ON posts(channel, fingerprint);
CREATE INDEX IF NOT EXISTS ix_posts_url ON posts(channel, canonical_url);
CREATE UNIQUE INDEX IF NOT EXISTS ux_posts_one_pending ON posts(channel, fingerprint) WHERE status = 'pending';
"""

# 게시 완료(또는 게시되었을 수 있음)로 간주하는 상태 (중복 판단 대상)
# unknown: 전송 도중 비정상 종료되어 결과를 모르는 경우 -> 이중 게시 방지를 위해 게시된 것으로 본다
POSTED_STATUSES = ("sent", "handed_off", "queued", "dry_run", "unknown")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).isoformat() if dt else None


def from_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


class Repository:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                                    check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._lock = threading.RLock()

    def init_schema(self) -> None:
        # executescript 는 자체적으로 COMMIT 하므로 트랜잭션 밖에서 실행 (모두 IF NOT EXISTS)
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)",
                          (str(SCHEMA_VERSION),))

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # ---------------- runs ----------------
    def start_run(self) -> int:
        with self.tx() as c:
            cur = c.execute("INSERT INTO runs(started_at, status) VALUES(?, 'running')", (iso(utcnow()),))
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, stats: dict, failures: list[dict]) -> None:
        with self.tx() as c:
            c.execute("UPDATE runs SET finished_at=?, status=?, stats_json=?, failures_json=? WHERE id=?",
                      (iso(utcnow()), status, json.dumps(stats, ensure_ascii=False),
                       json.dumps(failures, ensure_ascii=False), run_id))

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    # ---------------- toss products ----------------
    def upsert_toss_product(self, p: TossProduct, norm_name: str) -> bool:
        """상품 upsert + 가격이 바뀐 경우에만 가격 이력 추가. 가격 변동 여부 반환."""
        now = iso(p.collected_at)
        sold = None if p.sold_out is None else int(p.sold_out)
        with self.tx() as c:
            c.execute(
                """INSERT INTO toss_products(product_id, name, norm_name, brand, model_code, option_text,
                       url, share_url, image_url, sold_out, source, first_seen, last_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(product_id) DO UPDATE SET
                       name=excluded.name, norm_name=excluded.norm_name, brand=excluded.brand,
                       model_code=excluded.model_code, option_text=excluded.option_text,
                       url=excluded.url, share_url=COALESCE(excluded.share_url, toss_products.share_url),
                       image_url=excluded.image_url, sold_out=excluded.sold_out,
                       source=excluded.source, last_seen=excluded.last_seen""",
                (p.product_id, p.name, norm_name, p.brand, p.model_code, p.option_text, p.url,
                 p.share_url, p.image_url, sold, p.source, now, now))
            last = c.execute(
                "SELECT sale_price, discount_price, shipping_fee, sold_out FROM toss_price_history "
                "WHERE product_id=? ORDER BY id DESC LIMIT 1", (p.product_id,)).fetchone()
            current = (p.sale_price, p.discount_price, p.shipping_fee, sold)
            changed = last is None or tuple(last) != current
            if changed:
                c.execute(
                    "INSERT INTO toss_price_history(product_id, sale_price, discount_price, shipping_fee,"
                    " final_price, sold_out, collected_at) VALUES(?,?,?,?,?,?,?)",
                    (p.product_id, *current[:3], p.final_price, sold, now))
            return changed

    def price_history(self, product_id: str) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM toss_price_history WHERE product_id=? ORDER BY id",
                                 (product_id,)).fetchall()

    def previous_final_price(self, product_id: str) -> int | None:
        rows = self.conn.execute(
            "SELECT final_price FROM toss_price_history WHERE product_id=? ORDER BY id DESC LIMIT 2",
            (product_id,)).fetchall()
        return rows[1]["final_price"] if len(rows) > 1 else None

    def get_share_url(self, product_id: str) -> str | None:
        row = self.conn.execute("SELECT share_url FROM toss_products WHERE product_id=?",
                                (product_id,)).fetchone()
        return row["share_url"] if row else None

    def set_share_url(self, product_id: str, url: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE toss_products SET share_url=? WHERE product_id=?", (url, product_id))

    def compare_due(self, product_id: str, final_price: int | None, after: timedelta) -> bool:
        row = self.conn.execute(
            "SELECT last_compared_at, last_compared_price FROM toss_products WHERE product_id=?",
            (product_id,)).fetchone()
        if row is None or row["last_compared_at"] is None:
            return True
        if row["last_compared_price"] != final_price:
            return True
        return utcnow() - from_iso(row["last_compared_at"]) >= after

    def mark_compared(self, product_id: str, final_price: int | None) -> None:
        with self.tx() as c:
            c.execute("UPDATE toss_products SET last_compared_at=?, last_compared_price=? WHERE product_id=?",
                      (iso(utcnow()), final_price, product_id))

    # ---------------- coupang cache / api budget ----------------
    def get_search_cache(self, query: str, ttl: timedelta) -> list[dict] | None:
        row = self.conn.execute("SELECT fetched_at, payload_json FROM coupang_search_cache WHERE query=?",
                                (query,)).fetchone()
        if row and utcnow() - from_iso(row["fetched_at"]) < ttl:
            return json.loads(row["payload_json"])
        return None

    def put_search_cache(self, query: str, items: list[dict]) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO coupang_search_cache(query, fetched_at, payload_json) VALUES(?,?,?) "
                      "ON CONFLICT(query) DO UPDATE SET fetched_at=excluded.fetched_at, "
                      "payload_json=excluded.payload_json",
                      (query, iso(utcnow()), json.dumps(items, ensure_ascii=False)))

    def try_consume_api_call(self, service: str, per_hour: int) -> bool:
        """시간당 호출 예산 확인 후 1회 차감. 재시작해도 유지된다."""
        since = iso(utcnow() - timedelta(hours=1))
        with self.tx(immediate=True) as c:
            used = c.execute("SELECT COUNT(*) FROM api_calls WHERE service=? AND called_at>=?",
                             (service, since)).fetchone()[0]
            if used >= per_hour:
                return False
            c.execute("INSERT INTO api_calls(service, called_at) VALUES(?,?)", (service, iso(utcnow())))
            c.execute("DELETE FROM api_calls WHERE called_at < ?", (iso(utcnow() - timedelta(days=2)),))
            return True

    # ---------------- comparisons / review ----------------
    def save_comparison(self, run_id: int | None, toss: TossProduct, offer: CoupangOffer | None,
                        match: MatchResult | None, comp: Comparison | None, eligible: bool,
                        reject_reasons: list[str]) -> None:
        with self.tx() as c:
            c.execute(
                """INSERT INTO comparisons(run_id, product_id, coupang_product_id, coupang_name, coupang_url,
                     match_level, match_reasons, toss_final, coupang_final, diff, rate, grade, eligible,
                     reject_reasons, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, toss.product_id, offer.coupang_product_id if offer else None,
                 offer.name if offer else None, offer.url if offer else None,
                 match.level.value if match else None,
                 json.dumps(match.reasons, ensure_ascii=False) if match else None,
                 comp.toss_final if comp else toss.final_price,
                 comp.coupang_final if comp else None, comp.diff if comp else None,
                 str(comp.rate) if comp and comp.rate is not None else None,
                 comp.grade.value if comp and comp.grade else None, int(eligible),
                 json.dumps(reject_reasons, ensure_ascii=False), iso(utcnow())))

    def add_review(self, toss: TossProduct, offer: CoupangOffer, match: MatchResult) -> bool:
        with self.tx() as c:
            cur = c.execute(
                """INSERT OR IGNORE INTO review_queue(product_id, coupang_product_id, toss_final, coupang_final,
                     match_level, reasons, toss_name, coupang_name, toss_url, coupang_url, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (toss.product_id, offer.coupang_product_id, toss.final_price, offer.final_price,
                 match.level.value, json.dumps(match.reasons, ensure_ascii=False), toss.name, offer.name,
                 toss.post_url, offer.url, iso(utcnow())))
            return cur.rowcount > 0

    def list_reviews(self, status: str = "open", limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM review_queue WHERE status=? ORDER BY id DESC LIMIT ?",
                                 (status, limit)).fetchall()

    def set_review_status(self, review_id: int, status: str) -> bool:
        with self.tx() as c:
            return c.execute("UPDATE review_queue SET status=? WHERE id=?", (status, review_id)).rowcount > 0

    # ---------------- posts ----------------
    def last_post(self, channel: str, fingerprint: str, product_id: str, canonical_url: str,
                  statuses: tuple[str, ...] = POSTED_STATUSES) -> sqlite3.Row | None:
        marks = ",".join("?" * len(statuses))
        return self.conn.execute(
            f"""SELECT * FROM posts WHERE channel=? AND status IN ({marks})
                AND (fingerprint=? OR product_id=? OR canonical_url=?)
                ORDER BY id DESC LIMIT 1""",
            (channel, *statuses, fingerprint, product_id, canonical_url)).fetchone()

    def reserve_post(self, channel: str, fingerprint: str, product_id: str, canonical_url: str,
                     grade: str, toss_price: int, coupang_price: int, message: str,
                     decide) -> int | None:
        """중복 판단과 'pending' 예약을 하나의 쓰기 트랜잭션으로 수행.

        decide(last_posted_row, pending_row) -> (ok: bool, reason: str)
        """
        now = iso(utcnow())
        with self.tx(immediate=True) as c:
            last = self.last_post(channel, fingerprint, product_id, canonical_url)
            pending = c.execute(
                "SELECT * FROM posts WHERE channel=? AND status='pending' AND "
                "(fingerprint=? OR product_id=? OR canonical_url=?) ORDER BY id DESC LIMIT 1",
                (channel, fingerprint, product_id, canonical_url)).fetchone()
            ok, _reason = decide(last, pending)
            if not ok:
                return None
            cur = c.execute(
                """INSERT INTO posts(fingerprint, product_id, canonical_url, channel, grade, toss_price,
                     coupang_price, status, message, attempts, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,'pending',?,0,?,?)""",
                (fingerprint, product_id, canonical_url, channel, grade, toss_price, coupang_price,
                 message, now, now))
            return int(cur.lastrowid)

    def update_post(self, post_id: int, status: str, attempts: int, error: str | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE posts SET status=?, attempts=?, error=?, updated_at=? WHERE id=?",
                      (status, attempts, error, iso(utcnow()), post_id))

    def expire_stale_pending(self, older_than: timedelta) -> int:
        """비정상 종료로 남은 pending 예약을 'unknown'으로 정리 (재게시 허용 X, 운영자 확인용)."""
        with self.tx() as c:
            return c.execute("UPDATE posts SET status='unknown', updated_at=? WHERE status='pending' "
                             "AND updated_at < ?", (iso(utcnow()), iso(utcnow() - older_than))).rowcount

    def recent_posts(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()

    def count_posts_since(self, channel: str, since: datetime) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE channel=? AND status IN ('sent','handed_off') AND updated_at>=?",
            (channel, iso(since))).fetchone()[0]
