# dealbot — 토스쇼핑 특가 자동 발굴 · 쿠팡 가격 비교 봇

토스쇼핑 상품을 주기적으로 수집해 쿠팡의 **동일 상품** 가격과 비교하고, 검증된 S급/A급 특가만
카카오톡 오픈채팅방(또는 대체 채널)에 게시합니다.

> 핵심 원칙: 가짜 가격 비교 결과를 만들지 않는다 · 동일 상품이 아니면 비교하지 않는다 ·
> 검증되지 않은 특가는 자동 게시하지 않는다. 확인하지 못한 가격·배송비·품절 정보는 `None`
> 으로 남고, 그런 상품은 자동 게시 대상에서 빠집니다(0원으로 처리하지 않음).

---

### 찾아내는 특가 유형

| 유형 | 예 | 기준 (설정 변경 가능) |
|---|---|---|
| 🚨/🔥 S·A급 특가 | 토스 39,900원 vs 쿠팡 동일 구성 59,900원 | 쿠팡 동일 상품(HIGH) 대비 S: 30%·20,000원↑, A: 20%·10,000원↑ |
| 🎯 옵션 특가 | 맨투맨 옵션 대부분 2만원대인데 '그레이 / M'만 12,900원 | 같은 상품의 같은 구성 옵션들(3개↑) 중간값 대비 40%·5,000원↑. 미끼 옵션(추가구성·사은품 등)·품절 옵션 제외 |
| 📦 개당 특가 | 콜라 355ml 20개 10,000원 vs 쿠팡 40개 39,800원 | 쿠팡 동일 품목(수량만 다름) 개당가 대비 40%·5,000원↑ |

같은 묶음끼리의 반값(예: 20개 2만원 → 1만원)은 S·A급 비교에서 잡힙니다.
한 상품이 여러 유형에 걸리면 한 번만 게시합니다(S → A → 옵션 → 개당 순).

## 1. 외부 서비스 연동 현황 (조사 결과)

| 단계 | 사용 방식 | 상태 | 제약 |
|---|---|---|---|
| 토스쇼핑 상품 수집 | **토스 쉐어링크 Open API** (베스트·카테고리 베스트·하루특가 목록, 상품 상세, 쉐어링크 발급) | 구현 완료, **API 승인 후 설정 필요** | 승인제(검수 후 Access/Secret Key 발급, 호출 서버 IP 등록). 시범 운영 중이라 응답 필드가 바뀔 수 있음. 목록의 `productUrl`은 비추적 일반 링크 → 게시용은 쉐어링크 발급 사용 권장. 정확한 엔드포인트·필드명은 공식 문서를 이 환경에서 열 수 없어 **설정값(자리표시자)** 으로 둠 |
| 토스쇼핑 웹 스크래핑 | 사용 안 함 | 미구현 | 공개 API가 아니고 약관 확인 불가. 접근 제한 우회 금지 원칙 |
| 쿠팡 가격 | **쿠팡 파트너스 Open API** 상품 검색 (HMAC 서명) | 구현 완료, **API 승인 후 키 필요** | 파트너스 가입·승인 필요. 검색 호출 한도가 매우 작음(기본 시간당 10회로 설정, 공식 수치 확인 필요). 응답에 옵션 구성·배송비 금액·와우/카드/쿠폰 할인 구분이 없음 |
| 쿠팡 웹 크롤링 | 사용 안 함 | 미구현 | robots.txt·봇 차단. 우회 금지 |
| 카카오 오픈채팅방 게시 | 공식 API **없음** (카카오 개발자 포럼 공식 답변: 기존 단체방/오픈채팅방 대상 메시지 기능 미제공, 메시지 API는 사용자 간 용도) | **Windows PC 카카오톡 UI 자동화**로 구현 (비공식) | 아래 §7 위험 참고 |
| 대체 채널 | Telegram Bot API(공식), 승인 대기열(파일) | 구현 완료 | — |

## 2. 아키텍처와 데이터 흐름

