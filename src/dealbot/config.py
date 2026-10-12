"""설정 로딩.

- 일반 설정: YAML 파일 (config.yaml). 없으면 기본값 사용.
- 시크릿(API 키, 토큰): 환경변수 또는 .env 파일에서만 읽는다. YAML에 넣지 않는다.
"""

from __future__ import annotations

import copy
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "app": {
        "interval_minutes": 10,
        "db_path": "data/dealbot.db",
        "log_dir": "logs",
        "log_level": "INFO",
        "health_file": "data/health.json",
        "lock_file": "data/dealbot.lock",
        "timezone": "Asia/Seoul",
        "max_products_per_run": 200,
    },
    "http": {
        "timeout_seconds": 15,
        "max_retries": 3,
        "backoff_base_seconds": 2.0,
        "backoff_max_seconds": 60.0,
        "min_interval_seconds": 1.0,
        "max_concurrency": 2,
        "user_agent": "dealbot/0.1 (+price-compare)",
    },
    "toss": {
        "enabled": True,
        # 토스 쉐어링크 Open API (승인제). 정확한 URL/경로/필드명은 승인 후 공식 문서로 확인해 채운다.
        "base_url": "",
        "token_url": "",
        "sources": [
            # {"name": "daily_deal", "path": "/...", "params": {}}
        ],
        "pagination": {
            "type": "page",          # page | cursor | none
            "page_param": "page",
            "size_param": "size",
            "page_size": 50,
            "first_page": 1,
            "cursor_param": "cursor",
            "next_cursor_path": "data.nextCursor",
            "max_pages": 5,
        },
        "detail": {"enabled": False, "path": "/products/{product_id}", "item_path": "data",
                   "max_per_run": 50},
        "share_link": {"enabled": False, "path": "/links", "id_field": "tacaItemId",
                       "extra_body": {}, "short_url_path": "data.shortUrl"},
        "field_mapping": {
            "items_path": "data.products",
            "id": "productId",
            "name": "productName",
            "brand": "brandName",
            "model_code": "modelName",
            "option": "optionName",
            "sale_price": "price",
            "discount_price": "discountPrice",
            "shipping_fee": "deliveryFee",
            "free_shipping": "freeShipping",
            "url": "productUrl",
            "sold_out": "soldOut",
            "image": "imageUrl",
        },
        "allowed_url_hosts": ["toss.im", "tossshopping.com", "toss.shop"],
    },
    "coupang": {
        "enabled": True,
        "base_url": "https://api-gateway.coupang.com",
        "search_path": "/v2/providers/affiliate_open_api/apis/openapi/products/search",
        "search_limit": 10,
        "max_calls_per_hour": 10,
        "cache_ttl_minutes": 360,
        "sub_id": "",
    },
    "matching": {
        "min_token_similarity_high": 0.5,
        "min_token_similarity_low": 0.25,
        "require_explicit_quantity": True,
        "recompare_after_minutes": 180,
    },
    "deal": {
        "min_saving_rate": 20,
        "min_saving_amount": 10000,
        "grades": {
            "S": {"min_rate": 30, "min_amount": 20000},
            "A": {"min_rate": 20, "min_amount": 10000},
        },
        "publish_grades": ["S", "A"],
    },
    "repost": {
        "on_grade_upgrade": True,
        "min_additional_drop_rate": 5,
        "min_additional_drop_amount": 3000,
        # 같은 가격의 같은 특가를 일정 시간 후 다시 올리고 싶다면 시간 지정 (null = 재게시 안 함)
        "same_deal_after_hours": None,
        "pending_timeout_minutes": 30,
    },
    "publish": {
        "enabled": False,
        "mode": "dry_run",   # dry_run | approval_queue | kakao_pc | telegram
        "max_attempts": 3,
        "retry_backoff_seconds": 2.0,
        "max_posts_per_run": 5,
        "approval_queue": {"dir": "data/outbox"},
        "kakao": {
            "room_name": "",
            "open_chat_url": "",
            "send_mode": "assist",     # assist(사람이 Enter) | auto(자동 전송, 위험)
            "auto_send_accept_risk": False,
            "min_seconds_between_posts": 30,
            "max_posts_per_hour": 10,
        },
        "telegram": {"api_base": "https://api.telegram.org"},
    },
    "message": {
        "max_length": 1000,
        "max_name_length": 60,
        "footer": "",
    },
}

