# 01. Architect Design — Kubernetes 트래픽 기반 자동운영 오퍼레이터

작성: operator-architect · 대상 독자: metrics-collector-dev, policy-engine-dev, actuator-dev, qa-engineer

이 문서는 세 개발자가 **병렬로** 작업하기 위한 단일 계약서다. 실시간 대화가 불가능하므로,
인터페이스에 대한 모든 질문의 답을 여기서 찾을 수 있어야 한다. 코드 계약의 정본(正本)은
`operator/k8s_traffic_operator/schemas.py`이며, 이 문서와 코드가 충돌하면 **코드가 우선**한다.

---

## 0. 존재 이유 (읽고 시작할 것)

이 오퍼레이터는 CPU/메모리 같은 리소스 지표가 아니라, Gateway API 뒤에서 관측되는
**서비스 트래픽 지표(RPS / 에러율 / 지연시간)**를 판단 기준으로 스케일링·라우팅 제어·이상
대응을 수행한다. 따라서:

- CRD와 schemas 어디에도 `cpu`/`memory` 임계값을 두지 않는다. 스케일 판단의 입력은 항상 RPS/에러율/지연시간이다.
- flapping 방지(cooldown, hysteresis)는 CRD 레벨에서 강제된다 — policy 엔진이 무시할 수 없다.

## 1. 프로젝트 레이아웃

```
operator/
├── k8s_traffic_operator/
│   ├── __init__.py        # API_GROUP/API_VERSION/CRD_PLURAL 상수 정의
│   ├── main.py            # kopf 엔트리포인트 (kopf run -m k8s_traffic_operator.main)
│   ├── handlers.py        # @kopf.on.create/update/delete, @kopf.timer(reconcile)
│   ├── schemas.py         # ★ 공유 계약: TrafficSnapshot, Decision, ActuationResult ★
│   ├── metrics/           # metrics-collector-dev → collector.py: collect(spec)
│   ├── policy/            # policy-engine-dev    → engine.py:    evaluate(spec, snapshot, status)
│   └── actuator/          # actuator-dev         → executor.py:  apply(spec, decision)
├── crds/trafficpolicy.yaml
├── tests/
└── requirements.txt
```

CRD 좌표는 `__init__.py`에 상수로 고정: `group=ops.example.com`, `version=v1alpha1`, `plural=trafficpolicies`.

---

## 2. 모듈 간 데이터 계약 (schemas.py)

세 팀은 각자 스키마를 **정의하지 말고** `from k8s_traffic_operator.schemas import ...`로 가져다 쓴다.

### 2.1 단위 고정 — 위반 금지

| 항목 | 단위 | 타입 | 범위/비고 |
|---|---|---|---|
| latency (모든 p50/p95/p99) | **밀리초 ms** | float | 초(s) 아님 |
| error_rate | **비율 0.0~1.0** | float | 퍼센트(%) 아님. 5% = 0.05 |
| rps | **req/s** | float | 전체 backend 합산 |
| timestamp | **Unix epoch 초, UTC** | float | `time.time()` |
| window_seconds / cooldown | **초 seconds** | int | 분(min) 아님 |
| backend weight | **0~100 정수** | int | Gateway API HTTPRoute backendRef weight 규약 |

단위 불일치는 가장 흔한 경계면 버그다. metrics는 어떤 구현체에서 왔든 위 단위로 **정규화**해서 넘긴다.

### 2.2 TrafficSnapshot — metrics 생산, policy 소비

한 시점의 트래픽 관측. **구현체 중립**이다. Envoy Gateway / Istio 등 구현체 특정 필드
(envoy_cluster, istio_revision 등)는 넣지 않는다 — 그런 매핑은 metrics 모듈 내부에만 존재한다.

| 필드 | 타입 | 의미 |
|---|---|---|
| `status` | `"ok"｜"no_data"｜"collection_failed"` | **먼저 확인.** ok일 때만 지표가 채워짐 |
| `timestamp` | float | 수집 시각 (epoch 초) |
| `window_seconds` | int | 집계 윈도우 길이 (CRD `spec.window`에서 파생) |
| `rps` | Optional[float] | 전체 초당 요청 수 |
| `error_rate` | Optional[float] | 전체 에러율 0.0~1.0 (5xx/전체) |
| `p50/p95/p99_latency_ms` | Optional[float] | 전체 지연시간 (ms) |
| `total_ready_pods` | Optional[int] | 대상 Deployment의 Ready 파드 수 → **RPS/pod 계산의 분모** |
| `per_backend` | List[BackendTraffic] | backend별 분해 지표 (격리 판단용) |
| `meta` | Dict[str,str] | 진단용 자유 메타. **로직에 쓰지 말 것**(로깅만) |

`BackendTraffic`: `name`(HTTPRoute backendRef 이름과 동일해야 actuator 매칭 가능), `rps`, `error_rate`, `p99_latency_ms`, `ready_pods`.

