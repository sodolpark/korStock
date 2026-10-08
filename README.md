# KorStockTrade

KRX 보통주 현물 전략 `research_v1`의 수집·재생·위험 관리·키움 REST/WebSocket 실행 도구입니다. 원본의 미공개 진입 산식을 복제한 프로그램이 아니라 [구현 계획](1_plan/Implementation_Plan_KRX_Kiwoom.md)에 정의한 연구 가설의 구현입니다.

기본 설정은 **네트워크가 필요 없는 replay**이며 실전 주문은 꺼져 있습니다. 합성 데이터 기능 검증을 완료했으며, 실제 계좌 계약 검증·시장 데이터 성과 검증·20거래일 shadow·실거래 검증은 아직 진행하지 않았습니다. 범위별 결과는 [구현 상태](reports/Implementation_Status.md), 실행·복구 절차는 [운영 문서](docs/OPERATIONS.md)를 보세요.

## 설치와 즉시 실행

Python 3.12 이상을 지원하도록 작성했으며 현재 Windows의 Python 3.13.15에서 검증했습니다. PowerShell 기준:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.lock.txt
.\.venv\Scripts\python -m pip install --no-deps -e .
.\.venv\Scripts\python -m korstocktrade check
.\.venv\Scripts\python -m korstocktrade demo --output reports/runs/my-demo
```

데모 출력 폴더는 매번 새 경로를 사용합니다. 원시 로그는 기존 파일을 덮어쓰지 않습니다. Linux에서는 `.venv/bin/python`을 사용합니다.

데모는 합성 10호가와 체결을 생성하고 진입·체결·청산·비용·잔여 잔고 보고까지 실행합니다. 출력 손익은 수익성 검증 결과가 아닙니다.

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\ruff check src scripts tests
.\.venv\Scripts\python -m korstocktrade --help
```

## 구성

| 경로 | 역할 |
|---|---|
| `src/korstocktrade/broker/kiwoom` | 토큰, REST 제한·연속조회, WebSocket 로그인·재접속·구독, KRX 필드 파싱 |
| `market` | 거래일별 세션, 호가단위, 후보 선정·보유/미체결 우선 구독 |
| `strategy` | 시간가중 호가, 1초/완성 5초 지표, 3단계 진입, 독립 고점/청산 타이머 |
| `execution` | SQLite 주문·체결 원장, 현금/노출 예약, 대사, 명시적 복구 |
| `storage` | 비밀정보 제거, 수신 순서 JSONL, Parquet/DuckDB, OS 계좌 잠금 |
| `replay` | 지연·깊이·부분체결·취소 모형, 날짜 분리, bootstrap, 성과 보고 |
| `app` | 공통 처리 코어, CLI, 온라인 수집·운영 상태 |
| `configs` | 연구값, 실행 불가 근거 프로필, 운영 입력 양식 |
| `tests/fixtures` | 원본 연구의 64행 점수와 3건 거래 산술 자료 |

실행 경로에는 LLM 호출이 없습니다. 계좌 값과 신호는 직렬 처리하며, 모의/실전과 실행 모드별 원장을 분리합니다. `collect`에는 주문 어댑터를 주입하지 않고, `shadow`에는 로컬 체결 모형만 주입합니다.

## 주요 구현 규칙

- 미체결 매수 예약금도 전체 50% 노출과 현금 한도에 포함합니다. 보유 중 추가매수는 하지 않습니다.
- 주문 의도는 전송 전에 저장합니다. 응답 불명은 `UNKNOWN`으로 남기고 자동 재전송하지 않습니다.
- 체결 ID가 같은 통보는 한 번만 반영합니다. 시장 체결은 시각·가격·수량이 같아도 임의로 중복 제거하지 않습니다.
- 고점 수익 2.95%의 보호선은 2.36%입니다. 고점 확인 10초, 보호선 하방 확인 10초, 추세 AND 불균형 확인 21초를 각각 관리합니다.
- VI·시세 공백·구독 변경 시 연속 타이머를 초기화합니다. 고정 손절은 유효 매수호가로 별도 평가합니다.
- 15:19 청산을 시작하고, 15:20 이후 재생에서 연속매매 체결을 만들지 않습니다. 남은 잔고·주문은 보고서에 인계 대상으로 표시합니다.
- 실제 세금·수수료, 상품 분류·거래 가능 상태, 거래일별 일정은 입력 자료입니다. 예시 비용이나 합성 일정은 실거래 근거가 아닙니다.

