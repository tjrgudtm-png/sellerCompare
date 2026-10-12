"""명령행 인터페이스.

  dealbot init-db                    DB 초기화
  dealbot run-once [--fixtures DIR]  1회 실행 (fixtures 지정 시 오프라인 + 강제 dry-run)
  dealbot serve                      설정 주기(기본 10분)로 상주 실행
  dealbot health                     헬스체크 (마지막 정상 실행 시각), 비정상이면 종료코드 1
  dealbot runs | posts | review      기록 조회
  dealbot probe toss|coupang         실제 API 응답을 파일로 저장 (필드 매핑 검증용)
  dealbot test-publish               설정된 채널로 테스트 메시지 1건 전송
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from datetime import datetime
from pathlib import Path

from .chat_publisher import DryRunPublisher, PublishError, build_publisher
from .config import ConfigError, load_config
from .coupang_collector import CoupangPartnersClient, FixtureCoupangSource
from .http import HttpClient
from .monitoring import check_health, setup_logging
from .pipeline import Pipeline
from .repository import Repository
from .scheduler import RunLock, Scheduler, run_locked
from .toss_collector import FixtureTossSource, TossSharelinkClient


def _setup(args):
    cfg = load_config(args.config)
    setup_logging(cfg.path("app.log_dir"), cfg.get("app.log_level"), cfg.secrets.values())
    repo = Repository(cfg.path("app.db_path"))
    repo.init_schema()
    return cfg, repo


def build_pipeline(cfg, repo, fixtures: Path | None = None) -> Pipeline:
    http = HttpClient.from_config(cfg)
    if fixtures:
        toss = FixtureTossSource(cfg, sorted(fixtures.glob("toss_*.json")))
        coupang = FixtureCoupangSource(fixtures / "coupang_search.json")
        publisher = DryRunPublisher()
    else:
        toss = TossSharelinkClient(cfg, http)
        coupang = CoupangPartnersClient(cfg, http, repo)
        publisher = build_publisher(cfg, http, repo)
    return Pipeline(cfg, repo, toss, coupang, publisher)


def cmd_init_db(args) -> int:
    cfg, repo = _setup(args)
    print(f"DB 초기화 완료: {cfg.path('app.db_path')}")
    return 0


def cmd_run_once(args) -> int:
    cfg, repo = _setup(args)
    if not cfg.get("publish.enabled") and not args.fixtures:
        print("※ publish.enabled=false: 테스트 모드(dry-run) - 실제 메시지를 전송하지 않습니다")
    pipeline = build_pipeline(cfg, repo, Path(args.fixtures) if args.fixtures else None)
    stats = run_locked(RunLock(cfg.path("app.lock_file")), pipeline.run_once)
    if stats is None:
        print("다른 실행이 진행 중이라 건너뜀")
        return 2
    print(json.dumps(stats.summary(), ensure_ascii=False, indent=2))
    for f in stats.failures:
        print(f"  - [{f['stage']}/{f['kind']}] {f['detail']}")
    return 0 if stats.status != "failed" else 1


def cmd_serve(args) -> int:
    cfg, repo = _setup(args)
    pipeline = build_pipeline(cfg, repo)
    interval = float(cfg.get("app.interval_minutes"))
    sched = Scheduler(interval, pipeline.run_once, RunLock(cfg.path("app.lock_file")))
    signal.signal(signal.SIGINT, lambda *_: sched.stop())
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, lambda *_: sched.stop())
    mode = cfg.get("publish.mode") if cfg.get("publish.enabled") else "dry_run(테스트 모드)"
    print(f"dealbot 상주 실행: {interval}분 주기, 게시 모드={mode}. 종료: Ctrl+C")
    sched.run_forever()
    return 0


def cmd_health(args) -> int:
    cfg = load_config(args.config)
    ok, msg = check_health(cfg.path("app.health_file"), float(cfg.get("app.interval_minutes")))
    print(("OK " if ok else "FAIL ") + msg)
    hf = cfg.path("app.health_file")
    if hf.exists():
        print(hf.read_text(encoding="utf-8"))
    return 0 if ok else 1


def _print_rows(rows, cols) -> None:
    for r in rows:
        print(" | ".join(f"{c}={r[c]}" for c in cols))
    if not rows:
        print("(없음)")


def cmd_runs(args) -> int:
    _, repo = _setup(args)
    _print_rows(repo.recent_runs(args.limit), ["id", "started_at", "finished_at", "status", "stats_json"])
    return 0


def cmd_posts(args) -> int:
    _, repo = _setup(args)
    _print_rows(repo.recent_posts(args.limit),
                ["id", "channel", "status", "grade", "product_id", "toss_price", "coupang_price",
                 "attempts", "error", "updated_at"])
    return 0


def cmd_review(args) -> int:
    _, repo = _setup(args)
    if args.set:
        rid, status = args.set
        ok = repo.set_review_status(int(rid), status)
        print("변경됨" if ok else "해당 ID 없음")
        return 0 if ok else 1
    _print_rows(repo.list_reviews(args.status, args.limit),
                ["id", "match_level", "toss_name", "toss_final", "coupang_name", "coupang_final",
                 "toss_url", "coupang_url", "reasons"])
    return 0


def cmd_probe(args) -> int:
    cfg, repo = _setup(args)
    http = HttpClient.from_config(cfg)
    out_dir = cfg.base_dir / "data" / "probe"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    try:
        if args.target == "toss":
            client = TossSharelinkClient(cfg, http)
            client._check_config()
            sources = [s for s in cfg.get("toss.sources") if not args.source or s.get("name") == args.source]
            for s in sources:
                payload = client.fetch_raw(s["path"], s.get("params"))
                path = out_dir / f"toss_{s.get('name', 'source')}_{stamp}.json"
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"저장: {path}")
        else:
            if not args.query:
                print("--query 필요")
                return 1
            payload = CoupangPartnersClient(cfg, http, repo).fetch_raw(args.query)
            path = out_dir / f"coupang_{stamp}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"저장: {path}")
    except Exception as e:  # probe 는 진단용 - 어떤 오류든 출력만
        print(f"실패: {type(e).__name__}: {e}")
        return 1
    print("저장된 응답 구조를 보고 config.yaml 의 toss.field_mapping 을 맞춘 뒤 "
          "`dealbot run-once --fixtures <폴더>` 로 검증하세요.")
    return 0


def cmd_test_publish(args) -> int:
    cfg, repo = _setup(args)
    if cfg.get("publish.enabled") and not args.yes:
        print("실제 채널로 전송됩니다. 확인했다면 --yes 를 붙여 다시 실행하세요.")
        return 1
    message = args.message or "[dealbot 테스트] 연결 확인용 메시지입니다."
    try:
        publisher = build_publisher(cfg, HttpClient.from_config(cfg), repo)
        result = publisher.publish(message)
    except PublishError as e:
        print(f"전송 실패: {e}")
        return 1
    print(f"결과: {result.status} {result.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dealbot", description="토스쇼핑 특가 발굴 · 쿠팡 가격 비교 봇")
    parser.add_argument("-c", "--config", default=None, help="설정 파일 경로 (기본: ./config.yaml, 없으면 기본값)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db").set_defaults(func=cmd_init_db)
    p = sub.add_parser("run-once")
    p.add_argument("--fixtures", help="오프라인 검증용 응답 파일 폴더 (toss_*.json, coupang_search.json)")
    p.set_defaults(func=cmd_run_once)
    sub.add_parser("serve").set_defaults(func=cmd_serve)
    sub.add_parser("health").set_defaults(func=cmd_health)
    for name, fn in (("runs", cmd_runs), ("posts", cmd_posts)):
        p = sub.add_parser(name)
        p.add_argument("--limit", type=int, default=20)
        p.set_defaults(func=fn)
    p = sub.add_parser("review")
    p.add_argument("--status", default="open")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--set", nargs=2, metavar=("ID", "STATUS"), help="검토 상태 변경 (예: 3 confirmed)")
    p.set_defaults(func=cmd_review)
    p = sub.add_parser("probe")
    p.add_argument("target", choices=["toss", "coupang"])
    p.add_argument("--source")
    p.add_argument("--query")
    p.set_defaults(func=cmd_probe)
    p = sub.add_parser("test-publish")
    p.add_argument("--message")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_test_publish)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as e:
        print(f"설정 오류: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
