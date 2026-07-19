# QA 통합 정합성 검증 리포트

- 대상: k8s-traffic-operator (metrics / policy / actuator + handlers + CRD)
- 검증자: qa-engineer (스킬 operator-integration-qa)
- 일자: 2026-07-19
- 방법: 경계면 양쪽 코드 동시 교차 비교 + 진입 모듈 import 검증. (py_compile은 사전 통과 확인됨)

---

## 경계면 검증 리포트

### 1) metrics -> policy (스키마 필드/단위 일치)

- [PASS] 필드 일치: `collector.py`가 채우는 `TrafficSnapshot` 필드(status, timestamp, window_seconds, rps, error_rate, p50/p95/p99_latency_ms, total_ready_pods, per_backend, meta)와 policy가 접근하는 필드(`engine.py`: status, timestamp, error_rate, p99_latency_ms, total_ready_pods, per_backend, rps / `anomaly.py`: per_backend[].error_rate, per_backend[].name / `_current_pods`: total_ready_pods, per_backend[].ready_pods)가 모두 일치. policy가 읽는데 metrics가 안 채우는 필드는 없음 (AttributeError/None 위험 없음).
- [PASS] 단위 일치: latency=ms(`collector._to_ms`가 adapter.latency_unit="s"일 때만 *1000, envoy/istio 모두 "ms"), error_rate=0.0~1.0(`_safe_ratio`가 0~1로 클램프), rps=req/s, weight=0~100 정수. schemas.py 고정 단위와 전 모듈 준수.
- [PASS] no_data/collection_failed 처리: `engine.evaluate` (A) 블록(engine.py:165)에서 `snapshot.status != "ok"` -> 즉시 `noop` 반환. handlers.py:148도 non-ok를 평가 전 선차단(이중 방어). None 지표에 산술 없음.
- [PASS] 스키마 재정의 없음: 세 모듈 모두 `from ..schemas import ...` 사용, 로컬 재정의 없음.

### 2) policy -> actuator (action 완전성 / action별 필수 필드)

- [PASS] action 완전성(치명 방향): policy가 생성 가능한 action = {noop, scale, isolate_backend} (engine.py:167,198,257,287=noop / 223=isolate_backend / 245,277=scale). 이 셋 모두 actuator에 대응 분기 존재(executor.py:110 noop, 137 scale, 176 `_do_reroute`가 isolate_backend 처리). **policy가 만드는데 actuator가 못 받는 action은 없음.**
- [PASS] 필수 필드 규약: scale Decision은 항상 `target_replicas` 세팅(engine.py:250,279, 생성 전 None/`>current` 검증) + backend_weights=None. isolate_backend는 항상 `backend_weights` 세팅(engine.py:228) + target_replicas=None. actuator도 규약 위반을 방어 검증(executor.py:159 target_replicas None 거부, 187 backend_weights 빈값 거부).
- [FINDING-1, 비치명] actuator `reroute` 분기는 죽은 분기(dead branch): policy는 `reroute`를 절대 생성하지 않음(우선순위 로직이 isolate_backend/scale/noop만 산출). 런타임 실패는 아니나, CRD/스키마가 1급 action으로 정의한 reroute(카나리/트래픽 시프트)가 구현되지 않은 설계-구현 갭. 아래 상세.

### 3) CRD -> 코드 (모든 spec 필드 소비 여부)

- [PASS] target.httpRoute/namespace/deployment: metrics(collector.py:67-70 조회 대상) + actuator(executor.py:153,181 패치 대상) 소비.
- [PASS] thresholds.targetRPSPerPod / scaleDownRPSPerPod / scaleUpErrorRate / maxP99LatencyMs: policy(engine.py:177-180) 소비.
- [PASS] actions.minReplicas / maxReplicas: policy(clamp, scaling.py:_clamp) + actuator(scaler.clamp_replicas) 소비.
- [PASS] actions.cooldownSeconds: policy(engine.py:172, `_cooldown_remaining`) 소비.
- [PASS] actions.maxScaleStep: actuator(scaler.clamp_replicas 변경폭 제한) 소비.
- [PASS] actions.allowRouteIsolation: policy(engine.py:175 격리 허용 게이트) + actuator(executor.py:196 최종 게이트) 소비.
- [PASS] window: metrics(_parse_window) + handlers(_parse_window_seconds) 소비. 정의만 되고 무시되는 필드 없음.

### 4) handlers.py 배선 (kopf 등록 + import alias)