**규약:** status가 `ok`가 아니면 지표 필드는 모두 `None`. policy는 None에 산술을 적용하지 않는다.
handlers가 이미 non-ok 스냅샷은 평가 없이 걸러내지만, policy도 방어적으로 재확인 권장.

### 2.3 Decision — policy 생산, actuator 소비

| 필드 | 타입 | 의미 |
|---|---|---|
| `action` | `"noop"｜"scale"｜"reroute"｜"isolate_backend"` | 액션 종류 |
| `reason` | str | 사람이 읽는 판단 근거. 예: `"p99=920ms > maxP99LatencyMs=800"` |
| `target_replicas` | Optional[int] | scale일 때 **필수**. 목표 replica **절대값** |
| `backend_weights` | Optional[Dict[str,int]] | reroute/isolate_backend일 때 **필수**. `{backend명: weight 0~100}` |
| `severity` | `"none"｜"warning"｜"critical"` | 이상 심각도 |
| `anomaly_score` | Optional[float] | 이상 점수(z-score 등). 해석은 policy 소유 |
| `cooldown_until` | Optional[float] | 다음 대응 억제 종료 시각(epoch 초). 선택 |

**action별 필드 규약:**
- `noop` → target_replicas / backend_weights 무시
- `scale` → target_replicas 필수, backend_weights None
- `reroute` → backend_weights 필수, target_replicas None
- `isolate_backend` → backend_weights 필수 (격리 대상 weight를 낮춘 맵)

`target_replicas`는 **절대값**이다. actuator가 `minReplicas~maxReplicas`로 clamp하고 `maxScaleStep`으로 변경폭을 제한한다. 즉 policy는 "이상적인 목표"를 내고, actuator가 "최종 방어선"이다.

### 2.4 ActuationResult — actuator 생산, handlers 소비

`applied`(bool), `action`(에코백), `detail`(예: `"replicas 4 -> 6"`), `dry_run`(bool), `error`(Optional[str]).
실패해도 **예외를 밖으로 던지지 말 것** — timer 안정성을 위해 error 필드에 담아 반환.

---

## 3. TrafficPolicy CRD (crds/trafficpolicy.yaml)

`spec`은 네 영역: **target / thresholds / actions / window**.

```yaml
spec:
  target:
    httpRoute: checkout-route      # 관측 대상 HTTPRoute
    namespace: shop                # 생략 시 CR과 동일 ns
    deployment: checkout-service   # 스케일 대상 Deployment
  thresholds:
    targetRPSPerPod: 50            # 파드당 목표 RPS. 초과 시 스케일업 신호
    scaleDownRPSPerPod: 20         # 이보다 낮을 때만 축소 (hysteresis 밴드)
    scaleUpErrorRate: 0.05         # 에러율 상한 (비율 0~1). 5% 초과 시 이상
    maxP99LatencyMs: 800           # p99 상한 (ms)
  actions:
    minReplicas: 2                 # 축소 하한 (clamp)
    maxReplicas: 20                # 확대 상한 (clamp)
    cooldownSeconds: 120           # 대응 후 최소 대기 (flapping 방지, 필수)
    maxScaleStep: 4                # 1회 reconcile당 최대 replica 변경폭 (선택)
    allowRouteIsolation: true      # 문제 backend weight 격리 허용 여부
  window: "1m"                     # 집계 윈도우 (pattern: ^[0-9]+(ms|s|m|h)$)
```

**CRD 필드 → 소비 모듈 매핑** (QA 검증 포인트):

| 필드 | 소비 모듈 |
|---|---|
| target.* | metrics(조회 대상), actuator(패치 대상) |
| targetRPSPerPod, scaleDownRPSPerPod, scaleUpErrorRate, maxP99LatencyMs | policy |
| minReplicas, maxReplicas, maxScaleStep | actuator (clamp/변경폭) |
| cooldownSeconds | policy (억제 판단) |
| allowRouteIsolation | actuator (격리 허용 게이트) |
| window | metrics(집계), handlers(window_seconds 파생) |

**hysteresis:** 스케일업은 `targetRPSPerPod` 초과, 스케일다운은 `scaleDownRPSPerPod` 미만. 두 임계값 사이 밴드에서는 replica를 유지 → flapping 방지.

---

## 4. kopf 핸들러 흐름 (handlers.py)

세 핸들러가 등록된다:

1. **`register_policy`** (`@kopf.on.create` + `@kopf.on.update`) — 스펙 검증만. 필수 필드
   (target.deployment/httpRoute, thresholds.targetRPSPerPod) 누락 시 `PermanentError`,
   minReplicas>maxReplicas 검증. 실제 reconcile은 하지 않는다.

2. **`reconcile`** (`@kopf.timer`, interval=30s) — 오퍼레이터의 심장. 배선:

   ```python
   snapshot = metrics.collect(spec)                      # 1) 수집
   if snapshot.status != "ok": return ...                # non-ok면 평가 생략
   decision = policy.evaluate(spec, snapshot, status)    # 2) 평가 (cooldown/hysteresis 책임)
   result   = actuator.apply(spec, decision)             # 3) 실행 (clamp/변경폭/dry-run 책임)
   return {... status에 lastDecision/lastActuation 기록 ...}
   ```

   **interval(30s) ≤ window** 관계를 지킬 것 — timer가 window보다 자주 돌아야 반응성이 확보된다.

