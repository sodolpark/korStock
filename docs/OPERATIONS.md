# 실행과 복구

## 실행 모드와 사전 입력

| 모드 | 시세 | 주문 경로 |
|---|---|---|
| replay | 저장된 JSONL 및 기록된 timer | 로컬 체결 모형 |
| collect | 키움 WebSocket | 주문 기능 없음 |
| shadow | 키움 WebSocket | 로컬 모형, 예상 성과 표시 |
| mock | 모의 환경 | 키움 모의 주문 |
| live | 실전 환경 | 검증 파일 확인 후 최대 1종목·1주 |

`--mode`는 명시적으로 선택합니다. `mock`은 mock 도메인만 허용합니다. `live`에는 동결 설정·비용 검증·성과/운영/모의 계약/스트레스 통과·20개 이상 shadow 날짜·보고서 파일 해시가 필요합니다. 코드에서도 별도로 검사하므로 `--mode live`만으로 활성화되지 않습니다.

운영 전 다음 입력이 필요합니다.

1. 키움 REST API 신청 및 해당 환경의 앱키/시크릿, 허용 IP 설정.
2. 실제 거래일의 개장·신규진입 마감·청산·종가 단일가·종료 시각. 휴장일은 `closed=true`.
3. 당일 검증한 종목별 보통주 여부, 상장일, 정상/정지/단기과열/단일가 상태, 상하한가.
4. 계정·상품·날짜별 수수료·매도세율. `configs/research.toml`은 연구 자료 설명용 예시이며 `verified=false`.

`configs/session.example.json`, `configs/instruments.example.json`은 합성 데이터 양식입니다. 거래일 확인과 근거 입력 없이 `verified`를 변경하지 않습니다. 호가단위는 현재 코드의 `KRX-common-2023-01-25` 버전으로 관리합니다. 실제 적용 규정이 바뀌면 호가단위 코드와 경계 테스트를 변경하고 검증을 다시 실행합니다.

종목 API는 상품 분류와 운영 상태에 필요한 근거를 제공하지만, 일부 값만 보고 보통주라고 추측하지 않습니다. `--ranking`은 입력 종목 목록 중 당일 검증된 종목으로 후보를 갱신합니다. 새로운 종목은 검증된 메타데이터에 추가해야 합니다.

## 읽기 연결

실제 비밀값은 파일/채팅에 적지 않고 실행 프로세스의 환경변수로 공급합니다. `.env.example`은 변수명 안내이며 자동 로드하지 않습니다.

```powershell
# 로컬 환경 또는 OS 비밀 저장소에서 다음 이름을 공급
# KIWOOM_MOCK_APP_KEY / KIWOOM_MOCK_SECRET_KEY
# KIWOOM_LIVE_APP_KEY / KIWOOM_LIVE_SECRET_KEY
.\.venv\Scripts\python -m korstocktrade --config configs/my-mock.toml probe --symbols 005930 --output data/contracts/probe.json
```

`probe`는 인증·순위·종목 정보·잔고·예수금을 읽고 비밀 필드를 제거한 결과를 저장합니다. 주문 기능은 만들지 않습니다. 결과에는 민감할 수 있는 잔고와 금액이 있으므로 `data/`는 저장소에서 제외합니다. 토큰은 메모리에만 유지합니다.

계좌별 잠금은 `data/<environment>/<account_alias>/account.lock.runtime`에 잡습니다. 같은 계좌에 다른 별칭을 사용하는 우회는 하지 않습니다. 다른 API 프로그램의 세션/호출 예산은 이 프로그램이 통제할 수 없으므로 해당 계좌를 단독 운영해야 합니다.

```powershell
.\.venv\Scripts\python -m korstocktrade --config configs/my-mock.toml run --mode collect --date YYYY-MM-DD --session configs/my-session.json --instruments configs/my-instruments.json --duration 300
.\.venv\Scripts\python -m korstocktrade --config configs/my-mock.toml run --mode shadow --date YYYY-MM-DD --session configs/my-session.json --instruments configs/my-instruments.json --ranking
```