## 재생과 분석

```powershell
.\.venv\Scripts\python -m korstocktrade replay reports/runs/my-demo/events.jsonl --session reports/runs/my-demo/session.json --instruments reports/runs/my-demo/instruments.json --date 2026-10-08 --output reports/runs/replay.json
.\.venv\Scripts\python -m korstocktrade compare reports/runs/my-demo/report.json reports/runs/replay.json
.\.venv\Scripts\python -m korstocktrade parquet reports/runs/my-demo/events.jsonl data/demo.parquet
.\.venv\Scripts\python -m korstocktrade stress reports/runs/my-demo/events.jsonl --session reports/runs/my-demo/session.json --instruments reports/runs/my-demo/instruments.json --date 2026-10-08 --output reports/runs/stress.json
```

날짜 전체를 60/20/20으로 분리하는 `split`, 360초 분석창과 보유 구간의 경계 제거 함수, 날짜 블록 bootstrap 하한 계산을 제공합니다. `stress`는 비용 2배·불리한 1/2틱·1초 지연·청산 모듈 제거·고정창을 별도 설정 해시로 평가합니다. 실제 데이터가 부족하면 보고서의 자격 상태는 `UNVALIDATED`로 남습니다.

## 기간 백테스트 (기본: 최근 1년)

`backtest`는 기간 안의 날짜마다 수집된 원시 로그를 재생하고 결과를 합칩니다. 기본 기간은 **어제(KST)까지 365일**입니다. 날짜마다 독립된 계좌 일자로 재생하므로(매일 `risk.equity`에서 시작, 비복리) `--workers` 수만큼 병렬로 실행해도 결과가 같습니다.

```powershell
.\.venv\Scripts\python -m korstocktrade backtest
.\.venv\Scripts\python -m korstocktrade backtest --start 2025-10-09 --end 2026-10-08 --stress --workers 8
```

입력은 `run --mode collect|shadow`가 `data/<environment>/<account_alias>/<날짜>/<모드>/`에 남긴 `events-<run>.jsonl`입니다. 같은 실행이 함께 저장한 `session-<run>.json`·`instruments-<run>.json`도 사용합니다. 한 날짜에 재시작으로 로그가 여러 개이면 수신 순서대로 같은 계좌 원장에 이어서 재생합니다. `--session`(날짜별 일정)과 `--instruments`(`verified_date`별 종목 행)를 주면 기록된 입력보다 우선합니다.

키움 API는 과거 10호가를 제공하지 않습니다. 따라서 수집하지 않은 날짜는 합성하지 않고 `missing_weekdays`로 보고합니다(휴장일 포함). 일정·종목 입력이 없거나 휴장 일정인 날짜는 `skipped`, 재생 오류가 난 날짜는 `failed`에 사유와 함께 남습니다. 재생 가능한 날짜가 하나도 없으면 종료 코드는 1입니다.

출력 폴더(기본 `reports/runs/backtest-<시작>_<끝>/`)에는 날짜별 전체 보고서 `days/<날짜>.json`과 기간 요약 `summary.json`이 생성됩니다. 요약에 포함되는 항목은 다음과 같습니다.

- 순손익, 승률, 기대값, 손익비, 체결률
- 장중 평가를 포함한 기간 MDD, 최악·최고 일손익
- 날짜 블록 bootstrap 하한(거래가 있는 날 20일 이상)과 날짜순 60/20/20 분리 성과
- 월·종목·시간대·청산 사유별 결과, 마감 인계·중단 날짜

`--stress`는 `stress` 명령과 같은 8개 시나리오를 기간 전체에 적용합니다. 자격 상태는 계속 `UNVALIDATED`입니다.