- [PASS] 데코레이터 등록: `@kopf.on.create/update`(register_policy), `@kopf.timer interval=30`(reconcile), `@kopf.on.delete`(cleanup_policy). main.py가 handlers를 import하여 등록.
- [PASS] 호출 순서: reconcile 내부에서 collect(spec) -> status=="ok" 확인 -> evaluate(spec, snapshot, status) -> apply(spec, decision) 순으로 실제 배선(handlers.py:145,148,157,160).
- [PASS] import alias 일치: `from .metrics import collector as metrics`->`collector.collect` 존재, `from .policy import engine as policy`->`engine.evaluate` 존재, `from .actuator import executor as actuator`->`executor.apply` 존재. 시그니처 모두 일치.
- [FINDING-3, 환경/비치명] 폴백 함몰 위험 비대칭: metrics(prometheus_client가 `requests`를 import-guard) / policy(외부 의존 없음)는 런타임 의존성이 없어도 import 성공. 그러나 actuator/executor.py:37은 `from kubernetes import client, config`를 무조건 최상단 import -> `kubernetes` 미설치 시 handlers.py:39-42 try/except가 actuator를 None으로 삼켜 조용히 `_fallback_apply`(no-op)로 대체. 아래 상세.

### 5) 안전장치 실제 구현 여부

- [PASS] Cooldown(policy): `_cooldown_remaining`(engine.py:76) — cooldown_until 경로 + lastReconcileAt+action 폴백 경로 이중 판단. 억제 noop 시 `_carry_cooldown_until`로 종료 시각 보존(engine.py:205)하여 다중 reconcile 간 지속. 실제 구현됨.
- [PASS] Hysteresis(policy): scaling.py 업=targetRPSPerPod 초과(89-93), 다운=scaleDownRPSPerPod 미만(106-110), 밴드 내부는 유지. 서로 다른 임계값 사용 확인.
- [PASS] 변경폭 제한(actuator): scaler.clamp_replicas maxScaleStep(scaler.py:48-52) + router.clamp_weight_change MAX_WEIGHT_DELTA_PER_RECONCILE=30pp(router.py:44-50). 둘 다 구현.
- [PASS] Idempotency(actuator): scaler final==current -> "skipped"(scaler.py:91) + patch 없음, router changed==False -> "skipped"(router.py:169) + replace 생략.
- [PASS] no_data/collection_failed -> noop(policy): engine.py:165 확인(handlers 이중 차단).

---

## 발견된 결함 목록

### FINDING-1 (심각도: 낮음 / 비치명, 설계-구현 갭)
- 위치: policy/engine.py (전체 우선순위 로직) vs actuator/executor.py:117,176-207, schemas.py:44/117, crds/trafficpolicy.yaml, 설계문서 §2.3
- 설명: `Decision.action`의 4개 값 중 `reroute`를 policy 엔진이 어떤 경로에서도 생성하지 않는다. actuator에는 `reroute` 처리 경로(_do_reroute, allowRouteIsolation 게이트 없이 weight patch)가 존재하나 도달 불가능한 죽은 분기다.
- 재현 시나리오: 카나리/점진 트래픽 시프트를 의도해도 policy가 reroute Decision을 내지 않으므로 해당 기능이 동작하지 않는다. 단, 잘못된 action이 실행되거나 예외가 나지는 않는다(치명 아님).
- 제안 수정: 아래 둘 중 하나를 architect가 결정.
  (a) reroute를 계약에서 유지한다면 policy에 카나리/트래픽 시프트 판단 경로를 구현.
  (b) 현 범위에서 불필요하면 ActionType에서 reroute를 제거하고 actuator의 reroute 분기도 정리하여 계약과 구현을 일치.

### FINDING-2 (심각도: 정보성, 죽은 데이터)
- 위치: schemas.py:87-88(정의) / metrics/collector.py:139-140,163(생산) / policy 소비 없음
- 설명: `p50_latency_ms`, `p95_latency_ms`를 metrics가 채우지만 policy 로직은 `p99_latency_ms`만 사용(anomaly.py:108,115). p50/p95는 로깅/진단 외 소비처가 없음.
- 재현 시나리오: 기능적 문제 없음. 스킬 체크리스트 (b)"채우는데 아무도 읽지 않는 필드" 해당. 향후 정책 확장 여지로 남겨둘 수 있음.
- 제안 수정: 의도된 확장 여지면 유지(조치 불필요). 아니면 수집 비용 절감을 위해 생산 생략 검토.