`YYYY-MM-DD`는 실제 거래일로 교체합니다. 시스템의 KST 날짜와 일치해야 하며 온라인 일정은 `verified=true`여야 합니다. 초기에 30종목을 사용하고 실제 수신 품질을 확인한 뒤 50종목을 검토합니다. HTS 조건식이 있으면 WebSocket 어댑터의 조건 목록·일반·실시간·해제 메서드를 사용해 연결할 수 있습니다. 기본 후보 공급원은 순위/고정 목록입니다.

정상 시장 상태를 수신하기 전에는 신규 진입하지 않습니다. 장중 접속에서 `0s`가 아직 오지 않으면 `UNKNOWN`을 유지합니다. 이를 시간만으로 정상 상태로 바꾸지 않습니다. VI의 `9068=2`는 동적 VI이며 해제가 아닙니다. `1224` 이후 유효 체결과 신선한 호가가 함께 관측되어야 데이터 준비를 다시 시작합니다.

## 모의·실전 전환

읽기 연결 후 같은 구성으로 `--mode mock`을 사용해 주문·취소·체결·잔고 대사를 검증합니다. 부분체결과 VI 등 모의환경에서 재현하지 못한 경우는 로컬 장애 시험과 실제 검증의 차이를 보고서에 남깁니다.

`live_candidate`의 설정을 먼저 동결하고 `configs/qualification.example.json` 양식에 실제 검증 결과와 파일 SHA-256을 연결합니다. 설정의 `mode=live`, `profile=live_candidate`, `live_enabled=true`, `broker.environment=live`, `risk.max_positions=1`, `costs.verified=true`가 필요합니다. `check`의 설정/코드 해시와 검증 당시 값을 맞춰야 합니다. 동결 후 변경하면 다시 검증합니다. 이 저장소에는 실거래 통과 파일을 생성하지 않았습니다.

실전 명령의 형태는 다음과 같습니다. 준비가 완료되지 않은 상태에서는 검사에서 종료됩니다.

```powershell
.\.venv\Scripts\python -m korstocktrade --config configs/my-live-candidate.toml run --mode live --qualification reports/my-qualification.json --date YYYY-MM-DD --session configs/my-session.json --instruments configs/my-instruments.json
```

## 원장과 상태

`data/<environment>/<account_alias>/<date>/<mode>/` 아래에 원장 SQLite, 원시 JSONL, 설정/코드 해시, 상태 JSON, 종료 보고서와 백업이 저장됩니다. 원본 파일은 단일 실행의 순번과 단조 시계를 포함하며 이어 쓰지 않습니다. 실행마다 그날 사용한 일정·종목 입력을 `session-<run>.json`, `instruments-<run>.json`으로 로그 옆에 남깁니다. 이 파일들은 이후 기간 백테스트의 입력이 됩니다.

```powershell
.\.venv\Scripts\python -m korstocktrade status data/mock/research/YYYY-MM-DD/shadow/status.json
.\.venv\Scripts\python -m korstocktrade backup data/mock/research/YYYY-MM-DD/mock/ledger.sqlite data/backups/ledger.sqlite
```

첫 계좌 대사에서는 초기 현금/순자산 기준을 조회값으로 설정합니다. 기존 외부 잔고나 주문이 있으면 임의로 전략 포지션에 편입하지 않고 중단합니다. 이후 예상 현금과 실제 결제 기준 현금이 다르면 수수료 차이·입출금·외부 거래의 근거를 확인합니다. 입출금은 손익으로 계산하지 않습니다.

## 장애·재시작

운영자 중단은 실행 중 원장에 요청을 남깁니다. `halt`는 신규 진입과 미체결 매수를 중단하며, `--liquidate`를 추가하면 실행 중 서비스에 보유 청산도 요청합니다. 서비스가 꺼져 있으면 요청만 저장됩니다. 재시작과 대사로 운영자 중단을 자동 해제하지 않습니다.

```powershell
.\.venv\Scripts\python -m korstocktrade --config configs/my-mock.toml halt --mode mock --date YYYY-MM-DD --liquidate
```

