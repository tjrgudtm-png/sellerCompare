"""오프라인 통합 테스트: 샘플 응답 파일로 전체 파이프라인 실행 (dry-run)"""
from __future__ import annotations

from conftest import FIXTURES

from dealbot.cli import build_pipeline, main


def test_fixture_pipeline_end_to_end(cfg, repo, capsys):
    c = cfg({"matching": {"recompare_after_minutes": 0}})
    p = build_pipeline(c, repo, FIXTURES)
    stats = p.run_once()
    assert stats.collected == 11 and stats.collect_failed == 1
    assert stats.posted == 4 and stats.deals_found == 4
    out = capsys.readouterr().out
    assert "[S급 특가] 테스트브랜드 무선 이어폰 SAMPLE-EB100 블랙" in out
    assert "[A급 특가] 테스트브랜드 생수 500ml x 20개" in out
    assert "[옵션 특가] 테스트브랜드 오버핏 맨투맨" in out and "그레이 / M" in out
    assert "[개당 특가] 테스트브랜드 제로콜라 355ml x 20개" in out
    assert "니트 가디건" not in out   # 미끼 옵션(추가구성) 제외
    assert {r["product_id"] for r in repo.list_reviews()} == {"SAMPLE-004", "SAMPLE-008"}
    # 두 번째 실행: 같은 특가는 재게시하지 않음
    stats2 = p.run_once()
    assert stats2.posted == 0 and stats2.dedup_skipped == 4


def test_cli_run_once_and_health(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["run-once", "--fixtures", str(FIXTURES)]) == 0
    assert main(["health"]) == 0
    assert "OK" in capsys.readouterr().out