```
Scheduler (10분, 파일 락으로 중복 실행 방지, 밀린 주기는 건너뜀)
 └─ Pipeline.run_once()
     ① toss_collector      토스 쉐어링크 API 목록(+페이지네이션) → 필드 매핑 → TossProduct
     ② repository          상품 upsert + 가격이 바뀐 경우만 가격 이력 추가
     ③ coupang_collector   검색어 생성(브랜드+모델코드 / 정규화 상품명) → 파트너스 검색
                           (DB에 영속되는 시간당 호출 예산 + 검색 결과 캐시)
     ④ product_matcher     브랜드·모델코드·용량·중량·수량·색상·인치·구성 비교 → HIGH/MEDIUM/LOW
     ⑤ price_comparator    차액 = 쿠팡 최종가 − 토스 최종가, 절감률 = 차액 / 쿠팡 최종가 × 100 → S/A/일반
     ⑥ deal_filter         모든 조건 AND (HIGH, 가격 확정, 20%↑, 10,000원↑, 판매중, 링크 유효, S/A)
     ⑦ deduplication       상품ID·정규화명+옵션·정규 URL 기준 + 재게시 규칙, 트랜잭션 내 'pending' 예약
     ⑧ message_formatter   실제 수집값만으로 메시지 생성 (길면 축약)
     ⑨ chat_publisher      dry_run | approval_queue | kakao_pc | telegram (재시도·백오프)
     ⑩ monitoring          JSON 로그, 실행 통계(runs 테이블), health.json
   MEDIUM 매칭 / 쿠팡 배송비 미확정 HIGH → review_queue (사람 검토), LOW → 제외
```

## 3. 디렉터리 구조

```
config.example.yaml   설정 예시 (→ config.yaml)
.env.example          API 키 예시 (→ .env)
src/dealbot/
  config.py           설정 로딩/검증 (YAML + .env/환경변수)
  models.py           도메인 모델 (가격은 원 단위 int, 미확인은 None)
  http.py             요청 간격·동시성 제한, 네트워크/HTTP 오류 구분, 지수 백오프
  normalizer.py       상품명 정규화, 속성 추출, 가격 파싱
  toss_collector/     쉐어링크 API 클라이언트, 필드 매핑, 오프라인 픽스처 소스
  coupang_collector/  파트너스 검색 API 클라이언트(HMAC), 예산·캐시, 픽스처 소스
  product_matcher.py  동일 상품 판정
  price_comparator.py 차액/절감률/등급
  deal_filter.py      게시 대상 선별
  deduplication.py    중복 게시 방지·재게시 규칙
  message_formatter.py
  chat_publisher/     dry_run, approval_queue, kakao_pc, telegram
  repository/         SQLite 스키마와 접근 함수
  monitoring.py       로그·마스킹·헬스체크
  pipeline.py         전체 흐름
  scheduler.py        주기 실행·락
  cli.py              명령행
tests/                단위·통합 테스트 + fixtures(샘플 응답, 실제 상품 아님)
deploy/windows/       작업 스케줄러 등록 스크립트
```

## 4. 데이터베이스

SQLite(WAL) 파일 `data/dealbot.db`. `dealbot init-db` 또는 첫 실행 시 자동 생성(`CREATE TABLE IF NOT EXISTS`).

| 테이블 | 내용 |
|---|---|
| `toss_products` | 상품ID(PK), 이름/정규화명, 브랜드, 모델, 옵션, URL, 쉐어링크, 품절, 최초/최근 수집, 최근 비교 시각 |
| `toss_price_history` | 판매가·할인가·배송비·최종가·품절 — **값이 바뀔 때만** 추가 |
| `coupang_search_cache` | 검색어별 쿠팡 결과 캐시 |
| `api_calls` | 쿠팡 호출 기록(시간당 예산, 재시작해도 유지) |
| `comparisons` | 실행별 비교 결과, 매칭 근거, 등급, 탈락 사유 |
| `review_queue` | MEDIUM 매칭 / 가격 미확정 건 (사람 검토) |
| `posts` | 게시 이력: 채널, 상태(pending/sent/handed_off/queued/dry_run/failed/unknown), 가격, 메시지 |
| `runs` | 실행 기록: 수집·비교·특가·게시 건수, 실패 원인 |

중복 게시 방지는 `BEGIN IMMEDIATE` 트랜잭션 안에서 "이력 확인 → pending 예약"을 하고,
`(channel, fingerprint) WHERE status='pending'` 유니크 인덱스로 동시 예약을 막습니다.
전송 중 비정상 종료로 남은 pending 은 `unknown` 으로 바뀌며, 이중 게시를 피하기 위해 게시된 것으로 취급합니다.

## 5. 설치 (Windows)

