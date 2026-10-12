"""로그 시크릿 마스킹, 헬스체크, 설정 로딩"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

import pytest

from dealbot.config import ConfigError, load_config
from dealbot.monitoring import RunStats, check_health, setup_logging, write_health


def test_secrets_never_written_to_logs(tmp_path):
    setup_logging(tmp_path, "INFO", ["SUPERSECRETKEY123"])
    log = logging.getLogger("dealbot")
    log.info("auth CEA algorithm=HmacSHA256, access-key=AK1, signature=abcdef0123")
    log.info("key=SUPERSECRETKEY123", extra={"data": {"header": "Bearer tok.en-1", "x": "SUPERSECRETKEY123"}})
    log.info("https://api.telegram.org/bot123456:ABC-def/sendMessage")
    for h in log.handlers:
        h.flush()
    text = (tmp_path / "dealbot.jsonl").read_text(encoding="utf-8")
    for secret in ("SUPERSECRETKEY123", "abcdef0123", "AK1", "tok.en-1", "ABC-def"):
        assert secret not in text
    assert all(json.loads(line) for line in text.splitlines())


def test_health_ok_and_stale(tmp_path):
    path = tmp_path / "h.json"
    assert check_health(path, 10)[0] is False
    write_health(path, RunStats(status="success"), 10)
    assert check_health(path, 10)[0] is True
    later = datetime.now(timezone.utc) + timedelta(minutes=31)
    assert check_health(path, 10, now=later)[0] is False


def test_failed_run_does_not_update_last_success(tmp_path):
    path = tmp_path / "h.json"
    write_health(path, RunStats(status="success"), 10)
    first = json.loads(path.read_text())["last_success_at"]
    write_health(path, RunStats(status="failed"), 10)
    data = json.loads(path.read_text())
    assert data["last_success_at"] == first and data["last_failure_at"]


def test_config_defaults_and_override(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("app:\n  interval_minutes: 5\ndeal:\n  min_saving_rate: 25\n", encoding="utf-8")
    (tmp_path / ".env").write_text("COUPANG_ACCESS_KEY=from_dotenv\n", encoding="utf-8")
    c = load_config(p, env={})
    assert c.get("app.interval_minutes") == 5 and c.get("deal.min_saving_rate") == 25
    assert c.get("deal.min_saving_amount") == 10000 and c.get("publish.enabled") is False
    assert c.secrets.coupang_access_key == "from_dotenv"


def test_config_validation(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("publish:\n  mode: kakao_api\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(p, env={})
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.yaml", env={})
