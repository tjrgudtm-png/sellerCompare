"""구조화 로그, 시크릿 마스킹, 실행 통계, 헬스체크 파일."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("dealbot")

_SECRET_PATTERNS = [
    re.compile(r"(signature=)[0-9a-fA-F]+"),
    re.compile(r"(access-key=)[^,\s]+"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+"),
    re.compile(r"(bot)\d+:[A-Za-z0-9_\-]+"),
    re.compile(r"((?:client_secret|secret|password|token|api_key)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'&]+",
               re.IGNORECASE),
]


class SecretMaskingFilter(logging.Filter):
    """로그 메시지와 extra 필드에서 시크릿 값/패턴을 마스킹한다."""

    def __init__(self, secrets: list[str] | None = None):
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 4]

    def mask(self, text: str) -> str:
        for s in self.secrets:
            text = text.replace(s, "***")
        for pat in _SECRET_PATTERNS:
            text = pat.sub(lambda m: m.group(1) + "***", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.mask(record.getMessage())
        record.args = ()
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            record.data = json.loads(self.mask(json.dumps(data, ensure_ascii=False, default=str)))
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if isinstance(data, dict):
            payload.update(data)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(log_dir: Path, level: str = "INFO", secrets: list[str] | None = None) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("dealbot")
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    mask = SecretMaskingFilter(secrets)

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / "dealbot.jsonl", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(JsonFormatter())
    file_handler.addFilter(mask)

    err_handler = logging.handlers.RotatingFileHandler(
        log_dir / "errors.jsonl", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(JsonFormatter())
    err_handler.addFilter(mask)

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console.addFilter(mask)

    for h in (file_handler, err_handler, console):
        root.addHandler(h)
    root.propagate = False


def event(level: int, msg: str, **data) -> None:
    log.log(level, msg, extra={"data": data})


@dataclass
class RunStats:
    run_id: int | None = None
    started_at: str = ""
    finished_at: str | None = None
    status: str = "running"
    collected: int = 0
    collect_failed: int = 0
    compared: int = 0
    compare_skipped: int = 0
    match_high: int = 0
    match_medium: int = 0
    match_low: int = 0
    deals_found: int = 0
    posted: int = 0
    post_failed: int = 0
    dedup_skipped: int = 0
    failures: list[dict] = field(default_factory=list)

    def fail(self, stage: str, kind: str, detail: str, **extra) -> None:
        item = {"stage": stage, "kind": kind, "detail": detail[:500], **extra}
        self.failures.append(item)
        event(logging.WARNING, f"{stage} 실패: {kind}", **item)

    def summary(self) -> dict:
        d = asdict(self)
        d["failure_count"] = len(self.failures)
        d.pop("failures")
        return d


def write_health(path: Path, stats: RunStats, interval_minutes: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prev: dict = {}
    if path.exists():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
    now = datetime.now(timezone.utc).isoformat()
    health = {
        "updated_at": now,
        "interval_minutes": interval_minutes,
        "pid": os.getpid(),
        "last_run": stats.summary(),
        "last_success_at": now if stats.status in ("success", "partial") else prev.get("last_success_at"),
        "last_failure_at": now if stats.status == "failed" else prev.get("last_failure_at"),
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def check_health(path: Path, interval_minutes: float, now: datetime | None = None) -> tuple[bool, str]:
    """마지막 정상 실행이 주기의 3배 이내면 정상."""
    if not path.exists():
        return False, "헬스 파일이 없습니다 (아직 실행되지 않음)"
    try:
        health = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, f"헬스 파일을 읽을 수 없습니다: {e}"
    last = health.get("last_success_at")
    if not last:
        return False, "정상 완료된 실행 기록이 없습니다"
    now = now or datetime.now(timezone.utc)
    age = now - datetime.fromisoformat(last)
    limit = timedelta(minutes=interval_minutes * 3)
    if age > limit:
        return False, f"마지막 정상 실행이 {age} 전입니다 (허용 {limit})"
    return True, f"정상: 마지막 정상 실행 {last}"
