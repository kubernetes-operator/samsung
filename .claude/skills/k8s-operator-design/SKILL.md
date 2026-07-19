---
name: k8s-operator-design
description: "Python kopf 기반 Kubernetes Operator의 아키텍처를 설계한다. TrafficPolicy 같은 CRD 스키마 설계, kopf 핸들러(@kopf.on.create/update, @kopf.timer) 구조, 모듈 간 데이터 인터페이스(dataclass/pydantic) 정의, 프로젝트 스캐폴딩 생성에 사용. 오퍼레이터를 새로 설계하거나 CRD 필드를 추가/변경하거나 kopf 핸들러 구조를 조정할 때 반드시 이 스킬을 사용할 것."
---

# Kubernetes Operator 설계 (kopf 기반)

트래픽 기반 자동운영 오퍼레이터의 CRD와 kopf 구조를 설계하는 절차.

## 왜 CPU/메모리가 아닌 CRD 설계가 중요한가

기존 HPA는 리소스 사용률을 기준으로 스케일링한다. 이 오퍼레이터는 그 대신 **서비스가 실제로 처리하는 트래픽**(RPS, 에러율, 지연시간)을 판단 기준으로 삼는다. 이 차이가 CRD 스키마에 그대로 드러나야 한다 — `targetCPUUtilization` 같은 필드 대신 `targetRPSPerPod`, `maxErrorRate`, `maxP99LatencyMs` 같은 트래픽 지표 필드가 스펙의 중심이어야 한다.

## TrafficPolicy CRD 설계 원칙

CRD의 spec은 다음 네 영역을 반드시 포함한다:

1. **대상(target)** — 어떤 HTTPRoute/Gateway와 그 뒤의 어떤 Deployment(들)를 관리할지
2. **트래픽 임계값(thresholds)** — RPS/pod 목표치, 에러율 상한, p99 지연시간 상한. 스케일업/다운 임계값을 분리(hysteresis)하여 flapping을 CRD 레벨에서 방지
3. **대응 정책(actions)** — 스케일 범위(min/max replicas), 라우팅 격리 허용 여부, cooldown 기간
4. **관측 윈도우(window)** — 메트릭을 얼마의 시간 윈도우로 집계할지 (예: `1m`, `5m`)

```yaml
apiVersion: ops.example.com/v1alpha1
kind: TrafficPolicy
metadata:
  name: checkout-traffic-policy
spec:
  target:
    httpRoute: checkout-route
    namespace: shop
    deployment: checkout-service
  thresholds:
    targetRPSPerPod: 50
    scaleUpErrorRate: 0.05      # 5% 초과 시 이상 신호
    scaleDownRPSPerPod: 20      # 스케일다운은 더 낮은 임계값 (hysteresis)
    maxP99LatencyMs: 800
  actions:
    minReplicas: 2
    maxReplicas: 20
    cooldownSeconds: 120
    allowRouteIsolation: true    # 에러율 급증 시 해당 backend weight를 낮출 수 있는지
  window: "1m"
```

이 필드들은 임의가 아니다 — `policy-engine-dev`의 스케일링/이상탐지 로직과 `actuator-dev`의 안전장치(cooldown, 변경폭 제한)가 정확히 이 필드들을 소비한다. CRD 필드를 추가하면 반드시 소비하는 코드가 있는지 QA 단계에서 확인한다.

## kopf 핸들러 구조

```python
# handlers.py
import kopf

@kopf.on.create('ops.example.com', 'v1alpha1', 'trafficpolicies')
@kopf.on.update('ops.example.com', 'v1alpha1', 'trafficpolicies')
def register_policy(spec, name, namespace, **kwargs):
    # 검증 + 내부 상태 등록. 실제 reconcile은 timer가 수행한다.
    ...

@kopf.timer('ops.example.com', 'v1alpha1', 'trafficpolicies', interval=30)
def reconcile(spec, status, name, namespace, **kwargs):
    snapshot = metrics.collect(spec)      # metrics-collector-dev 모듈
    decision = policy.evaluate(spec, snapshot, status)  # policy-engine-dev 모듈
    actuator.apply(spec, decision)        # actuator-dev 모듈
    return {'lastDecision': decision.to_dict()}
```

`@kopf.timer`의 `interval`은 CRD의 `window`보다 짧아야 반응성이 확보된다. reconcile 함수가 세 모듈을 순서대로 호출하는 이 배선이 전체 시스템을 연결하는 지점이므로, 어떤 모듈이 개별적으로 완벽해도 이 배선이 빠지면 오퍼레이터는 동작하지 않는다 — QA가 반드시 확인해야 할 지점이다.

## 인터페이스 스키마 정의 (schemas.py)

세 개발 에이전트가 병렬로 작업하려면 공유 스키마가 코드로 먼저 존재해야 한다. `dataclass`를 권장한다 — pydantic보다 의존성이 가볍고, 이 오퍼레이터 규모에서는 실행 시 검증보다 타입 힌트로 충분하다.

```python
# schemas.py
from dataclasses import dataclass
from typing import Literal, Optional

@dataclass
class TrafficSnapshot:
    status: Literal["ok", "no_data", "collection_failed"]
    rps: Optional[float]           # requests per second, 전체 backend 합산
    error_rate: Optional[float]    # 0.0 ~ 1.0
    p50_latency_ms: Optional[float]
    p95_latency_ms: Optional[float]
    p99_latency_ms: Optional[float]
    per_backend: dict              # {backend_name: {rps, error_rate, ...}} — 격리 판단용

@dataclass
class Decision:
    action: Literal["noop", "scale", "reroute", "isolate_backend"]
    reason: str                    # 판단 근거 (어떤 지표가 어떤 임계값을 초과)
    target_replicas: Optional[int] = None
    backend_weights: Optional[dict] = None  # {backend_name: weight}
```

필드 단위(latency는 ms, error_rate는 0~1 비율)를 주석으로 고정한다 — 단위 불일치는 경계면 버그의 흔한 원인이다.

## 후속 작업

CRD 필드 추가/변경 요청이나 "이 인터페이스에 필드 하나 더 필요해" 같은 요청도 이 스킬로 처리한다. 변경 시 영향받는 모듈(어떤 필드를 누가 소비하는지)을 먼저 파악하고, 소비자 전원에게 변경을 알린다.