SECRET_ENV_VARS = (
    "TOSS_ACCESS_KEY",
    "TOSS_SECRET_KEY",
    "COUPANG_ACCESS_KEY",
    "COUPANG_SECRET_KEY",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)


class ConfigError(Exception):
    pass


@dataclass
class Secrets:
    toss_access_key: str | None = None
    toss_secret_key: str | None = None
    coupang_access_key: str | None = None
    coupang_secret_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    def values(self) -> list[str]:
        return [v for v in vars(self).values() if v]


class Config:
    """dict 기반 설정 + 점(.) 경로 접근."""

    def __init__(self, data: dict[str, Any], secrets: Secrets, base_dir: Path):
        self.data = data
        self.secrets = secrets
        self.base_dir = base_dir

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def path(self, dotted: str) -> Path:
        p = Path(self.get(dotted))
        return p if p.is_absolute() else self.base_dir / p

    def validate(self) -> None:
        if float(self.get("app.interval_minutes")) <= 0:
            raise ConfigError("app.interval_minutes 는 0보다 커야 합니다")
        mode = self.get("publish.mode")
        if mode not in {"dry_run", "approval_queue", "kakao_pc", "telegram"}:
            raise ConfigError(f"알 수 없는 publish.mode: {mode}")
        for key in ("deal.min_saving_rate", "deal.min_saving_amount"):
            if float(self.get(key)) < 0:
                raise ConfigError(f"{key} 는 음수일 수 없습니다")
        grades = self.get("deal.grades")
        if not {"S", "A"} <= set(grades):
            raise ConfigError("deal.grades 에 S, A 등급이 모두 필요합니다")


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_dotenv(path: Path) -> dict[str, str]:
    """의존성 없는 최소 .env 파서 (KEY=VALUE, # 주석)."""
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def load_secrets(env: dict[str, str] | None = None, dotenv_path: Path | None = None) -> Secrets:
    merged: dict[str, str] = {}
    if dotenv_path is not None:
        merged.update(load_dotenv(dotenv_path))
    merged.update({k: v for k, v in (env if env is not None else os.environ).items()
                   if k in SECRET_ENV_VARS})
    return Secrets(
        toss_access_key=merged.get("TOSS_ACCESS_KEY") or None,
        toss_secret_key=merged.get("TOSS_SECRET_KEY") or None,
        coupang_access_key=merged.get("COUPANG_ACCESS_KEY") or None,
        coupang_secret_key=merged.get("COUPANG_SECRET_KEY") or None,
        telegram_bot_token=merged.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=merged.get("TELEGRAM_CHAT_ID") or None,
    )


def load_config(path: str | Path | None = None, overrides: dict | None = None,
                env: dict[str, str] | None = None) -> Config:
    cfg_path = Path(path) if path else Path("config.yaml")
    user: dict = {}
    if cfg_path.exists():
        with cfg_path.open(encoding="utf-8") as f:
            user = yaml.safe_load(f) or {}
        if not isinstance(user, dict):
            raise ConfigError(f"{cfg_path} 형식이 올바르지 않습니다")
    elif path:
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {cfg_path}")
    data = deep_merge(DEFAULTS, user)
    if overrides:
        data = deep_merge(data, overrides)
    base_dir = cfg_path.resolve().parent
    secrets = load_secrets(env, base_dir / ".env")
    cfg = Config(data, secrets, base_dir)
    cfg.validate()
    return cfg
