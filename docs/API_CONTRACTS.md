# 키움 연결 계약

공식 상세 명세를 조회해 필드/요청 스키마를 구현했습니다. 브라우저 도구에서 접근되지 않은 상세 페이지는 동일 공식 URL을 HTTPS로 직접 읽었습니다. 실제 계정 응답·권한·모의 지원 범위까지 확인한 것은 아닙니다. `scripts/fetch_contracts.py`로 공개 문서 캐시를 다시 만들 수 있습니다. 실제 응답 검증은 `probe`와 모의 주문 단계에서 수행합니다.

| 기능 | API | 구현상 계약 |
|---|---|---|
| 인증/폐기 | au10001/au10002 | `/oauth2/token`, `/oauth2/revoke`, 만료 KST, 메모리 토큰, 환경변수 비밀값 |
| 순위 | ka10032 | `/api/dostk/rkinfo`, `stex_tp=1` |
| 조건검색 | ka10171~ka10174 | WebSocket `CNSRLST/CNSRREQ/CNSRCLR`, `stex_tp=K`, 실시간 최대 10식 |
| 시장 체결 | 0B | `10` 가격 절댓값, **`15` 부호로 주도 방향**; `13` 누적량과 `228` API 강도 별도 저장 |
| 10호가 | 0D | 매도 가격 `41..50`, 매수 가격 `51..60`, 매도 잔량 `61..70`, 매수 잔량 `71..80` |
| 주문 | 00 | `9203` 주문번호, `904` 원주문, `909` 체결번호, `914/915` 단위 체결가/수량, `902` 미체결 |
| 잔고 | 04 | 원장 검증을 요청하며 보유량에 다시 더하지 않음 |
| 시장 시간 | 0s | `215`: 0 장전, 3 장시작, 2 종가 단일가 알림, 4/8 정규장 종료 |
| VI | 1h | `9068`은 정적/동적/복합/연장 구분, `1224` 해제시각; 2를 해제로 해석하지 않음 |
| 주문/취소/정정 | kt10000~kt10003 | `/api/dostk/ordr`, `dmst_stex_tp=KRX`, 지정가 `trde_tp=0` |
| 미체결/체결 | ka10075/ka10076 | `/api/dostk/acnt`, `stex_tp=1`, 연속조회 완료 후 대사 |
| 잔고/예수금 | kt00018/kt00001 | 잔고 `dmst_stex_tp=KRX`, 현금 주문가능 `100stk_ord_alow_amt`, 결제 기준 현금 `d2_entra` |
| 종목/VI 조회 | ka10099/ka10100/ka10001/ka10054 | 읽기 허용 API. 분류·날짜·정상 상태가 미확정이면 진입 불가 |

주문은 초당 5회, 조회는 별도 초당 5회, 모의는 TR별 초당 1회 상한을 설정으로 관리합니다. 취소/매도/대사를 순위/신규 매수보다 우선합니다. 조회 재시도는 제한적으로 수행하고, 주문·취소·정정은 자동 재시도하지 않습니다. 취소 REST 접수와 실제 취소 확인을 구분합니다. 운영 코어의 재가격 방식은 취소 확인 후 남은 수량 재주문입니다. 정정 메서드는 별도 계약 검증용으로 제공하며 기본 루프에서는 사용하지 않습니다.

`_NX`, `_AL`은 KRX로 변환하지 않습니다. 상품 접두어 `A`는 문서화한 계좌/조건 응답에서만 허용합니다. 원시 로그의 토큰·앱키·시크릿·인증 헤더·계좌번호는 제거합니다. 로그의 로컬 순번은 수신 순서만 증명하며 거래소의 전체 체결 수신을 증명하지 않습니다.

공식 출처:

- [시장 체결 0B](https://openapi.kiwoom.com/guide/apiGuideContents/14/0B)
- [10호가 0D](https://openapi.kiwoom.com/guide/apiGuideContents/14/0D)
- [주문 체결 00](https://openapi.kiwoom.com/guide/apiGuideContents/14/00)
- [VI 1h](https://openapi.kiwoom.com/guide/apiGuideContents/14/1h)
- [장시작시간 0s](https://openapi.kiwoom.com/guide/apiGuideContents/14/0s)
- [매수 주문 kt10000](https://openapi.kiwoom.com/guide/apiGuideContents/13/kt10000)
- [정정 kt10002](https://openapi.kiwoom.com/guide/apiGuideContents/13/kt10002), [취소 kt10003](https://openapi.kiwoom.com/guide/apiGuideContents/13/kt10003)
- [순위 ka10032](https://openapi.kiwoom.com/guide/apiGuideContents/05/ka10032)
- [미체결 ka10075](https://openapi.kiwoom.com/guide/apiGuideContents/08/ka10075), [체결 ka10076](https://openapi.kiwoom.com/guide/apiGuideContents/08/ka10076)
- [잔고 kt00018](https://openapi.kiwoom.com/guide/apiGuideContents/08/kt00018), [예수금 kt00001](https://openapi.kiwoom.com/guide/apiGuideContents/08/kt00001)
- [키움 서비스 한도](https://openapi.kiwoom.com/intro?dummyVal=0)
- [KRX 거래시간·VI](https://regulation.krx.co.kr/contents/RGL/03/03010409/RGL03010409.jsp)

주의할 미확정 계약: 재접속 직후 체결 통보 범위, 주문 통보의 누락/중복 패턴, 실제 수수료 정산 단위, 계정별 모의 지원 차이. 체결번호 없는 REST 행은 감사 자료로만 사용하고 `cntr_qty`를 추정 증분 체결로 더하지 않습니다. 동일 주문의 복수 REST 행도 합산을 추측하지 않고 대사를 보류합니다.