3. **`cleanup_policy`** (`@kopf.on.delete`) — 관리 중단 로깅(향후 정리 훅).

**폴백(stub) 구조:** 미완성 모듈은 handlers 상단 try/except import에서 `None`이 되고,
`_fallback_collect/evaluate/apply`로 대체된다. 따라서 세 팀 중 누구든 자기 모듈만 먼저
완성해도 오퍼레이터는 크래시 없이 뜬다. **각 팀은 아래 진입 함수를 정확히 이 시그니처로 구현할 것:**

| 모듈 파일 | 함수 시그니처 | handlers의 import 별칭 |
|---|---|---|
| `metrics/collector.py` | `collect(spec: dict) -> TrafficSnapshot` | `from .metrics import collector as metrics` |
| `policy/engine.py` | `evaluate(spec: dict, snapshot: TrafficSnapshot, status: dict) -> Decision` | `from .policy import engine as policy` |
| `actuator/executor.py` | `apply(spec: dict, decision: Decision) -> ActuationResult` | `from .actuator import executor as actuator` |

`spec`은 kopf가 넘기는 dict(= CRD `spec` 하위). `.get()`으로 접근. `status`는 직전 reconcile이
남긴 dict(`lastReconcileAt`, `lastDecision` 등 포함) — policy가 cooldown 판단에 활용.

---

## 5. 팀별 작업 지침 요약

### metrics-collector-dev (`metrics/collector.py`)
- Prometheus HTTP API에서 RPS/에러율/p50·p95·p99를 PromQL로 조회, ms/비율/req·s로 정규화.
- 구현체(Envoy Gateway/Istio) 메트릭 이름 매핑은 모듈 내부에만. **TrafficSnapshot은 중립 유지.**
- `total_ready_pods`는 policy의 RPS/pod 분모 → 반드시 채운다(불가 시 None + status 로깅).
- 데이터 없음 → `no_data`, 접근 실패 → `collection_failed`. 예외를 handlers로 던지지 말 것.
- per_backend의 `name`은 HTTPRoute backendRef 이름과 정확히 일치시킬 것(actuator 매칭 키).

### policy-engine-dev (`policy/engine.py`)
- 입력 TrafficSnapshot(ok) + thresholds로 판단. RPS/pod = `rps / total_ready_pods`.
- hysteresis: 업은 targetRPSPerPod 초과, 다운은 scaleDownRPSPerPod 미만.
- cooldown: `status`의 직전 결정 시각과 `cooldownSeconds`로 억제, 억제 시 `noop`.
- 이상 탐지(EWMA/z-score) → severity/anomaly_score. target_replicas는 절대값.

### actuator-dev (`actuator/executor.py`)
- kubernetes python client로 Deployment replica 패치, HTTPRoute backendRefs weight 패치.
- **안전장치 필수:** min/max clamp, `maxScaleStep` 변경폭 제한, dry-run, 실패 시 롤백/에러 반환.
- `allowRouteIsolation=false`면 isolate_backend 거부(applied=False). 예외를 던지지 말 것.

---

## 6. 가정 (Assumptions)

사용자 요구사항이 미지정이라 아래를 기본값으로 가정하고 진행함. 이견 시 이 문서를 개정한다.

1. **Gateway API 구현체 미정** → schemas는 구현체 중립. 구현체 선택은 metrics 모듈 내부 문제로 격리.
2. **reconcile interval = 30s** (window 기본 1m보다 짧게). window 기반 동적 조정은 후속 과제.
3. **CRD group = `ops.example.com`, version = `v1alpha1`** (skill 예시 준수). scope=Namespaced.
4. **backend weight는 상대 가중치**(합=100 강제 안 함) — Gateway API 규약. 검증은 actuator.
5. Prometheus는 클러스터 내 접근 가능하다고 가정(엔드포인트는 metrics 모듈 설정 대상).

---

## 7. QA 검증 포인트 (qa-engineer용)

- [ ] 세 모듈이 자기 스키마를 재정의하지 않고 schemas.py를 import하는가.
- [ ] 단위 준수: latency=ms, error_rate=0~1, weight=0~100 정수.
- [ ] CRD 모든 필드에 소비 코드가 존재하는가(§3 매핑표 기준). 미소비 필드/미정의 참조 없는지.
- [ ] handlers의 metrics→policy→actuator 배선이 살아있는가(폴백에 갇히지 않았는가).
- [ ] flapping 안전장치가 실제 구현되었는가: policy=cooldown/hysteresis, actuator=clamp/maxScaleStep.
- [ ] Decision.action별 필수 필드 규약(§2.3)을 policy가 지키고 actuator가 검증하는가.
- [ ] non-ok 스냅샷에서 policy가 None 지표에 산술을 하지 않는가.