### FINDING-3 (심각도: 낮음 / 환경 의존, by-design이나 관측성 개선 권장)
- 위치: actuator/executor.py:37 (`from kubernetes import client, config`) + handlers.py:39-42
- 설명: actuator만 런타임 의존성(kubernetes)을 최상단에서 무조건 import한다. 미설치 환경에서 handlers의 방어적 try/except가 actuator를 None으로 흡수해 조용히 no-op 폴백으로 동작 -> "오퍼레이터는 떠 있으나 실제 액추에이션이 비활성" 상태가 로그 없이 발생 가능. (검증 환경에서 실제로 `kubernetes` 미설치로 executor import 실패 재현; metrics/policy는 정상 import.)
- 재현 시나리오: 프로덕션 이미지에 kubernetes 미포함/버전 충돌 시, snapshot이 ok여도 모든 대응이 dry-run 폴백으로 처리되고 아무것도 실행되지 않는데 경고가 없음.
- 제안 수정: (1) requirements.txt에 kubernetes 명시되어 있으므로 정상 배포에선 문제없음(그대로 by-design 허용 가능). (2) 관측성 개선으로 handlers 또는 main.startup에서 metrics/policy/actuator 각각이 실제 모듈인지 폴백인지 startup 시 1줄 로깅 권장(폴백 함몰 조기 감지).

---

## 최종 요약

**전체 통과: 5개 경계면(metrics->policy, policy->actuator, CRD->코드, handlers 배선, 안전장치) 모두 PASS. 치명적(FAIL) 결함 없음.** 발견 항목 3건은 모두 비치명(reroute 죽은 분기 1건 = 설계-구현 갭, 정보성 2건)이며 런타임 실패/오작동을 유발하지 않는다. FINDING-1(reroute)은 계약 유지/축소 여부를 operator-architect가 결정할 사항이다.

---

## 후속 조치: FINDING-1 해결 (2026-07-19)

사용자 결정에 따라 `policy/engine.py`에 reroute 생성 로직을 구현했다 (isolate_backend의 복구 대칭 짝: 이상 해소 후 격리됐던 backend weight를 점진적으로 healthy(100)까지 복구).

구현 중 자체 시뮬레이션(비-API 단위 테스트, `python3` 인라인 스크립트)으로 2건의 결함을 추가로 발견·수정했다:

1. **복구 조기 중단 버그**: reroute의 backend_weights는 항상 "목표값"(100)을 담으므로, weight<100 여부만으로 다음 cycle의 복구 지속 여부를 판단하면 1 cycle 만에 멈춘다. `Decision.reason`에 `[recovery done=D total=T backends=...]` 마커를 실어 진행 상태를 자체 추적하도록 수정(`_parse_recovery_marker`/`_recovery_state`).
2. **cooldown-복구 상호작용 버그**: (a) `cooldownSeconds>0`일 때 cooldown 폴백 경로가 reroute도 "실제 액션"으로 취급해 매 복구 cycle마다 재차 cooldown을 걸어 수렴을 극단적으로 지연시킴 → reroute를 cooldown 폴백 대상에서 제외. (b) 최초 isolate_backend 이후 cooldown이 여러 cycle 지속되면 그 사이의 cooldown-noop들이 `backend_weights`를 보존하지 않아 cooldown이 풀린 뒤 복구 이력 자체가 유실되어 영원히 시작되지 않음 → cooldown-noop이 `backend_weights`를 그대로 carry-forward하도록 수정(`_carry_backend_weights`), `_recovery_state`가 noop에 실린 degraded weight도 인식하도록 일반화.

검증: `cooldownSeconds=0`/`120` 두 시나리오, 복구 도중 이상 재발 시나리오를 인라인 스크립트로 재현하여 모두 의도대로 동작함을 확인(`python3 -m py_compile` 전체 통과 포함). 실제 클러스터 대상 통합 테스트는 미수행(권한 필요 작업, 범위 밖).

**갱신된 최종 판정: 전체 PASS, FINDING-1 해결 완료. 남은 FINDING-2(정보성)/FINDING-3(환경/관측성 권장)은 그대로 유효.**

---

## 후속 조치: nginx Gateway Fabric 어댑터 추가 (2026-07-19)