| 상태 | 처리와 재개 조건 |
|---|---|
| WebSocket 단절 | 신규 진입 차단, 기존 원장 보존, 재로그인/재구독. 계좌 대사와 데이터 준비 후 재개 |
| 오래된 호가/VI | 해당 후보·연속 타이머 초기화. 유효 가격 없이 매도 체결을 가정하지 않음 |
| 큐 초과/처리 지연 | 원시 로그에 품질 플래그, 신규 진입 차단. 누락 구간 포함 수익성은 정상 자료로 판정하지 않음 |
| 원시 로그 저장 실패 | 신규 진입 중단. 계좌 원장이 정상일 때 기존 주문·위험 축소 처리는 계속 시도 |
| 계좌 원장 저장 실패 | 프로세스 오류와 인계 대상. 기록할 수 없는 상태에서 새 주문을 내지 않음 |
| 주문 응답 불명 | `UNKNOWN`, 재전송 금지. 미체결·당일 체결·잔고·현금 대사 |
| 일일 손실한도 | 당일 `EXIT_ONLY`를 저장. 재시작으로 해제하지 않음 |
| 정규장 종료 잔고/미체결 | `closing_handoff_required=true`. HTS/계좌 확인 후 별도 인계. NXT 이동 없음 |

복구 시작 시 저장 원장→미체결→체결→잔고/현금→구독→데이터 준비 순서를 따릅니다. 새 프로세스는 고점·보호선·진입 횟수·일일 중단만 복구하고 연속 확인 타이머는 복구하지 않습니다. 이전 날짜 원장을 다음 날에 직접 이어 쓰지 않습니다. 이전 잔고 인계와 새 거래일 기준 확인이 먼저입니다.

REST 체결 조회에 체결번호가 없으면 기존 WebSocket 체결과 안전하게 합산할 수 없습니다. 자동 대사는 차이를 기록하고 멈춥니다. 실제 조회 자료로 주문번호 연결·식별 가능한 체결·취소/거절·현금 조정을 검토한 뒤 `recover`에 JSON 증거를 제공합니다. 명령은 전송 기능이 없으며 계좌 잠금과 복구 전 백업을 사용합니다.

```powershell
.\.venv\Scripts\python -m korstocktrade --config configs/my-mock.toml recover --root data --date YYYY-MM-DD --mode mock data/reviewed-recovery.json
```

증거 필드는 `trading_date`, `account`, `observed_at`(UTC epoch seconds), `evidence_reference`, `bindings`, `identified_fills`, `terminal_orders`, `cash_adjustments`, `snapshot`입니다. 예시 구조와 중복 복구 검증은 `tests/test_recovery.py`에 있습니다. 주문 연결은 종목·매매 방향·수량·지정가까지 일치해야 하며, 원장에 이미 정해진 주문번호를 변경할 수 없습니다. 외부 거래를 처리할 때에는 독립된 원장 근거와 현금 조정을 확인해야 합니다.

## 재생의 범위

재생은 거래소 시각으로 재정렬하지 않습니다. 원시 로그의 수신 순서, 단조 시계, timer, 시장/VI/품질 이벤트를 사용합니다. 로그 여러 개를 한 파일로 이어 붙이면 run ID 검증에서 거부합니다. 같은 날짜의 실행 로그는 각각 새 프로세스처럼 재생하되(지표·확인 타이머 초기화) 계좌 원장은 이어서 사용합니다. 중간에 잘린 JSONL은 오류로 표시하며 조용히 버리지 않습니다.

`backtest`는 기간의 날짜별로 이 재생을 반복합니다. 날짜마다 새 원장과 `risk.equity`로 시작하며, 전날 잔고를 넘겨받지 않습니다. 마감 인계가 필요한 날짜는 요약의 `closing_handoff_days`로 따로 표시합니다. 기록 중일 수 있는 당일 로그는 기본 기간에서 제외합니다. 수집 공백일·입력 누락일·오류일은 각각 구분해 보고하므로, 기간 성과는 실제 재생된 날짜 수와 함께 해석합니다.

체결 모형은 다음 호가의 가용 깊이에 지정가가 닿을 때만 부분체결을 만듭니다. 수동 호가 대기열 우선순위, 종가 단일가 체결은 추정하지 않습니다. 오래된 데이터, 상하한가 잔량 없음, VI, 정지 상태에서는 미체결이 남을 수 있습니다.

`shadow`의 예상 잔고/주문이 남은 채 중단되면 보고서와 원장을 보존합니다. 이전 shadow 원장을 수동 삭제해서 실적을 이어 붙이지 않습니다. 새 관찰 실험은 새 계좌 별칭과 원장으로 구분하고 해당 구간의 불완전성을 기록합니다.