```powershell
# Python 3.11 이상 설치 후, 프로젝트 폴더에서
python -m venv .venv
.venv\Scripts\activate
pip install -e .[windows,dev]

copy config.example.yaml config.yaml
copy .env.example .env      # 메모장으로 열어 키 입력
dealbot init-db
```

## 6. 실행 순서 (최초에는 실제 게시 비활성)

1. **오프라인 검증** — 샘플 응답으로 전체 흐름 확인 (실제 상품 아님, 항상 dry-run)
   ```
   dealbot run-once --fixtures tests/fixtures
   ```
2. **API 키 발급 후 실제 응답 확인** — 응답을 파일로 저장해 필드 매핑을 맞춤
   ```
   dealbot probe toss               # data/probe/toss_*.json
   dealbot probe coupang --query "삼성 SM-R630N"
   ```
   저장된 JSON 구조에 맞게 `config.yaml`의 `toss.base_url / token_url / sources / field_mapping` 수정 후
   `dealbot run-once --fixtures data/probe` 로 매핑 검증 (쿠팡 픽스처가 없으면 비교 대상 0건으로 나옴).
3. **실데이터 테스트 모드** — `publish.enabled: false` 상태로
   ```
   dealbot run-once                 # 게시 예정 메시지만 출력
   dealbot review                   # MEDIUM/가격 미확정 검토 목록
   dealbot runs ; dealbot posts
   ```
   며칠간 비교 결과를 검토해 오탐이 없는지 확인합니다.
4. **실제 게시 활성화** — `config.yaml` 에서
   ```yaml
   publish:
     enabled: true
     mode: kakao_pc
     kakao:
       room_name: "오픈채팅방 이름"   # 카카오톡 채팅방 창 제목과 동일
       send_mode: assist
   ```
   연결 확인: `dealbot test-publish --yes`

## 7. 카카오톡 오픈채팅방 게시 (`kakao_pc`) — 위험과 사용법

- 카카오는 오픈채팅방에 메시지를 보내는 **공식 API를 제공하지 않습니다.** 이 기능은 PC 카카오톡 채팅방 창에
  Win32 메시지로 텍스트를 넣는 **비공식 자동화**입니다.
- **위험**: 카카오 운영정책상 비정상 자동화로 간주되어 계정 이용이 제한될 수 있습니다. 카카오톡 업데이트로
  창 구조가 바뀌면 동작하지 않을 수 있고, 전송 성공 여부를 프로그램이 확인할 수 없습니다.