실제 클러스터 검증을 시도하는 과정에서 이 클러스터의 실제 Gateway API 구현체가 nginx Gateway
Fabric(GatewayClass: nginx, controller gateway.nginx.org)임을 확인했으나, 기존 어댑터는
Envoy Gateway/Istio만 지원했다. 사용자 결정에 따라 `metrics/adapters/nginx_gateway_fabric.py`를
추가했다(코드 구현 + mock 검증, 클러스터에 실제 리소스는 생성하지 않음 — 아래 "실제 클러스터
검증 범위와 한계" 참조).

**실측으로 확인한 제약**: 이 클러스터의 nginx-gateway 데이터플레인에는 stub_status/메트릭
location이 구성되어 있지 않아, 이 어댑터가 가정하는 `nginxplus_http_*` 메트릭(nginx-prometheus-exporter
컨벤션)이 실제로는 노출되지 않는다. 또한 NGINX Plus API는 지연시간 percentile을 제공하지
않고 평균 응답시간만 제공하므로, 이 어댑터는 p50만 근사치로 채우고 p95/p99는 의도적으로
None을 반환한다(평균을 percentile로 위장하지 않기 위함) — 이 어댑터를 쓰는 배포에서는
지연시간 기반 이상탐지가 비활성 상태가 된다.

**검증 방법**: `PrometheusClient.query`를 mock으로 대체해 `collector.collect()` →
`policy.evaluate()`까지 파이프라인 전체를 인라인 스크립트로 실행. 정상 트래픽(RPS/에러율/
backend 매핑) 시나리오와 no_data 시나리오(빈 결과 → status="no_data" → noop) 모두 확인.
실제 클러스터 대상 E2E는 수행하지 않았다(범위 밖 — 위 제약 참조).

## 후속 조치: pytest 정식 회귀 테스트 스위트 (2026-07-19)

이 문서 앞부분과 검증 대화 중 인라인 스크립트로 임시로 돌렸던 모든 검증을
`operator/tests/`에 pytest 스위트로 정식화했다. 총 116개 테스트, 10개 파일, 1,380줄.

| 파일 | 테스트 수 | 대상 |
|------|----------|------|
| `test_schemas.py` | 5 | 공유 계약(TrafficSnapshot/Decision/ActuationResult) 기본값·직렬화 |
| `test_policy_scaling.py` | 10 | hysteresis 스케일업/다운, 결측치 방어, capacity_target_for_anomaly |
| `test_policy_baseline.py` | 8 | EWMA warmup, upper_breach/z-score, target별 baseline 격리 |
| `test_policy_anomaly.py` | 9 | 정적+EWMA 조합 severity 산정, culprit backend 판별 |
| `test_policy_engine.py` | 17 | cooldown(양쪽 경로+reroute 제외), isolate_backend, **reroute 복구(다회 cycle, cooldown 간섭 생존, 이상 재발 시 재격리)** — 실제 발견했던 버그 2건의 회귀 테스트 포함 |
| `test_metrics_adapters.py` | 24 | 3개 어댑터 계약 키, 레지스트리/별칭, map_backend_name 휴리스틱 |
| `test_metrics_collector.py` | 7 | collect() 파이프라인 happy path, no_data, collection_failed, nginx 어댑터의 p95/p99=None 설계 |
| `test_actuator_scaler.py` | 10 | clamp_replicas, scale_deployment(idempotent/dry-run/API 에러) |
| `test_actuator_router.py` | 12 | clamp_weight_change, set_backend_weights(409 재시도, v1beta1 폴백, 값 검증) |
| `test_actuator_executor.py` | 14 | Decision→실행 dispatch, allowRouteIsolation 게이트가 isolate_backend에만 적용됨(reroute는 미적용) 확인 |

**실행 방법**: `cd operator && python3 -m venv .venv && ./.venv/bin/pip install pytest kopf kubernetes && ./.venv/bin/pytest` (pytest.ini의 `pythonpath = .`로 별도 PYTHONPATH 설정 불필요).

**mock 전략**: kubernetes client는 `unittest.mock.MagicMock`으로 대체(실제 클러스터 접근 없음).
Prometheus는 `PrometheusClient`의 `session` 생성자 인자(테스트 확장점으로 이미 설계돼 있었음)에
가짜 HTTP 세션을 주입해 실제 응답 파싱 경로까지 포함해 검증했다. `policy.baseline`의 프로세스
전역 상태는 `conftest.py`의 autouse 픽스처로 매 테스트 전후 초기화한다.

**한계**: 여전히 실제 Kubernetes API 서버·Prometheus·kopf 이벤트 루프를 띄운 end-to-end 테스트는
아니다(전부 mock/단위 수준). 실제 클러스터 통합 테스트는 위 "실제 클러스터 검증 범위와 한계"
절에서 설명한 대로 범위 밖으로 남아 있다.

## 실제 클러스터 검증 범위와 한계

이 세션에서 실제 클러스터(kubernetes-admin, cluster-admin 권한)에 접근 가능함을 확인했으나,
**해당 클러스터에 리소스를 생성하는 실제 E2E 검증은 사용자 결정에 따라 수행하지 않았다.**
수행한 것은 클러스터 상태의 읽기 전용 조회(GatewayClass/Gateway/네임스페이스/메트릭 엔드포인트
확인)뿐이다. 따라서 이 문서의 PASS 판정은 전부 **정적 분석 + mock 기반 단위 검증** 수준이며,
실제 클러스터에 CRD/CR/RBAC/워크로드를 배포한 상태에서의 kopf reconcile 동작, 실제 Kubernetes
API 서버와의 인증/RBAC 상호작용, 실제 Prometheus 스크래핑 파이프라인은 검증되지 않았다.