- **사전 조건**: Windows PC 에서 카카오톡 로그인 → 오픈채팅방(https://open.kakao.com/o/gcmpd0Ri) 입장 →
  그 채팅방을 **별도 창으로 열어 둔 상태** 유지. 로그인 정보는 프로그램이 다루지 않습니다.
- `send_mode: assist`(기본, 권장): 봇이 입력창에 메시지를 채우고 창을 앞으로 띄웁니다. **사람이 Enter 로 전송**합니다.
  입력창에 아직 보내지 않은 글이 있으면 덮어쓰지 않고 실패로 기록 → 다음 실행에서 다시 시도합니다.
- `send_mode: auto`: Enter 까지 자동. `auto_send_accept_risk: true` 를 명시해야만 동작합니다.
  시간당 게시 상한(`max_posts_per_hour`)과 게시 간격(`min_seconds_between_posts`)이 적용됩니다.
- 대안: `mode: approval_queue`(data/outbox 에 메시지 파일 저장 → 수동 복사) 또는 `mode: telegram`.

## 8. 10분 주기 자동 실행 / 재부팅 후 자동 실행

- 주기 실행: `dealbot serve` (주기는 `app.interval_minutes`, 기본 10분). 실행이 주기보다 길어지면
  겹쳐 실행하지 않고 다음 주기로 넘어가며, 다른 dealbot 프로세스가 돌고 있으면 해당 주기는 건너뜁니다.
- 재부팅 후 자동 실행 (Windows 작업 스케줄러, 로그온 시 시작):
  ```powershell
  powershell -ExecutionPolicy Bypass -File deploy\windows\install_task.ps1
  Start-ScheduledTask -TaskName dealbot      # 즉시 시작
  powershell -ExecutionPolicy Bypass -File deploy\windows\uninstall_task.ps1   # 해제
  ```
  카카오톡 창 자동화는 로그인된 데스크톱이 필요하므로 Windows 서비스가 아닌 **로그온 트리거**를 사용합니다.
  PC 자동 로그인과 카카오톡 자동 시작을 함께 설정해야 무인 재부팅 후에도 게시가 이어집니다.
  절전 모드로 들어가지 않도록 전원 설정도 확인하세요.

## 9. 테스트

```
pip install -e .[dev]
python -m pytest -q
```

| 요구 테스트 | 파일 |
|---|---|
| 1 상품 가격 수집 | `tests/test_collect.py` (매핑, 누락 필드, 품절, 페이지네이션, 쿠팡 파싱·서명·예산, HTTP 재시도) |
| 2 동일 상품/다른 옵션 판별 | `tests/test_matcher.py` |
| 3 할인율·차액 / 4 S·A 등급 | `tests/test_comparator.py` |
| 5 중복 게시 방지 | `tests/test_dedup.py` |
| 6 가격 수집 실패 시 오판 방지 | `tests/test_failure_safety.py` |
| 7 메시지 포맷 | `tests/test_formatter.py` |
| 8 게시 실패·재시도 | `tests/test_publish.py` |
| 9 연속 실행·중복 실행 방지 | `tests/test_scheduler.py` |
| 통합(오프라인 E2E), 로그 마스킹, 헬스체크 | `tests/test_e2e_fixtures.py`, `tests/test_monitoring_config.py` |

## 10. 오류 확인

- `dealbot health` — 마지막 정상 실행 시각 확인(주기의 3배 이상 지나면 FAIL, 종료코드 1). `data/health.json`
- `logs/dealbot.jsonl` — 전체 구조화 로그 / `logs/errors.jsonl` — 경고·오류만 / `logs/console.log` — 상주 실행 콘솔
- `dealbot runs` — 실행별 수집·비교·특가·게시 건수 / `dealbot posts` — 게시 상태와 오류
- 실패 원인 `kind`: `network`(연결/타임아웃), `http`(상태코드), `format`(응답 구조), `mapping`(필드 누락), `config`(설정/키 누락)
- 로그에는 API 키, 서명, Bearer 토큰, 텔레그램 봇 토큰이 마스킹됩니다.

## 11. 실제 제약 및 미구현 항목

- **토스 쉐어링크 API 상세 스펙 미확인**: 이 개발 환경에서 공식 문서 접속이 차단되어 엔드포인트·필드명은
  설정 자리표시자입니다. 승인 후 `probe`로 확인해 맞춰야 합니다. 배송비·품절 필드가 응답에 없다면
  해당 상품은 자동 게시되지 않습니다(의도된 동작).
- **쿠팡 가격의 한계**: 파트너스 검색 API는 표시 판매가만 제공합니다. 쿠폰·카드·와우 할인은 확인할 수 없어
  적용하지 않으며, 메시지에 "표시가 기준"으로 표기합니다. `isFreeShipping=false` 상품은 배송비를 알 수 없어
  자동 게시하지 않고 검토 대기열로 보냅니다. 옵션별 가격·필수 옵션 추가금도 확인할 수 없어,
  상품명에서 옵션(용량/수량/색상)을 양쪽 모두 확인할 수 있을 때만 HIGH 로 판정합니다.
- **쿠팡 호출 한도**: 시간당 호출 수가 작아 한 주기에 비교할 수 있는 상품 수가 제한됩니다. 할인율이 큰 상품,
  가격이 바뀐 상품부터 비교하고 나머지는 다음 주기로 미룹니다.
- **옵션별 가격**: 토스 쉐어링크 API 응답에 옵션별 가격이 있는지 확인하지 못했습니다. 없으면 옵션 특가는
  동작하지 않습니다(가격을 추정하지 않음). `probe toss` 결과로 `options_path` 등 옵션 매핑을 맞추세요.
- **이미지 유사도**: 매칭기에 확장 지점(`image_similarity`)만 있고 기본 구현은 없습니다(이미지 다운로드·해시 필요).
- **카카오 오픈채팅 게시**: 공식 미지원 → 비공식 PC 자동화(위험 고지). 전송 확인 불가.
- **이용약관**: 쿠팡 파트너스 API를 가격 비교 목적으로 쓰는 것, 토스 쉐어링크 게시물의 광고 고지 의무 등은
  각 서비스 약관을 직접 확인하세요. 고지 문구가 필요하면 `message.footer`에 넣습니다.
